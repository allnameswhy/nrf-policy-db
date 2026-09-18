"""PDF → 계층 .md 변환 (파이프라인 1단계).

PROJECT_NOTES.md §3 단계 ① 참조. 코퍼스 실측(2026-08, 10권) 기반 설계.

- 입력: pdfs/*.pdf (자체 글롭 확장 — PowerShell 미확장 대응)
- 출력: reports/{report_id}.md (frontmatter + 헤딩 계층 + 본문. LLM 요약 없음)
- 도구: PyMuPDF로만 텍스트 추출 (`import pymupdf` — fitz 별칭은 deprecated). LLM 사용 금지.
- 계층 판별: 북마크 미사용. L1(장) 문법 프로파일 6종 순차 시도 + 하위 패밀리 캐스케이드
  (보고서 전역 첫 등장 순서 → ##~####, 4단계 캡, 부모 범위 내 1부터 단조증가 검증).
- 헤딩 ID: 감지 서수 경로 {report_id}_c{i}s{j}… (PROJECT_NOTES §4)
- report_id: **등록부 report_ids.tsv 조회만**(src/registry.py — 1단계 register.py가 표지
  관리번호로 채움, 2026-09-04). 파일명 유도·슬러그 폴백 없음 — 미등록 PDF는 프리플라이트에서
  `skipped_unregistered`로 시끄럽게 스킵. **합본 분할도 등록부**(2026-09-08): register가
  경량 스캔(detect_parts)으로 파트마다 행(rid, pages)을 만들고 여기서는 그 범위를 그대로
  쓴다 — 파트도 각자 rid(`_NN` 접미 폐지), 파트 하나가 공란이면 파일 전체 미등록.
- 진단: --scan (md 미작성, 감지 결과만 덤프). 로그: logs/extract_log.json
- 구조(장 문법)가 잡히지 않는 문서는 **플랫 청킹 폴백**(투 레인): 문단 경계 그리디
  크기 청킹 → 합성 '# 구간 N (p.a-b)' 헤딩 + frontmatter 'structure: flat' 표식으로
  수용해 다운스트림(mdio/annotate/build_*)이 무수정 처리한다. 본문 텍스트 400자
  미만(이미지 위주 문서)이면 종전대로 스킵 + 로그 후 수동 확인(OCR 별도 과제).
- 레인 수동 지정: --lane {auto,structured,flat} (기본 auto=자동 분기).
  structured = 감지 실패 시 플랫 폴백 억제·시끄러운 스킵(승격 검토 대상),
  flat = 구조 감지 생략·곧장 구간 청킹. 감지 실패 시 l1_attempts + 헤딩 의심 줄
  샘플(l1_suspects)을 로그·--scan에 남겨 프로파일 승격 판단 재료로 쓴다.
- 재실행 가드: 출력 .md가 이미 있으면 기본 스킵, --force로만 재추출·덮어쓰기.
  재추출은 annotate가 삽입한 요약 블록과 verify가 기록한 verified_extract·
  verified_annotate 스탬프를 소실시킨다(소실 = 의도된 미검증 리셋). 합본은 파트
  일부의 .md만 있어도 스킵 — 부분 실패 재시도는 --force뿐이며 성공 파트의 요약도 함께
  소실됨에 주의. --scan은 가드 미적용.
- 검증 스탬프 1단 verified_register(2026-09-07): .md를 만드는 시점에 등록부 rid 조회가
  이미 성공했으므로 extract가 frontmatter에 오늘 날짜로 기록한다(재추출 시 새로 기록).
  이후 verify가 verified_extract → verified_annotate를 차례로 얹고, build_db·build_index는
  세 스탬프가 모두 있는 파일만 싣는다.
"""

from __future__ import annotations

import argparse
import bisect
import dataclasses
import difflib
import datetime
import glob as globmod
import json
import re
import sys
import traceback
import unicodedata
from pathlib import Path

import pymupdf

from registry import parts_for, rid_reason

# ---------------------------------------------------------------------------
# 상수 · 정규식
# ---------------------------------------------------------------------------

FOOTER_RE = re.compile(r"^\s*-\s*(\d+|[ivxlcdmIVXLCDM]+)\s*-\s*$")
LEADER_RE = re.compile(r"[·‧․.]{4,}")  # 목차류 리더런 (가운뎃점·마침표 계열)

# 한글 서수열: 하 이후 거~허 연장 실측 (2025-17)
HANGUL_ORD = "가나다라마바사아자차카타파하거너더러머버서어저처커터퍼허"
ROMAN_UNI = "ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ"

# 불릿 정준 랭크표 (사용자 확정). U+2012(‒)는 스캔 시 '-'로 정규화되므로 별도 항목 없음.
BULLET_RANK = {
    "□": 1,
    "○": 2, "ㅇ": 2, "❍": 2, "◉": 2,
    "▸": 3, "▪": 3, "‣": 3,
    "-": 4, "–": 4, "∙": 4, "•": 4, "Ÿ": 4,
}
DASH_BULLETS = {"-", "–"}          # 뒤에 공백 필수 (음수·범위 표기 오탐 방지)
GLYPH_BULLETS = set(BULLET_RANK) - DASH_BULLETS
# 랭크표 밖의 불릿 의심 글리프 — 원문 유지 + 로그
SUSPECT_BULLETS = set("◈☐■◦▶►●◇◆▷▹✓✔")

MAX_HEADING_LEN = 45   # 번호 토큰 이후 허용 글자수
MAX_TITLE_LINE = 60    # 두 줄형 헤딩의 제목 줄 허용 글자수
DIVIDER_MAX_LINES = 15  # 장 구분 페이지(절 목록) 판정: 페이지의 비어있지 않은 줄 상한 (R8)
BULLET_START_RE = re.compile(r"^[-•◦○●▪·※▶▷■□]")  # 구분 페이지 이어짐 줄에서 제외할 불릿 시작

# L1(장) 프로파일 — (이름, 패턴, 두줄형 여부). 시도 순서 = 이 순서.
PROFILES = [
    # R8(2026-09-16, 2024-16): 구분 페이지의 "제1장"(붙여쓰기) 단독 줄도 분리형 — 종전 `\s+`는 "제 1 장"만.
    ("jang_split", re.compile(r"^제\s*(\d{1,2})\s*장$"), True),
    ("jang", re.compile(r"^제\s*(\d{1,2})\s*장[.\s]+\S"), False),
    ("roman_unicode", re.compile(r"^([Ⅰ-Ⅻ])\.\s*\S"), False),
    ("roman_ascii", re.compile(r"^([IVX]{1,4})\.?\s+\S"), False),
    # 로마자 혼용(WAVE_PLAN R1, 2026-09-09 — 정찰 8권): 한 문서에 ASCII "I."와 유니코드 "Ⅱ."가 섞여 위 두
    # 프로파일이 장을 1개만 잡는 판형. 순수 로마 문서는 앞 프로파일이 먼저 채택되므로 무영향.
    ("roman_mixed", re.compile(r"^([IVXⅠ-Ⅻ]{1,4})\.?\s+\S"), False),
    ("arabic", re.compile(r"^(\d{1,2})\.(?!\s*\d)\s+\S"), False),
    ("bare_digit_split", re.compile(r"^(\d{1,2})$"), True),
]

# 감지 실패 시 승격 판단용 헤딩 의심 줄 캐치올(번호 토큰류 줄머리) — 진단 전용, 추출에 무관여
L1_SUSPECT_RE = re.compile(
    r"^(?:제\s*\d{1,3}\s*[가-힣]"            # 제N장/편/부/절 …
    r"|\d{1,3}\s*[장편부절]"                  # N장 (제 생략형)
    r"|\d{1,3}\s*[.)]"                       # N. / N)
    r"|\d{1,3}$"                             # 단독 숫자 (두줄형)
    r"|[Ⅰ-Ⅻⅰ-ⅻ]"                          # 로마 유니코드
    r"|[IVXLC]{1,5}\s*[.)]"                  # 로마 ASCII
    r"|[\[(]\s*\d{1,3}\s*[\])]"              # (N) / [N]
    r"|(?:Chapter|CHAPTER|Part|PART)\s+\S)"  # 영문 표기
)

# 하위 헤딩 패밀리 — 한 줄이 여러 패턴에 걸리면 이 순서의 첫 매치만 인정
SUB_FAMILY_DEFS = [
    ("jeol", re.compile(r"^제\s*(\d{1,2})\s*절[.\s]*\S")),   # R9: 붙여쓴 `제1절연구의…` 허용(2023-07)
    ("num_dot_num", re.compile(r"^(\d{1,2})\.(\d{1,2})\.?\s+\S")),
    ("num_dot", re.compile(r"^(\d{1,2})\.(?!\s*\d)\s*\S")),
    ("paren_num", re.compile(r"^\((\d{1,2})\)\s*\S")),
    ("num_paren", re.compile(r"^(\d{1,2})\)\s*\S")),
    ("ga_dot", re.compile(r"^([가-힣])\.\s*\S")),
    ("ga_paren", re.compile(r"^\(?([가-힣])\)\s*\S")),
]
TWO_LINE_HANGUL = "two_line_hangul"  # 단독 1글자(가~하) + 다음 줄 제목 (2025-12 L4)

# 뒷부속(참고문헌/부록) 마커. '붙임'·'[첨부]'(괄호형)는 본문 한가운데 출현 실측(2025-32)으로 제외.
REF_TITLE_RE = re.compile(r"^\[?\s*참\s*고\s*문\s*헌\s*\]?$")
APPENDIX_RE = re.compile(r"^[<\[]?\s*부\s*록(?=$|[\s\d>\].:_])[>\]]?\s*\.?\s*(.*)$")
ATTACH_RE = re.compile(r"^첨\s*부(?=$|[\s\d.:_])\s*\.?\s*(.*)$")  # 맨몸 첨부 — 무꼬리말 페이지에서만 인정

# 앞부속 페이지 표제(본문 시작 탐색 시 배제)
FRONT_TITLE_RE = re.compile(
    r"^\s*(요\s*약\s*문|SUMMARY|Summary|CONTENTS|Contents|목\s*차|제\s*출\s*문"
    r"|표\s*차\s*례|그\s*림\s*차\s*례|표\s*목\s*차|그\s*림\s*목\s*차"
    r"|List of Tables|List of Figures)\s*$"
)
ABSTRACT_TITLE_RE = re.compile(r"최\s*종\s*보\s*고\s*서\s*초\s*록")

# 삭제 대상 템플릿 잔재(접기 매칭)
TEMPLATE_FOLD = ("편집순서", "(정책과제관리번호기재)")
# 안내문/판권 페이지 신호(파트 끝 3페이지 이내에서만)
COLOPHON_HINTS = ("개인적견해", "글꼴은문", "이하여백")

CAPTION_RE = re.compile(r"^\s*[\[<(〈［]?\s*(?:그림|표)\s*[\d.\-]+")
PAGE_COUNT_CELL_RE = re.compile(
    r"^\s*\d*\s*(페이지|쪽|면수|면)\s*\.?\s*$|^\s*\d+\s*(페이지|쪽|면수|면)\s*$|^\s*\d{1,4}\s*$")

PUA_RE = re.compile(r"[-]")


def fold(s: str) -> str:
    """공백을 모두 제거해 접기 비교용 문자열로."""
    return re.sub(r"\s+", "", s or "")


# 불릿 통일(글리프→'- ', 셀 불릿→'•')·파이프 이스케이프가 만드는 표기 차이 무력화용
_FOLD_G_TABLE = {ord(c): None for c in GLYPH_BULLETS | DASH_BULLETS | {"|", "\\"}}


def fold_g(s: str) -> str:
    """fold + 글리프·불릿·표 구두점 제거 — 표기 정규화를 무시한 순수 글자 대조용.

    양측에 대칭 적용하면 의도된 리라이트는 통과하고 글자 소실만 남는다.
    마스킹 안전판(build_body)과 verify.py가 공유한다.
    """
    return fold(s).translate(_FOLD_G_TABLE)


def roman_ascii_to_int(s: str) -> int:
    vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
    total = 0
    for i, ch in enumerate(s):
        v = vals.get(ch, 0)
        if i + 1 < len(s) and vals.get(s[i + 1], 0) > v:
            total -= v
        else:
            total += v
    return total


def join_fragments(fragments: list[str]) -> str:
    """HWP 줄바꿈 재조합.

    공백 규칙(확정 + 스팟체크 보정 1건):
    - 앞 끝 [A-Za-z0-9%).] AND 뒤 시작 [A-Za-z0-9(] → 공백 1개 (확정 규칙)
    - 앞 끝 한글 AND 뒤 시작 [A-Za-z0-9] → 공백 1개 ("으로2025년" 방지 보정.
      '('는 제외 — "협력(MOU)"류는 원문에서 붙음)
    핵심 보존 케이스: "공\\n유"→"공유"(한글+한글 무공백), "2025\\n년"→"2025년"(숫자+한글 무공백).
    """
    out = ""
    for frag in fragments:
        frag = frag.strip()
        if not frag:
            continue
        if not out:
            out = frag
            continue
        if (re.search(r"[A-Za-z0-9%).]$", out) and re.match(r"^[A-Za-z0-9(]", frag)) or \
                (re.search(r"[가-힣]$", out) and re.match(r"^[A-Za-z0-9]", frag)):
            out += " " + frag
        else:
            out += frag
    return out


# ---------------------------------------------------------------------------
# 스캔 캐시
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Line:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    bold: bool
    in_table: bool = False
    is_leader: bool = False
    is_footnote: bool = False


@dataclasses.dataclass
class Table:
    bbox: tuple
    rows: list          # extract() 원본 행렬
    markdown: str | None = None   # 정리 패스 결과. 퇴화 시 None(마스킹도 안 함)
    caption: str | None = None


@dataclasses.dataclass
class PageScan:
    index: int                    # 0-based PDF 페이지
    width: float
    height: float
    footer: str | None            # 꼬리말 값 원문("3", "iv", "II" …)
    footer_arabic: int | None
    lines: list = dataclasses.field(default_factory=list)
    tables: list = dataclasses.field(default_factory=list)
    n_images: int = 0
    text_chars: int = 0
    leader_lines: int = 0
    image_only: bool = False
    blank: bool = False
    dropped: bool = False   # 안내문/판권 등 통페이지 삭제 표시


def scan_page(page, index: int, tables: bool = True) -> PageScan:
    """페이지 1장 스캔. tables=False = 표 인식(find_tables) 생략 — register의 합본 판별용
    경량 스캔(0.5~1.3초/권, 2026-09-08 실측). 꼬리말·공백·이미지·리더 줄 판정은 표와 무관하므로
    detect_parts 결과는 두 모드가 동일해야 한다(verify가 전체 스캔으로 대조)."""
    d = page.get_text("dict")
    scan = PageScan(index=index, width=page.rect.width, height=page.rect.height,
                    footer=None, footer_arabic=None)

    raw_lines = []
    for block in d.get("blocks", []):
        if block.get("type") == 1:
            scan.n_images += 1
            continue
        for ln in block.get("lines", []):
            text = "".join(sp.get("text", "") for sp in ln.get("spans", []))
            text = unicodedata.normalize("NFC", text).replace("‒", "-").replace(" ", " ")
            if not text.strip():
                continue
            x0, y0, x1, y1 = ln["bbox"]
            size = max((sp.get("size", 0.0) for sp in ln.get("spans", [])), default=0.0)
            bold = any(sp.get("flags", 0) & 16 for sp in ln.get("spans", []))
            raw_lines.append(Line(text=text.rstrip(), x0=x0, y0=y0, x1=x1, y1=y1,
                                  size=round(size, 1), bold=bold))
    raw_lines.sort(key=lambda l: (round(l.y0, 1), round(l.x0, 1)))

    # 꼬리말: FOOTER_RE 매치 줄은 전부 제거, 값은 가장 아래 것
    kept, footer_line = [], None
    for ln in raw_lines:
        if FOOTER_RE.match(ln.text.strip()):
            footer_line = ln
            continue
        kept.append(ln)
    if footer_line is not None:
        val = FOOTER_RE.match(footer_line.text.strip()).group(1)
        scan.footer = val
        if val.isdigit():
            scan.footer_arabic = int(val)

    # 표
    if tables:
        try:
            tabs = page.find_tables()
            for t in tabs.tables:
                try:
                    rows = t.extract()
                except Exception:
                    rows = []
                scan.tables.append(Table(bbox=tuple(t.bbox), rows=rows))
        except Exception:
            pass
    for tab in scan.tables:
        tab.caption, tab.markdown = clean_table(tab.rows)

    # 표 bbox 마스킹 대상 표시 (정리 성공한 표만)
    for ln in kept:
        area = max((ln.x1 - ln.x0), 0.1) * max((ln.y1 - ln.y0), 0.1)
        for tab in scan.tables:
            if tab.markdown is None:
                continue
            bx0, by0, bx1, by1 = tab.bbox
            ix = max(0.0, min(ln.x1, bx1) - max(ln.x0, bx0))
            iy = max(0.0, min(ln.y1, by1) - max(ln.y0, by0))
            if ix * iy >= 0.5 * area:
                ln.in_table = True
                break
        if LEADER_RE.search(ln.text):
            ln.is_leader = True
            scan.leader_lines += 1

    scan.lines = kept
    scan.text_chars = sum(len(l.text.strip()) for l in kept)
    scan.image_only = scan.text_chars < 50 and scan.n_images >= 1
    scan.blank = scan.text_chars == 0 and scan.n_images == 0
    return scan


def scan_document(doc, tables: bool = True) -> list[PageScan]:
    scans = [scan_page(page, i, tables=tables) for i, page in enumerate(doc)]
    strip_running_heads(scans)
    return scans


# 러닝헤드(장제목/보고서 제목 + 쪽 번호) — 맨 위/맨 아래 줄. 번호가 뒤(A) 또는 앞(B).
RUN_HEAD_A = re.compile(r"^(?P<t>\S.*?\S)\s*(?:[·･‧_|\-–—]\s*)?(?P<n>\d{1,3})$")
RUN_HEAD_B = re.compile(r"^(?P<n>\d{1,3})\s*(?:[·･‧_|\-–—]\s*)?(?P<t>[^\s.)\]}>]\S*.*)$")
RUN_HEAD_MAX_LEN = 80
RUN_HEAD_BAND = 0.15        # 페이지 높이 대비 맨 위/맨 아래 띠
RUN_HEAD_MIN_PAGES = 3      # 같은 위치·같은 쪽 번호 오프셋이 이만큼 이상이어야 러닝헤드로 인정


def strip_running_heads(scans: list[PageScan]) -> int:
    """러닝헤드 제거(WAVE_PLAN R5, 2026-09-09): 쪽 번호가 `- N -`가 아니라 머리말·꼬리말의 제목 줄에 붙은 판형
    (2019-17 `Ⅱ. 책임있는 연구수행  37`, 2024-11 `제2장 … 현황  35`/`34  사내대학원 …`, 2024-40 `… 연구 ･ 141`,
    `National Research Foundation _ 57`)은 FOOTER_RE가 못 걸러 매 쪽 제목 줄이 본문·헤딩에 섞인다.
    규칙(문서 단위, 표 비의존 — 경량·전체 스캔 동일): 각 쪽의 맨 위(y0 < 15%)·맨 아래(y1 > 85%) 줄 중 리더런이
    아니고 80자 이하이며 `제목 [구분자] 숫자` 또는 `숫자 [구분자] 제목`인 줄을 후보로 모아, 같은 위치에서
    **쪽 번호 − 쪽 인덱스(오프셋)가 같은 후보가 3쪽 이상**인 묶음만 러닝헤드로 본다(제목이 장마다 바뀌어도,
    합본으로 오프셋이 달라져도 각 묶음이 독립 판정; 우연히 숫자로 끝나는 본문 줄은 오프셋이 안 맞아 남는다).
    제거한 줄의 번호는 그 쪽에 꼬리말이 없을 때 footer/footer_arabic로 채운다(`- 1 -` 앵커·합본 분할이 이런
    판형에서도 작동). 번호 없는 러닝헤드(2019-70 머리말)와 맨몸 숫자 꼬리말은 대상이 아니다. 반환 = 제거 줄 수."""
    groups: dict[tuple[str, int], list[tuple[PageScan, Line, int]]] = {}
    for s in scans:
        if not s.lines:
            continue
        top = min(s.lines, key=lambda l: l.y0)
        bot = max(s.lines, key=lambda l: l.y1)
        for pos, ln, in_band in (("top", top, top.y0 < RUN_HEAD_BAND * s.height),
                                 ("bot", bot, bot.y1 > (1 - RUN_HEAD_BAND) * s.height)):
            t = ln.text.strip()
            if not in_band or ln.is_leader or len(t) > RUN_HEAD_MAX_LEN:
                continue
            m = RUN_HEAD_A.match(t) or RUN_HEAD_B.match(t)
            # 제목부는 글자를 포함해야 한다 — 맨몸 세 자리 쪽 번호("100" → "10"+"0")·소수("82.3")는 러닝헤드가 아님
            if not m or len(fold(m.group("t")).strip("·･‧_|-–—")) < 2 \
                    or not re.search(r"[^\d\s·･‧_|\-–—.,]", m.group("t")):
                continue
            n = int(m.group("n"))
            groups.setdefault((pos, n - s.index), []).append((s, ln, n))
    removed = 0
    for members in groups.values():
        if len(members) < RUN_HEAD_MIN_PAGES:
            continue
        for s, ln, n in members:
            if ln not in s.lines:
                continue
            s.lines.remove(ln)
            removed += 1
            s.text_chars = sum(len(l.text.strip()) for l in s.lines)
            s.image_only = s.text_chars < 50 and s.n_images >= 1
            s.blank = s.text_chars == 0 and s.n_images == 0
            if s.footer is None:
                s.footer = str(n)
                s.footer_arabic = n
    return removed


def mark_footnotes(scans: list[PageScan], body_range: tuple[int, int]) -> int:
    """본문 최빈 폰트 크기 기준으로 페이지 하단 'N)' 각주(+연속줄) 표시. 표시 건수 반환."""
    sizes = {}
    for scan in scans[body_range[0]:body_range[1] + 1]:
        for ln in scan.lines:
            if ln.in_table or ln.is_leader:
                continue
            sizes[ln.size] = sizes.get(ln.size, 0) + len(ln.text)
    if not sizes:
        return 0
    modal = max(sizes.items(), key=lambda kv: kv[1])[0]
    count = 0
    for scan in scans[body_range[0]:body_range[1] + 1]:
        in_fn = False
        last_y1 = 0.0
        for ln in scan.lines:
            small = ln.size <= modal - 1.3
            bottom = ln.y0 > 0.66 * scan.height
            height = max(ln.y1 - ln.y0, 1.0)
            adjacent = in_fn and (ln.y0 - last_y1) < 2.5 * height
            if small and bottom and re.match(r"^\d{1,3}\)\s*\S", ln.text.strip()):
                ln.is_footnote = True
                in_fn = True
                count += 1
            elif in_fn and small and bottom and adjacent:
                ln.is_footnote = True   # 각주 연속줄 (수직 인접일 때만 — 작은 폰트 본문 오인 방지)
            else:
                in_fn = False
            if ln.is_footnote:
                last_y1 = ln.y1
    return count


# ---------------------------------------------------------------------------
# 표 재구성 (find_tables().extract() 행렬 → GFM)
# ---------------------------------------------------------------------------

def clean_cell(text: str | None) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text).replace("‒", "-").replace(" ", " ")
    frags = []
    for frag in text.split("\n"):
        frag = frag.strip()
        if not frag:
            continue
        # 셀 내부 불릿은 단일 글리프 •
        m = re.match(r"^([□○ㅇ❍◉▸▪‣∙•Ÿ]|-\s)\s*(.*)$", frag)
        if m:
            frag = "• " + m.group(2)
        frags.append(frag)
    # 불릿 항목은 그대로 두고, 아닌 조각들만 재조합
    if any(f.startswith("• ") for f in frags):
        out, buf = [], []
        for f in frags:
            if f.startswith("• "):
                if buf:
                    out.append(join_fragments(buf))
                    buf = []
                out.append(f)
            else:
                buf.append(f)
        if buf:
            out.append(join_fragments(buf))
        joined = " ".join(out)
    else:
        joined = join_fragments(frags)
    return joined.replace("|", "\\|").strip()


def clean_table(rows: list) -> tuple[str | None, str | None]:
    """extract() 행렬 → (캡션, GFM 마크다운). 퇴화 시 (None, None)."""
    if not rows:
        return None, None
    grid = [[clean_cell(c) for c in row] for row in rows]

    # 캡션 흡수 행 복원: 비어있지 않은 셀이 1개뿐이고 그 값이 캡션 패턴
    caption = None
    while grid:
        nonempty = [c for c in grid[0] if c]
        if len(nonempty) == 1 and CAPTION_RE.match(nonempty[0]):
            caption = nonempty[0]
            grid = grid[1:]
        else:
            break

    if not grid:
        return caption, None

    # 연속 중복 셀 병합(병합셀 스팬 잔재) — 뒤쪽을 비움
    for row in grid:
        for i in range(len(row) - 1, 0, -1):
            if row[i] and row[i] == row[i - 1]:
                row[i] = ""

    # 전체 공백 열 제거
    ncols = max(len(r) for r in grid)
    for row in grid:
        row.extend([""] * (ncols - len(row)))
    keep_cols = [j for j in range(ncols) if any(r[j] for r in grid)]
    grid = [[r[j] for j in keep_cols] for r in grid]
    # 전체 공백 행 제거
    grid = [r for r in grid if any(c for c in r)]

    if len(grid) < 2 or (grid and len(grid[0]) < 2):
        return caption, None

    lines = ["| " + " | ".join(grid[0]) + " |",
             "|" + "|".join([" --- "] * len(grid[0])) + "|"]
    for row in grid[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return caption, "\n".join(lines)


def page_table_fold(tables: list) -> str:
    """페이지의 정리 성공한 표들(caption+markdown)의 fold_g 결합 — 커버 판정용 국소 haystack."""
    parts = []
    for tab in tables:
        if tab.markdown is None:
            continue
        if tab.caption:
            parts.append(tab.caption)
        parts.append(tab.markdown)
    return fold_g("\n".join(parts))


def table_covers(text: str, table_fold: str) -> bool:
    """마스킹될 줄의 글자가 표 정리 결과에 실제로 실렸는가 — 안 실린 줄을 지우면 소실.

    find_tables().extract()가 행렬 생성 단계에서 셀 텍스트를 놓치는 실측(4권 21건)
    대응. ① fold_g 부분문자열(대부분), ② 공백 토큰 전건 포함(병합셀 중복 붕괴·셀 내
    조각 재배치 등 clean_table의 무손실 재배열 흡수 — 페이지 국소 haystack 전제라
    타처 우연 일치로 진짜 소실이 가려지지 않는다).
    """
    needle = fold_g(text)
    if not needle:
        return True
    if needle in table_fold:
        return True
    return all(t in table_fold for t in (fold_g(tok) for tok in text.split()) if t)


# ---------------------------------------------------------------------------
# report_id · 합본 분리
# ---------------------------------------------------------------------------

def find_existing_outputs(out_dir: Path, rids: list[str]) -> list[Path]:
    """등록부 rid들(합본이면 파트마다 각자 rid)의 기존 출력 {rid}.md 탐지 — 재실행 가드."""
    return [p for p in (Path(out_dir) / f"{rid}.md" for rid in rids) if p.exists()]


def detect_parts(scans: list[PageScan]) -> list[tuple[int, int]]:
    """합본 파트 경계 — register(경량 스캔, 등록부 pages 열의 출처)와 verify(전체 스캔, 대조)가
    공유하는 **유일한** 분할 규칙. 표(find_tables) 데이터에 의존하지 않는 PageScan 필드만
    써야 두 스캔이 일치한다(footer_arabic·blank·image_only·leader_lines·lines 수).
    = split_bundle(쪽 번호 재시작 분할) → merge_front_parts(앞부속 오분리 병합, WAVE_PLAN R2)."""
    return merge_front_parts(scans, split_bundle(scans))


def part_has_body(scans: list[PageScan], part: tuple[int, int]) -> bool:
    """파트에 본문 시작(1장 후보 페이지)이 있는가. 표 내부 표식을 무시해(ignore_tables) 경량 스캔과
    전체 스캔이 같은 답을 낸다 — detect_parts 병합 판정 전용."""
    return find_body_start(scans, part, ignore_tables=True)[0] is not None


def merge_front_parts(scans: list[PageScan], parts: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """앞부속 오분리 병합(WAVE_PLAN §4 R2, 2026-09-09): 앞부속(제출문·요약문·목차)이 물리 쪽 번호로
    매겨지고 본문이 '- 1 -'로 재시작하는 판형은 split_bundle이 앞부속을 별도 파트로 자른다(정찰 15권 —
    그 파트는 본문 시작이 없어 skipped_no_body_start, 초록은 유실). 본문 시작이 없는 파트는 뒤 파트에
    붙인다(앞부속이 연속이면 누적, 마지막 파트는 그대로). 진짜 합본·요약문 파트형 6권은 파트 1에 본문
    시작이 있어 무영향(경량 스캔 실측 2026-09-09)."""
    out: list[tuple[int, int]] = []
    i = 0
    while i < len(parts):
        start, end = parts[i]
        while i + 1 < len(parts) and not part_has_body(scans, (start, end)):
            i += 1
            end = parts[i][1]
        out.append((start, end))
        i += 1
    return out


def split_bundle(scans: list[PageScan]) -> list[tuple[int, int]]:
    """아라비아 꼬리말 런이 1보다 큰 값까지 진행된 뒤 '- 1 -'로 재시작하고, **재시작 앞 구간(현재 파트
    시작~직전 쪽)에 이미 본문 시작이 있으면** 파트 경계(R8, 2026-09-16 사용자 결정 — 종전 "사이에 무꼬리말/
    로마/목차 페이지가 있어야 경계"를 대체). 앞부속이 본문과 연속 아라비아인 판형(2025-02)은 앞 구간에
    본문이 없어 분리되지 않고, 본편 뒤에 쪽 번호가 1로 재시작하는 부록 책자가 간격 쪽 없이 붙은 판형
    (2024-23 p127 꼬리말 109 → p128 '- 1 -')은 분리된다 — 분리하지 않으면 find_body_start의 앵커(파트 내
    마지막 '- 1 -')가 책자로 뛰어 본편이 통째로 앞부속 취급되는 무음 유실(실측 106쪽). 게이트는
    part_has_body(표 비의존)라 register 경량 스캔·verify 전체 스캔이 같은 분할을 낸다. 경계 뒤 '뒤로
    건너뛰기'는 새 파트의 번호 없는 표지·목차를 새 파트에 귀속시키는 단계(2025-17 p139 표지)로 유지.
    """
    n = len(scans)
    run_max = 0
    boundaries = [0]

    def frontish(s: PageScan) -> bool:
        return s.blank or s.image_only or s.leader_lines >= 3 or \
            (bool(s.lines) and s.leader_lines / len(s.lines) > 0.3)

    for i, s in enumerate(scans):
        if s.footer_arabic is None:
            continue
        if s.footer_arabic == 1 and run_max > 1 and part_has_body(scans, (boundaries[-1], i - 1)):
            # 경계: 재시작 지점에서 뒤로, 앞부속스러운 페이지(무꼬리말/리더런/공백/이미지,
            # 잔류 꼬리말이 남은 그림목차 등)를 건너뛴 지점 (2025-17 p139 표지 실측)
            k = i - 1
            while k > boundaries[-1]:
                sk = scans[k]
                if sk.footer_arabic is not None and not frontish(sk):
                    break
                k -= 1
            start = k + 1
            if start > boundaries[-1]:
                boundaries.append(start)
            run_max = 1
        else:
            run_max = max(run_max, s.footer_arabic)
    parts = []
    for bi, start in enumerate(boundaries):
        end = (boundaries[bi + 1] - 1) if bi + 1 < len(boundaries) else n - 1
        parts.append((start, end))
    return parts


# ---------------------------------------------------------------------------
# 최종보고서 초록 파싱
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Meta:
    title: str = ""
    title_en: str = ""
    lead_researcher: str = ""
    institution: str = ""
    keywords_ko: list = dataclasses.field(default_factory=list)
    keywords_en: list = dataclasses.field(default_factory=list)
    abstract: str = ""
    abstract_page: int | None = None
    warnings: list = dataclasses.field(default_factory=list)


def _split_keywords(text: str) -> list[str]:
    text = join_fragments(text.split("\n")).strip().rstrip(".")  # 셀 내 줄바꿈 재조합
    if not text:
        return []
    # 쉼표 우선. 쉼표가 없고 가운뎃점만 있으면 가운뎃점 분리(키워드 내부 · 보존 목적).
    sep = "," if "," in text else ("·" if "·" in text else None)
    if sep is None:
        return [text]
    return [t.strip() for t in text.split(sep) if t.strip()]


def _parse_title_cell(text: str, meta: Meta) -> None:
    ko = re.search(r"\(\s*한\s*글\s*\)\s*(.*?)(?=\(\s*영\s*문\s*\)|$)", text, re.S)
    en = re.search(r"\(\s*영\s*문\s*\)\s*(.*)$", text, re.S)
    if ko:
        meta.title = join_fragments(ko.group(1).split("\n"))
    if en:
        meta.title_en = join_fragments(en.group(1).split("\n"))
    if not ko and not en and text.strip() and not meta.title:
        meta.title = join_fragments(text.split("\n"))


def _parse_researcher_cell(text: str, meta: Meta) -> None:
    text = join_fragments(text.split("\n"))
    m = re.match(r"^(.*?)\s*[(（](.*?)[)）]\s*$", text)
    if m:
        meta.lead_researcher = m.group(1).strip()
        meta.institution = m.group(2).strip()
    else:
        meta.lead_researcher = text.strip()


def parse_abstract(scans: list[PageScan], part: tuple[int, int]) -> Meta:
    meta = Meta()
    page = None
    for scan in scans[part[0]:min(part[0] + 8, part[1] + 1)]:
        if any(ABSTRACT_TITLE_RE.search(l.text) for l in scan.lines):
            page = scan
            break
    if page is None:
        meta.warnings.append("초록 페이지 없음 — 메타데이터 공란")
        return meta
    meta.abstract_page = page.index

    rows = None
    for tab in page.tables:
        if tab.rows and len(tab.rows) >= 4:
            rows = tab.rows
            break

    def handle(label: str, value: str, state: dict) -> None:
        f = fold(label)
        if not f:
            # 라벨 없는 연속 값 — 진행 중인 모드로 귀속
            if not value.strip():
                return
            if state["mode"] == "abstract":
                state["abstract"].append(value)
            elif state["mode"] == "keywords":
                _consume_keywords(value.strip(), state, meta)
            elif state["mode"] == "title":
                _parse_title_cell(value, meta)
            return
        if f.startswith("정책과제명") or f == "과제명":
            state["mode"] = "title"
            _parse_title_cell(value, meta)
        elif f.startswith("연구책임자"):
            state["mode"] = None
            _parse_researcher_cell(value, meta)
        elif f.startswith("요약"):
            state["mode"] = "abstract"
            if value.strip():
                state["abstract"].append(value)
        elif f.startswith("색인어") or f in ("한글", "한글:", "영어", "영문"):
            state["mode"] = "keywords"
            if f.startswith("색인어"):
                value_full = value
            else:
                value_full = label + " " + value
            _consume_keywords(value_full, state, meta)
        elif f.startswith("면수"):
            # 요약 행의 면수 부속 셀 — 모드 보존, 면수 아닌 값만 연속 처리
            if value.strip():
                handle("", value, state)
        elif f.startswith(("관리번호", "연구기간", "참여연구원", "연구용역비", "연구비")):
            state["mode"] = None
        else:
            # 라벨이 아닌 셀 — 진행 중인 모드의 연속 값으로 처리
            if state["mode"] == "abstract":
                state["abstract"].append(label if not value else label + "\n" + value)
            elif state["mode"] == "keywords":
                _consume_keywords((label + " " + value).strip(), state, meta)
            elif state["mode"] == "title":
                _parse_title_cell(label + "\n" + value, meta)

    def _consume_keywords(text: str, state: dict, meta: Meta) -> None:
        for m in re.finditer(r"[(（]?\s*(한\s*글|영\s*어|영\s*문)\s*[)）]?\s*[::]?\s*", text):
            state.setdefault("kw_marks", []).append((m.start(), m.end(), fold(m.group(1))))
        marks = state.pop("kw_marks", [])
        if not marks:
            tgt = "en" if state.get("kw_last") == "en" else "ko"
            vals = _split_keywords(text)
            if tgt == "en":
                meta.keywords_en.extend(vals)
            else:
                meta.keywords_ko.extend(vals)
            return
        for i, (s, e, name) in enumerate(marks):
            end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
            seg = text[e:end]
            if name.startswith("한"):
                meta.keywords_ko.extend(_split_keywords(seg))
                state["kw_last"] = "ko"
            else:
                meta.keywords_en.extend(_split_keywords(seg))
                state["kw_last"] = "en"

    state = {"mode": None, "abstract": []}
    MAIN_LABELS = ("관리번호", "연구기간", "정책과제명", "과제명", "연구책임자", "참여연구원",
                   "연구용역비", "연구비", "요약", "색인어", "면수")
    KW_SUB_LABELS = ("한글", "영어", "영문")

    def label_kind(cell_fold: str, kw_context: bool) -> str | None:
        if len(cell_fold) > 40:
            return None
        for lb in MAIN_LABELS:
            if cell_fold.startswith(lb):
                return "main"
        if kw_context and cell_fold.rstrip(":：") in KW_SUB_LABELS:
            return "kw_sub"
        return None

    if rows:
        for row in rows:
            cells = [c for c in ((c or "").strip() for c in row) if c]
            if not cells:
                continue
            # 한 행에 라벨/값 쌍이 여러 개 올 수 있음(연구책임자|값|참여연구원수|값|… 실측)
            kw_context = state["mode"] == "keywords" or \
                any(fold(c).startswith("색인어") for c in cells)
            segments: list[list] = []   # [라벨 or None, [값들]]
            for c in cells:
                kind = label_kind(fold(c), kw_context)
                if kind is not None:
                    segments.append([c, []])
                elif segments:
                    segments[-1][1].append(c)
                else:
                    segments.append([None, [c]])
            for label, vals in segments:
                vals = [v for v in dict.fromkeys(vals) if not PAGE_COUNT_CELL_RE.match(v)]
                value = "\n".join(vals)
                if label is None:
                    handle("", value, state)
                elif fold(label).rstrip(":：") in KW_SUB_LABELS:
                    handle("색인어", f"({fold(label)}) {value}", state)
                else:
                    handle(label, value, state)
    else:
        # 표 미검출 폴백: 줄 단위 라벨 매칭
        meta.warnings.append("초록 표 미검출 — 줄 단위 폴백 파싱")
        lines = [l.text.strip() for l in page.lines if l.text.strip()
                 and not ABSTRACT_TITLE_RE.search(l.text)]
        buf_label, buf_val = None, []
        labels = ("관리번호", "연구기간", "정책과제명", "연구책임자", "참여연구원",
                  "연구용역비", "연구비", "요약", "색인어", "면수")
        for text in lines:
            f = fold(text)
            if any(f.startswith(fold(lb)) for lb in labels) and len(f) <= 30:
                if buf_label is not None:
                    handle(buf_label, "\n".join(buf_val), state)
                buf_label, buf_val = text, []
            else:
                buf_val.append(text)
        if buf_label is not None:
            handle(buf_label, "\n".join(buf_val), state)

    # 초록 요약: 불릿 항목 단위로 줄바꿈 재조합(항목 시작 글리프·※·번호 유지, 랩 줄은 결합)
    abstract_lines = []
    for chunk in state["abstract"]:
        for frag in chunk.split("\n"):
            frag = frag.strip()
            if not frag or PAGE_COUNT_CELL_RE.match(frag):
                continue
            starts_item = frag[0] in GLYPH_BULLETS or frag[0] in "※§◾▪•" or \
                re.match(r"^[-–]\s", frag) or re.match(r"^[\[(]", frag)
            if abstract_lines and not starts_item:
                abstract_lines[-1] = join_fragments([abstract_lines[-1], frag])
            else:
                abstract_lines.append(frag)
    meta.abstract = "\n".join(abstract_lines)

    for field, name in ((meta.title, "title"), (meta.lead_researcher, "lead_researcher"),
                        (meta.institution, "institution"), (meta.abstract, "abstract")):
        if not field:
            meta.warnings.append(f"초록에서 {name} 결측")
    if not meta.keywords_ko:
        meta.warnings.append("초록에서 keywords_ko 결측")
    return meta


def fallback_institution(scans: list[PageScan], part: tuple[int, int], meta: Meta) -> None:
    """기관 결측 시: ① 제출문의 주관연구기관 라벨 ② 표지에서 연구책임자 이름 바로 윗줄."""
    if meta.institution:
        return
    for scan in scans[part[0]:min(part[0] + 4, part[1] + 1)]:
        for i, ln in enumerate(scan.lines):
            m = re.match(r"^[○o•\-\s]*주\s*관\s*연\s*구\s*기\s*관\s*명?\s*[::]?\s*(.*)$", ln.text.strip())
            if m:
                val = m.group(1).strip()
                if not val and i + 1 < len(scan.lines):
                    val = scan.lines[i + 1].text.strip()
                if val:
                    meta.institution = val
                    meta.warnings.append("institution: 제출문 주관연구기관 폴백 사용")
                    return
    if meta.lead_researcher:
        cover = scans[part[0]]
        for i, ln in enumerate(cover.lines):
            if fold(ln.text) == fold(meta.lead_researcher) and i > 0:
                meta.institution = cover.lines[i - 1].text.strip()
                meta.warnings.append("institution: 표지(이름 윗줄) 폴백 사용")
                return
    meta.warnings.append("institution 결측 — 폴백 실패")


def fallback_title(scans: list[PageScan], part: tuple[int, int], meta: Meta) -> None:
    """제목 결측 시(합본 후속 파트 등): 파트 첫 텍스트 페이지에서 최대 폰트 줄(연속 동일 크기 결합)."""
    if meta.title:
        return
    for scan in scans[part[0]:min(part[0] + 3, part[1] + 1)]:
        cands = [l for l in scan.lines if not l.is_leader and l.text.strip()]
        if not cands:
            continue
        top = max(l.size for l in cands)
        frag = [l.text.strip() for l in cands if l.size >= top - 0.3]
        meta.title = join_fragments(frag[:3])
        meta.warnings.append(f"title: p.{scan.index + 1} 최대 폰트 줄 폴백 사용")
        return
    meta.warnings.append("title 결측 — 폴백 실패")


# ---------------------------------------------------------------------------
# 본문 시작 · 구조 감지
# ---------------------------------------------------------------------------

def page_is_front(scan: PageScan) -> bool:
    if scan.leader_lines >= 3:
        return True
    if scan.lines and scan.leader_lines / len(scan.lines) > 0.3:
        return True
    for ln in scan.lines[:6]:
        if FRONT_TITLE_RE.match(ln.text.strip()) or ABSTRACT_TITLE_RE.search(ln.text):
            return True
    return False


def profile_value(name: str, m: re.Match) -> int:
    g = m.group(1)
    if name == "roman_unicode":
        return ROMAN_UNI.index(g) + 1
    if name == "roman_ascii":
        return roman_ascii_to_int(g)
    if name == "roman_mixed":  # 한 글자 유니코드 Ⅰ~Ⅻ 또는 ASCII 조합 — 혼용 글리프 정수화
        return ROMAN_UNI.index(g) + 1 if len(g) == 1 and g in ROMAN_UNI else roman_ascii_to_int(g)
    return int(g)


def line_l1_candidates(ln: Line, next_ln: Line | None, ignore_tables: bool = False) -> list[str]:
    """이 줄에서 1장 후보로 성립하는 프로파일 이름 목록(값=1인 것만).
    ignore_tables=True는 표 내부 표식을 무시한다(경량 스캔과 동일 판정 — detect_parts 병합용)."""
    out = []
    text = ln.text.strip()
    if (ln.in_table and not ignore_tables) or ln.is_leader or ln.is_footnote:
        return out
    for name, pat, two_line in PROFILES:
        m = pat.match(text)
        if not m:
            continue
        try:
            if profile_value(name, m) != 1:
                continue
        except (ValueError, IndexError):
            continue
        if two_line:
            if next_ln is None or not next_ln.text.strip():
                continue
            nt = next_ln.text.strip()
            if len(nt) > MAX_TITLE_LINE or FOOTER_RE.match(nt) or LEADER_RE.search(nt):
                continue
        else:
            rest = text[m.end() - 1:]
            if len(rest) > MAX_HEADING_LEN:
                continue
        out.append(name)
    return out


def page_has_front_title(scan: PageScan, ignore_tables: bool = False) -> bool:
    """표 밖의 앞부속 표제 줄(요약문/SUMMARY/목차/CONTENTS/차례) 존재 여부 — 룩어헤드 가드용."""
    for ln in scan.lines[:8]:
        if ln.in_table and not ignore_tables:
            continue
        if FRONT_TITLE_RE.match(ln.text.strip()) or ABSTRACT_TITLE_RE.search(ln.text):
            return True
    return False


LOOKAHEAD_PAGES = 25  # 후보 페이지 뒤로 앞부속 표제가 남아 있으면 아직 앞부속(요약문 로마 헤딩 오탐 차단)


def _first_l1_cands(scan: PageScan, ignore_tables: bool) -> list[str]:
    """페이지에서 1장 후보가 성립하는 첫 줄의 프로파일 목록(없으면 빈 목록)."""
    for j, ln in enumerate(scan.lines):
        nxt = scan.lines[j + 1] if j + 1 < len(scan.lines) else None
        cands = line_l1_candidates(ln, nxt, ignore_tables)
        if cands:
            return cands
    return []


def find_body_start(scans: list[PageScan], part: tuple[int, int],
                    ignore_tables: bool = False) -> tuple[int | None, list[str]]:
    """(본문 시작 페이지, 그 페이지의 1장 후보 프로파일들). 앵커 = 파트 내 아라비아 '- 1 -' 마지막 페이지.
    ignore_tables=True: 표 내부 표식 무시(경량 스캔과 같은 판정) — detect_parts의 앞부속 병합 판정용."""
    anchor = part[0]
    for i in range(part[0], part[1] + 1):
        if scans[i].footer_arabic == 1:
            anchor = i
    for i in range(anchor, part[1] + 1):
        scan = scans[i]
        if scan.blank or page_is_front(scan):
            continue
        cands = _first_l1_cands(scan, ignore_tables)
        if not cands:
            continue
        if scan.image_only:
            # R8(2026-09-16, 2024-16): 장식 그림 때문에 image_only로 분류된 장 구분 페이지("제1장" + 제목 줄, 텍스트 5자)도
            # 본문 시작이 될 수 있다 — 종전엔 image_only를 무조건 건너뛰어 앵커가 첫 텍스트 쪽의 3단계 항목(Ⅰ.)으로 밀리고
            # 장 구조가 전부 어긋났다. 단 다음 텍스트 쪽이 1장 표식을 되풀이하면(2019-17 `I. 서론`·2021-43·2025-01 실측 —
            # 그림 쪽은 요약 표제·절 목록일 수 있음) 종전대로 그 쪽이 본문 시작(적재본 불변).
            nxt_scan = next((scans[k] for k in range(i + 1, part[1] + 1)
                             if not scans[k].blank and not scans[k].image_only), None)
            if nxt_scan is not None and _first_l1_cands(nxt_scan, ignore_tables):
                continue
        # 룩어헤드: 뒤 25페이지 안에 앞부속 표제가 남아 있으면 이 후보는 요약문/영문요약 내부
        ahead_end = min(i + LOOKAHEAD_PAGES, part[1])
        if any(page_has_front_title(scans[k], ignore_tables) for k in range(i + 1, ahead_end + 1)):
            continue
        return i, cands
    return None, []


# --- 목차 장 번호·R4 장 번호 오타 승격 (extract walk_l1 ↔ verify C 목차 대조 공유) ---------------------------

# 목차 줄용 완화 프로파일 — 목차는 분리형('제 1 장'·단독 숫자)을 한 줄로 조판하므로 기본형으로 대조
RELAXED_PROFILE = {"jang_split": "jang", "bare_digit_split": "arabic"}
BARE_MD_RE = re.compile(r"^(\d{1,2})\s+\S")  # bare_digit .md 헤딩 텍스트("1 제목")용


def relaxed_profile_pats(profile_name: str) -> list[tuple[str, re.Pattern]]:
    """목차 줄용 완화 패턴 목록. 로마자는 유니코드/ASCII 표기가 본문과 어긋나는 실측(2025-29: 목차 Ⅰ.
    vs 본문 II.)이 있어 두 형태 모두 수집한다."""
    pat_of = {n: p for n, p, _ in PROFILES}
    r = RELAXED_PROFILE.get(profile_name, profile_name)
    if r in ("roman_unicode", "roman_ascii"):
        return [("roman_unicode", pat_of["roman_unicode"]), ("roman_ascii", pat_of["roman_ascii"])]
    return [(r, pat_of[r])]


def toc_title_key(line_text: str, m: re.Match) -> str:
    """목차 줄에서 번호 토큰·리더런 이후(쪽 번호)·끝 쪽 번호를 뗀 제목 접기 — R4 제목 일치 판정용."""
    t = line_text[m.end() - 1:]
    t = re.split(r"[·‧․.]{2,}", t)[0]
    t = re.sub(r"\s*\d{1,3}\s*$", "", t)
    return fold(t)


# --- 목차 페이지 인식 (R6, 사용자 채택 2026-09-11) ---------------------------------------------------
# 종전 유일 규칙 = 리더런(`····`) 3줄 이상 페이지. W1 실측 4권(2019-17·2021-81·2021-42·2019-69)은 목차에
# 리더가 없거나(쪽 번호가 다음 줄에 단독), 리더 페이지가 표·그림 목차뿐이거나, 표제 앞에 템플릿 잔재
# 「편집순서 N」이 붙어 대조를 생략했다. 보강: 목차 표제 페이지(「목차/차례」·「CONTENTS」 — 접두·괄호 꼬리
# 「(영문목차)」 허용)와 그 뒤 표제 없는 이어짐 페이지(리더런 3줄 또는 맨몸 숫자 줄 3줄 이상)를 목차로 본다.
# 표·그림 목차 등 다른 앞부속 표제는 이어짐을 끊는다. **국문 우선 2단**(사용자 결정): 장 번호는 1차(리더 페이지
# ∪ 국문 표제·이어짐)에서 수집하고 하나도 없을 때만 2차(영문 CONTENTS·이어짐) — 영문 목차에만 번호가 붙은
# 항목(`VI. References`류)이 「목차에만 있는 장 번호」 헛 FAIL을 내는 노출 차단. FRONT_TITLE_RE·page_is_front·
# find_body_start·detect_parts는 건드리지 않는다(본문 시작·합본 분할 불변).
TOC_TEMPLATE_PREFIX_RE = re.compile(r"^\s*편집순서\s*\d+\s*")
TOC_PAREN_TAIL_RE = re.compile(r"\s*\([^()]*\)\s*$")
TOC_TITLE_KO_RE = re.compile(r"^(?:목\s*차|차\s*례)$")
TOC_TITLE_EN_RE = re.compile(r"^(?:CONTENTS|Contents)$")
BARE_NUM_LINE_RE = re.compile(r"^\d{1,3}$")  # 쪽 번호가 제목 다음 줄에 단독으로 놓인 조판
TOC_MIN_NUM_LINES = 3
TOC_TITLE_SCAN_LINES = 8  # page_has_front_title과 같은 범위


def toc_title_kind(text: str) -> str | None:
    """앞부속 표제 줄 분류 — "ko"(목차/차례) · "en"(CONTENTS) · "other"(표·그림 목차, 요약문, SUMMARY, 제출문,
    초록 등 다른 앞부속 표제 = 목차 이어짐을 끊는 줄) · None(표제 아님)."""
    t = TOC_TEMPLATE_PREFIX_RE.sub("", text or "", count=1)
    t = TOC_PAREN_TAIL_RE.sub("", t, count=1).strip()
    if TOC_TITLE_KO_RE.match(t):
        return "ko"
    if TOC_TITLE_EN_RE.match(t):
        return "en"
    if FRONT_TITLE_RE.match(t) or ABSTRACT_TITLE_RE.search(t):
        return "other"
    return None


def page_toc_title(scan: PageScan) -> tuple[str | None, int]:
    """페이지의 첫 표제 (분류, 줄 인덱스). 목차 표제(ko/en)는 첫 8줄에서만, 다른 앞부속 표제(other)는 페이지
    어디서든 — 목차 꼬리가 같은 쪽에서 표·그림 목차로 넘어가는 지점(2019-17 p14: `V. 결론`·`부록` 뒤 「표 차례」)을
    잡기 위해서다."""
    for j, ln in enumerate(scan.lines):
        kind = toc_title_kind(ln.text.strip())
        if kind == "other" or (kind in ("ko", "en") and j < TOC_TITLE_SCAN_LINES):
            return kind, j
    return None, -1


def page_toc_kind(scan: PageScan) -> str | None:
    return page_toc_title(scan)[0]


def page_toc_like(scan: PageScan) -> bool:
    """표제 없는 이어짐 페이지 판정 — 리더런 3줄 또는 맨몸 숫자 줄 3줄."""
    if scan.leader_lines >= 3:
        return True
    return sum(1 for ln in scan.lines if BARE_NUM_LINE_RE.match(ln.text.strip())) >= TOC_MIN_NUM_LINES


def lines_toc_like(lines) -> bool:
    """표제 앞 꼬리 구간 판정 — 리더 줄 또는 맨몸 숫자 줄이 하나라도 있으면 목차 꼬리."""
    return any(ln.is_leader or BARE_NUM_LINE_RE.match(ln.text.strip()) for ln in lines)


def toc_pages(scans, part, body_start) -> tuple[list[int], list[int]]:
    """(1차, 2차) 목차 페이지 인덱스(오름차순). 1차 = 리더런 페이지 ∪ 국문 표제 페이지·이어짐(종전 집합을
    포함), 2차 = 영문 표제 페이지·이어짐(리더런 페이지는 어느 사슬에 있어도 1차). 사슬은 다른 앞부속 표제·
    빈 쪽·목차답지 않은 쪽에서 끊기되, 표제 앞에 목차 꼬리(맨몸 숫자·리더 줄)가 있는 쪽은 사슬에 넣고 끊는다."""
    primary: list[int] = []
    secondary: list[int] = []
    chain = None  # None | "ko" | "en" — 직전 표제 사슬
    for i in range(part[0], body_start):
        s = scans[i]
        kind, at = (None, -1) if (s.blank or s.image_only) else page_toc_title(s)
        tail_of = None  # 이 쪽 앞부분에서 끝나는 사슬(표·그림 목차 등이 같은 쪽 중간에서 시작)
        if kind in ("ko", "en"):
            chain = kind
        elif kind == "other":
            if chain is not None and at > 0 and lines_toc_like(s.lines[:at]):
                tail_of = chain
            chain = None
        elif s.blank or s.image_only or (chain is not None and not page_toc_like(s)):
            chain = None
        lane = chain or tail_of
        if s.leader_lines >= 3 or lane == "ko":
            primary.append(i)
        elif lane == "en":
            secondary.append(i)
    return primary, secondary


def _collect_toc_titles(scans, pages, pats) -> dict[int, list[str]]:
    titles: dict[int, list[str]] = {}
    for i in pages:
        for ln in scans[i].lines:
            t = ln.text.strip()
            if CAPTION_RE.match(t):
                continue
            for name, pat in pats:
                m = pat.match(t)
                if m:
                    try:
                        titles.setdefault(profile_value(name, m), []).append(toc_title_key(t, m))
                    except (ValueError, IndexError):
                        pass
                    break
    return titles


def toc_chapter_titles(scans, part, body_start, profile_name: str) -> dict[int, list[str]]:
    """앞부속 목차(toc_pages)의 {장 번호: [제목 접기…]} — verify C 목차 대조와 R4 승격 조건이 같은 값을
    쓴다. 표·그림 목차 항목(CAPTION_RE)은 제외. 같은 번호가 여러 줄이면(아라비아 하위 항목 혼입) 전부 보관.
    국문 우선 2단: 1차 페이지에서 하나도 못 모으면 2차(영문 CONTENTS) 페이지에서 수집."""
    pats = relaxed_profile_pats(profile_name)
    primary, secondary = toc_pages(scans, part, body_start)
    titles = _collect_toc_titles(scans, primary, pats)
    if not titles and secondary:
        titles = _collect_toc_titles(scans, secondary, pats)
    return titles


def toc_chapter_values(scans, part, body_start, profile_name: str) -> set[int]:
    """목차 장 번호 집합(toc_chapter_titles의 키)."""
    return set(toc_chapter_titles(scans, part, body_start, profile_name))


def chapter_title_key(text: str) -> str:
    """장 헤딩 텍스트에서 선두 번호 토큰을 뗀 제목 접기 — R4 '다른 제목' 판정용."""
    t = (text or "").strip()
    for _name, pat, _two in PROFILES:
        m = pat.match(t)
        if m:
            return fold(t[m.end() - 1:])
    m = BARE_MD_RE.match(t)
    return fold(t[m.end() - 1:] if m else t)


TITLE_SIM_MIN_LEN = 8    # 유사도 경로를 여는 최소 접기 길이(짧은 제목의 우연 일치 차단)
TITLE_SIM_RATIO = 0.8    # difflib 비율 하한 — 2024-41 목차 Ⅴ '…기관 연계·협력 활성화 방안' vs 본문 '…기관 산학연 연계·협력 활성화' = 0.89


def _toc_title_match(key: str, toc_keys) -> bool:
    """제목 접기 key가 목차 항목 접기 중 하나와 (1) 동일 또는 4자 이상 포함 관계(R4 매칭 규칙 — R9에서 분리), 또는
    (2) 둘 다 TITLE_SIM_MIN_LEN 이상이고 difflib 유사도가 TITLE_SIM_RATIO 이상(R9 — 단어 하나가 삽입·삭제된 표기 차이:
    2024-41 '산학연' 삽입 0.89, 적재본 실측 `실험실창업지원사업 및 유사사업 현황`↔`…사업 현황` 0.81, `개편방안(안)`↔`개편방향(안)` 0.95;
    전혀 다른 제목은 0.0~0.5)."""
    for k in (toc_keys or []):
        if not k:
            continue
        if k == key or (len(k) >= 4 and k in key) or (len(key) >= 4 and key in k):
            return True
        if len(k) >= TITLE_SIM_MIN_LEN and len(key) >= TITLE_SIM_MIN_LEN \
                and difflib.SequenceMatcher(None, k, key).ratio() >= TITLE_SIM_RATIO:
            return True
    return False


def typo_promotable(prev_text: str, cand_text: str, cand_val: int, expected: int,
                    toc: dict[int, list[str]] | None) -> bool:
    """R4 장 번호 오타 승격(WAVE_PLAN §4 R4, 사용자 채택 2026-09-09 — 2021-38 p.143 '제4장 결론 및 시사점'이
    목차의 제5장): 후보가 직전 장과 같은 번호(expected-1)이고 제목이 직전 장과 다르며, **목차의 다음 번호
    (expected) 항목 제목과 일치**(접기 후 동일 또는 4자 이상 포함)할 때만 다음 서수로 승격한다.
    '다른 제목'만으로는 러닝헤드+쪽 번호('제1장 서론 5')·랩 결합 변형·아라비아 하위 항목 '1.'이 승격되는
    오탐이 적재 23권 회귀에서 3권 나와(2026-09-09) 목차 제목 일치를 필수 조건으로 삼았다.
    extract(walk_l1)와 verify(C 목차 대조, md_chapter_values)가 같은 함수를 쓴다.
    **R9(2026-09-18, 사용자 채택)**: 후보 번호가 expected+1(원문이 한 칸 건너뜀 — 2024-41 본문 I·II·IV·V·VI, 목차 Ⅰ~Ⅴ)
    도 같은 조건(목차 expected 항목 제목 일치)으로 expected로 본다 → "기대 번호 ±1 + 제목 일치". 그 밖의 번호(장 안의
    `1.` 항목 등)는 승격하지 않는다. 제목 일치는 정상 채택(번호 = 기대)의 조건이 아니다 — 적재 64권 실측에서 채택 장
    329개 중 46개가 표기 차이로 매칭에 실패(IMPLEMENTATION_NOTES §3 단계①)."""
    if not toc or expected not in toc or cand_val not in (expected - 1, expected + 1):
        return False
    a, b = chapter_title_key(prev_text), chapter_title_key(cand_text)
    if not a or not b or a == b:
        return False
    return _toc_title_match(b, toc[expected])


def md_chapter_values(texts: list[str], profile_name: str, toc: dict[int, list[str]] | None) -> list[int | None]:
    """.md 깊이 1 헤딩 텍스트 열 → 유효 장 번호 열(표면 번호에 R4 승격을 같은 규칙으로 적용). 번호 없는
    헤딩은 None. toc = toc_chapter_titles 결과. verify C가 목차 번호 집합과 대조할 때 사용."""
    md_pats = list(relaxed_profile_pats(profile_name))
    if profile_name == "bare_digit_split":
        md_pats.append(("bare", BARE_MD_RE))
    out: list[int | None] = []
    prev_text, prev_val = "", None
    for text in texts:
        v = None
        for name, pat in md_pats:
            m = pat.match(text)
            if m:
                try:
                    v = int(m.group(1)) if name == "bare" else profile_value(name, m)
                except (ValueError, IndexError):
                    v = None
                break
        if v is not None and prev_val is not None and typo_promotable(prev_text, text, v, prev_val + 1, toc):
            v = prev_val + 1
        out.append(v)
        if v is not None:
            prev_text, prev_val = text, v
    return out


def find_body_start_flat(scans, part) -> int | None:
    """플랫 폴백용 본문 시작 — 파트 앞의 공백/이미지 전용/앞부속 연속 구간 다음 첫 페이지.

    L1 후보 신호를 요구하지 않는다(장 번호가 없는 문서 대상). extract 폴백과
    verify.py가 이 함수를 공유해 재현 판정이 어긋날 수 없다(table_covers 전례).
    """
    for i in range(part[0], part[1] + 1):
        s = scans[i]
        if s.blank or s.image_only or page_is_front(s):
            continue
        return i
    return None


@dataclasses.dataclass
class Heading:
    addr: int                     # body_lines 인덱스
    depth: int                    # 1=장
    hid: str                      # 서수 경로 ID
    text: str
    kind: str = "normal"          # normal | references | appendix
    family: str = ""
    value: object = None
    extra_addrs: list = dataclasses.field(default_factory=list)  # 두줄형 제목·랩 연속줄


def is_colophon_page(scan: PageScan) -> bool:
    folded = fold(" ".join(l.text for l in scan.lines))
    if any(h in folded for h in COLOPHON_HINTS):
        return True
    return bool(scan.lines) and fold(scan.lines[0].text) == "주의"


def mark_colophon_pages(scans, part, warnings: list) -> None:
    """파트 끝 3페이지 이내의 안내문/판권/주의 페이지를 통째로 드롭 표시 (구조 감지 전 선행)."""
    for scan in scans[max(part[0], part[1] - 2):part[1] + 1]:
        if not scan.blank and not scan.image_only and is_colophon_page(scan):
            scan.dropped = True
            warnings.append(f"p.{scan.index + 1} 안내문/판권 페이지 삭제")


def collect_body_lines(scans, part, body_start):
    """본문 구간의 (페이지, 줄) 평탄화 목록. 각주·리더·표내부 줄 포함(플래그로 구분), 드롭 페이지 제외."""
    out = []
    for scan in scans[body_start:part[1] + 1]:
        if scan.dropped:
            continue
        for j, ln in enumerate(scan.lines):
            out.append((scan.index, j, ln))
    return out


def _static_guard(ln: Line, rest_len: int) -> str | None:
    if ln.in_table:
        return "표 내부"
    if ln.is_leader:
        return "리더런"
    if ln.is_footnote:
        return "각주"
    if rest_len > MAX_HEADING_LEN:
        return f"제목 {rest_len}자 초과"
    return None


def divider_list_addrs(body_lines, head_addr: int, start: int, pat, two_line: bool) -> list[int]:
    """장 구분 페이지의 절 목록(R8, 2026-09-16 — 2024-29·2024-33 실측): 방금 채택한 장 헤딩(head_addr)과 같은
    페이지의 나머지 줄(start 이후)이 전부 절 제목·뒷부속 표제 꼴이고 페이지가 짧으면 그 줄들은 목차이지 헤딩이
    아니다 — 종전에는 목록 줄이 빈 절 헤딩이 되고 본문 쪽의 같은 절 제목은 순번 불일치로 기각되어 장 본문이
    마지막 절 아래로 몰렸고, 목록 끝의 '참고 문헌' 줄이 참고문헌 장을 열어 결론 본문이 요약 제외됐다.
    기준: 페이지 비어있지 않은 줄 ≤ DIVIDER_MAX_LINES, 전부 ≤ MAX_TITLE_LINE, 표·각주·리더 줄 없음, 목록 줄 ≥ 2,
    각 목록 줄이 하위 패밀리·참고문헌·부록·L1 패턴 중 하나에 매치(분리형 L1은 다음 제목 줄과 한 단위, 매치 줄
    바로 뒤 ≤30자 비마커·비불릿 줄 1개는 랩 이어짐으로 허용). 반환 = 헤딩 후보에서 제외할 addr 목록(빈 목록 =
    구분 페이지 아님). 줄 자체는 build_body가 본문 텍스트로 남긴다(verify 커버리지 검사와 1:1 유지)."""
    pg = body_lines[head_addr][0]
    lo = head_addr
    while lo > 0 and body_lines[lo - 1][0] == pg:
        lo -= 1
    hi = start
    while hi < len(body_lines) and body_lines[hi][0] == pg:
        hi += 1
    page = [k for k in range(lo, hi) if body_lines[k][2].text.strip()]
    if len(page) > DIVIDER_MAX_LINES:
        return []
    for k in page:
        ln = body_lines[k][2]
        if ln.in_table or ln.is_footnote or ln.is_leader or len(ln.text.strip()) > MAX_TITLE_LINE:
            return []
    rest = [k for k in page if k >= start]
    if len(rest) < 2:
        return []
    prev_marker = False
    idx = 0
    while idx < len(rest):
        t = body_lines[rest[idx]][2].text.strip()
        m = pat.match(t)
        if m or _match_sub_any(t) or REF_TITLE_RE.match(t) or APPENDIX_RE.match(t):
            if m and two_line:      # 분리형 L1(번호 단독 줄) + 다음 제목 줄 = 한 단위
                idx += 1
            prev_marker = True
        elif prev_marker and len(t) <= 30 and not BULLET_START_RE.match(t) \
                and not any(p.match(t) for _, p, _ in PROFILES):
            prev_marker = False     # 직전 마커 줄의 랩 이어짐 1줄
        else:
            return []
        idx += 1
    return rest


def _two_line_title_addr(body_lines, i: int, consumed, skip_addrs) -> int | None:
    """분리형 L1(번호 단독 줄 i)의 제목 줄 addr. 기본은 다음 줄(i+1). **R9(c, 2023-07)**: 다음 줄이 하위 헤딩 꼴
    (`제1절연구의…`)이고 같은 쪽 바로 앞 줄(i-1)이 짧은 비마커·비표·비각주 제목 줄(` 서론`)이면 앞 줄 — 제목이 번호
    상자 위에 놓인 판형. 둘 다 부적합이면 None(종전 '두줄형 제목 줄 부적합')."""
    pg = body_lines[i][0]
    nxt = body_lines[i + 1][2] if i + 1 < len(body_lines) else None
    nt = nxt.text.strip() if nxt is not None else ""
    next_ok = bool(nt) and len(nt) <= MAX_TITLE_LINE and not FOOTER_RE.match(nt) \
        and not LEADER_RE.search(nt) and not nxt.in_table
    if next_ok and _match_sub_any(nt) and i - 1 >= 0:
        ppg, _, prev = body_lines[i - 1]
        pt = prev.text.strip()
        if ppg == pg and (i - 1) not in consumed and (i - 1) not in skip_addrs and pt \
                and len(pt) <= MAX_TITLE_LINE and not prev.in_table and not prev.is_leader \
                and not prev.is_footnote and not FOOTER_RE.match(pt) and not LEADER_RE.search(pt) \
                and not any(p.match(pt) for _, p, _ in PROFILES) and not _match_sub_any(pt) \
                and not REF_TITLE_RE.match(pt) and not APPENDIX_RE.match(pt):
            return i - 1
    return i + 1 if next_ok else None


def _l1_title_text(body_lines, i: int, two_line: bool, consumed, skip_addrs) -> str:
    """L1 후보 i의 제목 비교용 텍스트(R9 b) — 분리형은 제목 줄을 붙이고, 단줄형은 그대로(랩 결합은 목차 접기의
    4자 이상 포함 일치로 충분)."""
    text = body_lines[i][2].text.strip()
    if two_line:
        t = _two_line_title_addr(body_lines, i, consumed, skip_addrs)
        return f"{text} {body_lines[t][2].text.strip()}" if t is not None else text
    return text


def _next_same_value_cand(body_lines, start: int, pat, name: str, value: int, two_line: bool,
                          consumed, skip_addrs) -> int | None:
    """start 이후 **처음 나오는** 같은 프로파일 패턴·같은 번호·정적 가드 통과·미소비 L1 후보의 addr(없으면 None).
    R9 b는 이 첫 후보만 본다 — 목차가 `N.` 항목을 여러 층위에 겹쳐 쓰는 판형(2024-04, 같은 번호 후보 61줄)에서
    "뒤 어딘가의 일치 후보"는 엉뚱한 줄을 장으로 올리기 때문."""
    for k in range(start, len(body_lines)):
        if k in consumed or k in skip_addrs:
            continue
        ln = body_lines[k][2]
        t = ln.text.strip()
        if not t:
            continue
        m = pat.match(t)
        if not m:
            continue
        try:
            v = profile_value(name, m)
        except (ValueError, IndexError):
            continue
        if v != value or _static_guard(ln, 0 if two_line else len(t[m.end() - 1:])):
            continue
        return k
    return None


def walk_l1(body_lines, profile, report_id: str, diag: dict | None = None,
            no_footer_pages: set | None = None, toc: dict | None = None,
            warnings: list | None = None):
    """선택 프로파일로 장 시퀀스 + 뒷부속 감지. (chapters, 소비된 addr 집합, 구분 페이지 제외 addr 집합) 반환.
    toc = 앞부속 목차 {장 번호: [제목 접기]}(toc_chapter_titles — R4 장 번호 오타 승격 조건, None이면 승격 없음),
    warnings = 승격 기록 대상(PartResult.warnings)."""
    name, pat, two_line = profile
    no_footer_pages = no_footer_pages or set()
    chapters: list[Heading] = []
    consumed: set[int] = set()
    skip_addrs: set[int] = set()   # R8 장 구분 페이지 절 목록 — 헤딩 후보 제외(본문 텍스트로는 유지)
    expected = 1
    backmatter = False
    ref_seen = False

    def log_reject(i, text, reason):
        if diag is not None:
            diag.setdefault("l1_rejects", []).append(
                {"page": body_lines[i][0] + 1, "text": text[:40], "reason": reason})

    i = 0
    while i < len(body_lines):
        pg, j, ln = body_lines[i]
        text = ln.text.strip()
        if not text or i in consumed or i in skip_addrs:
            i += 1
            continue

        # 뒷부속 마커 (장 2개 이상 확보 후부터)
        if len([c for c in chapters if c.kind == "normal"]) >= 2 and not ln.in_table \
                and not ln.is_leader and not ln.is_footnote and len(text) <= MAX_HEADING_LEN + 10:
            if REF_TITLE_RE.match(text):
                chapters.append(Heading(addr=i, depth=1, hid="", text=re.sub(r"\s+", " ", text),
                                        kind="references", family="backmatter"))
                consumed.add(i)
                backmatter = True
                ref_seen = True
                i += 1
                continue
            am = APPENDIX_RE.match(text)
            if am is None and pg in no_footer_pages:
                # 맨몸 '첨부'는 꼬리말 없는 뒷부속 페이지에서만 (2025-16 p34 vs 2025-32 p49 실측)
                am = ATTACH_RE.match(text)
            if am and not (backmatter and chapters and chapters[-1].kind == "appendix"):
                chapters.append(Heading(addr=i, depth=1, hid="", text=re.sub(r"\s+", " ", text),
                                        kind="appendix", family="backmatter"))
                consumed.add(i)
                backmatter = True
                i += 1
                continue
            # 참고문헌 이후의 무표기 부록(2025-17): 단독 숫자 1 + 제목 줄
            if ref_seen and chapters[-1].kind == "references" and re.match(r"^1$", text):
                nxt = body_lines[i + 1][2] if i + 1 < len(body_lines) else None
                if nxt is not None and nxt.text.strip() and len(nxt.text.strip()) <= MAX_TITLE_LINE:
                    title = re.sub(r"\s+", " ", nxt.text.strip())
                    chapters.append(Heading(addr=i, depth=1, hid="", text="부록",
                                            kind="appendix", family="backmatter",
                                            extra_addrs=[]))
                    consumed.add(i)
                    backmatter = True
                    i += 1
                    continue

        if backmatter:
            i += 1
            continue

        m = pat.match(text)
        if m:
            try:
                val = profile_value(name, m)
            except (ValueError, IndexError):
                i += 1
                continue
            guard = _static_guard(ln, 0 if two_line else len(text[m.end() - 1:]))
            if guard:
                log_reject(i, text, guard)
                i += 1
                continue
            # R4 승격 예비 판정(제목 비교는 제목 줄을 합친 뒤 typo_promotable에서)
            promote = val != expected and toc is not None and val in (expected - 1, expected + 1) \
                and expected in toc and any(c.kind == "normal" for c in chapters)
            if val != expected and not promote:
                log_reject(i, text, f"순번 불일치(기대 {expected}, 실제 {val})")
                i += 1
                continue
            # R9 b(2026-09-18, 2023-05 실측): 번호는 기대와 같은데 제목이 목차와 다르고, 뒤에서 **처음 나오는** 같은 번호
            # 후보의 제목이 목차와 일치하면 현재 후보는 장 안의 항목(`4. 사업 추진 계획(안)`)으로 보고 기각한다.
            # 목차 표기 차이만으로는(다음 후보가 없거나 그 후보도 불일치) 아무것도 바뀌지 않는다.
            if val == expected and toc and expected in toc \
                    and not _toc_title_match(chapter_title_key(
                        _l1_title_text(body_lines, i, two_line, consumed, skip_addrs)), toc[expected]):
                k = _next_same_value_cand(body_lines, i + 1, pat, name, expected, two_line, consumed, skip_addrs)
                if k is not None and _toc_title_match(chapter_title_key(
                        _l1_title_text(body_lines, k, two_line, consumed, skip_addrs)), toc[expected]):
                    log_reject(i, text, f"목차 제목 불일치(다음 같은 번호 후보 p.{body_lines[k][0] + 1} 일치)")
                    i += 1
                    continue
            title_text = text
            extra = []
            if two_line:
                t_addr = _two_line_title_addr(body_lines, i, consumed, skip_addrs)
                if t_addr is None:
                    log_reject(i, text, "두줄형 제목 줄 부적합")
                    i += 1
                    continue
                title_text = f"{text} {body_lines[t_addr][2].text.strip()}"
                extra = [t_addr]
                if t_addr < i and diag is not None:
                    diag.setdefault("l1_two_line_prev", []).append({"page": pg + 1, "text": title_text[:40]})
            else:
                # 헤딩 랩 결합: 줄이 컬럼 우측 끝까지 차고 다음 줄이 짧은 비마커 줄이면 이어붙임
                if i + 1 < len(body_lines):
                    nxt_pg, _, nxt = body_lines[i + 1]
                    if nxt_pg == pg and ln.x1 > 0 and nxt.text.strip() \
                            and len(nxt.text.strip()) <= 30 and not nxt.in_table \
                            and not any(p.match(nxt.text.strip()) for _, p, _ in PROFILES) \
                            and not _match_sub_any(nxt.text.strip()) \
                            and not REF_TITLE_RE.match(nxt.text.strip()) \
                            and not APPENDIX_RE.match(nxt.text.strip()) \
                            and _is_full_width(ln, body_lines, pg):
                        title_text = f"{text} {nxt.text.strip()}"
                        extra = [i + 1]
            if promote:
                prev = [c for c in chapters if c.kind == "normal"][-1]
                clean = re.sub(r"\s+", " ", title_text)
                if not typo_promotable(prev.text, clean, val, expected, toc):
                    log_reject(i, text, f"순번 불일치(기대 {expected}, 실제 {val})")
                    i += 1
                    continue
                kind = "skip" if val > expected else "typo"
                label = "장 번호 건너뜀 보정" if kind == "skip" else "장 번호 오타 승격"
                if warnings is not None:
                    warnings.append(f"p.{pg + 1} {label} {val}→{expected}: {clean[:30]}")
                if diag is not None:
                    diag.setdefault("l1_promoted", []).append(
                        {"page": pg + 1, "text": clean[:40], "from": val, "to": expected, "kind": kind})
                val = expected
            head_addr = i
            if extra and extra[0] < i:      # R9 c: 제목 줄이 번호 줄 앞 — 헤딩 단위의 첫 줄을 addr로, 번호 줄은 extra
                head_addr, extra = extra[0], [i]
            h = Heading(addr=head_addr, depth=1, hid="", text=re.sub(r"\s+", " ", title_text),
                        family=name, value=val, extra_addrs=extra)
            # 장 제목이 참고문헌이면 references 장으로 (2025-02 "6. 참고문헌")
            if fold(title_text).endswith("참고문헌"):
                h.kind = "references"
                ref_seen = True
                backmatter = True
            chapters.append(h)
            consumed.add(h.addr)
            consumed.update(extra)
            expected += 1
            i = max([h.addr, *extra]) + 1
            if h.kind == "normal":
                rest = divider_list_addrs(body_lines, h.addr, i, pat, two_line)
                if rest:
                    skip_addrs.update(rest)
                    i = rest[-1] + 1
                    if diag is not None:
                        diag.setdefault("divider_pages", []).append(pg + 1)
                    if warnings is not None:
                        warnings.append(f"p.{pg + 1} 장 구분 페이지 절 목록 {len(rest)}줄 헤딩 제외")
            continue
        i += 1
    return chapters, consumed, skip_addrs


def _match_sub_any(text: str):
    for fam, pat in SUB_FAMILY_DEFS:
        m = pat.match(text)
        if m:
            return fam, m
    return None


def _is_full_width(ln: Line, body_lines, pg: int) -> bool:
    xs = [l.x1 for p, _, l in body_lines if p == pg and not l.in_table]
    if not xs:
        return False
    return ln.x1 >= 0.92 * max(xs)


def _sub_value(fam: str, m: re.Match):
    if fam == "jeol":
        return int(m.group(1))
    if fam == "num_dot_num":
        return int(m.group(2))
    if fam in ("num_dot", "paren_num", "num_paren"):
        return int(m.group(1))
    if fam in ("ga_dot", "ga_paren"):
        ch = m.group(1)
        if ch not in HANGUL_ORD:
            return None
        return HANGUL_ORD.index(ch) + 1
    return None


def detect_sub_headings(body_lines, chapters, profile_name, report_id, diag=None, skip_addrs=None):
    """정규 장 내부에서 하위 패밀리 발견(전역 첫 등장 순서) + 부모 범위 단조증가 검증.
    skip_addrs = walk_l1이 장 구분 페이지 절 목록으로 제외한 addr(R8) — 두 패스 모두 건너뛴다."""
    skip_addrs = skip_addrs or set()
    normal_spans = []
    for ci, ch in enumerate(chapters):
        if ch.kind != "normal":
            continue
        start = ch.addr + 1 + len(ch.extra_addrs)
        nxt = chapters[ci + 1].addr if ci + 1 < len(chapters) else len(body_lines)
        normal_spans.append((ci, start, nxt))

    banned = set()
    if profile_name == "arabic":
        banned.add("num_dot")          # L1과 동일 패턴 — 모호성 배제
    if profile_name == "bare_digit_split":
        return [], {}, {}              # 하위 무표기(실측) — 평탄 유지

    # 1) 패밀리 발견: 값==1(첫 항)로 시작하는 유효 후보의 전역 첫 등장 순서.
    #    두줄형 한글(2025-12 단독 '가'+제목 줄)도 같은 패스에서 위치 순서로 경쟁시킨다
    #    — 별도 후행 패스로 돌리면 각주 유래 num_paren 등이 depth 슬롯을 선점한다(실측).
    family_order = []
    for ci, start, end in normal_spans:
        for i in range(start, end):
            if i in skip_addrs:
                continue
            pg, j, ln = body_lines[i]
            text = ln.text.strip()
            hit = _match_sub_any(text)
            if hit:
                fam, m = hit
                if fam in banned or fam in family_order:
                    continue
                if _sub_value(fam, m) != 1:
                    continue
                if _static_guard(ln, len(text[m.end() - 1:])):
                    continue
                family_order.append(fam)
                continue
            if TWO_LINE_HANGUL not in family_order and text == "가" \
                    and not ln.in_table and not ln.is_leader and i + 1 < end:
                nxt = body_lines[i + 1][2]
                nt = nxt.text.strip()
                if nt and len(nt) <= MAX_TITLE_LINE and not _match_sub_any(nt) \
                        and not FOOTER_RE.match(nt) and not LEADER_RE.search(nt):
                    family_order.append(TWO_LINE_HANGUL)

    depth_of = {fam: idx + 2 for idx, fam in enumerate(family_order) if idx < 3}
    if diag is not None:
        diag["family_order"] = family_order
        diag["family_depths"] = depth_of

    # 2) 구조 워크: 부모 범위 내 1부터 +1 단조 검증
    headings: list[Heading] = []
    counters: dict = {}
    stats = {fam: {"accepted": 0, "rejected": 0} for fam in depth_of}

    def log_reject(fam, i, text, reason):
        stats[fam]["rejected"] += 1
        if diag is not None:
            lst = diag.setdefault("sub_rejects", [])
            if len(lst) < 200:
                lst.append({"family": fam, "page": body_lines[i][0] + 1,
                            "text": text[:40], "reason": reason})

    fam_sizes: dict = {}            # 패밀리 → 채택 헤딩 폰트 크기 목록 (무점 보정 게이트)

    for ci, start, end in normal_spans:
        chapter = chapters[ci]
        stack: list[Heading] = []   # 현재 활성 하위 헤딩(얕은→깊은)
        child_count: dict = {}      # 부모 hid → 자식 수 (서수 경로)
        i = start
        while i < end:
            if i in skip_addrs:
                i += 1
                continue
            pg, j, ln = body_lines[i]
            text = ln.text.strip()
            consumed_extra = []
            fam = None
            val = None
            title_text = text
            is_repair = False
            hit = _match_sub_any(text)
            if hit and hit[0] in depth_of:
                fam, m = hit
                val = _sub_value(fam, m)
                if val is None:
                    i += 1
                    continue
                guard = _static_guard(ln, len(text[m.end() - 1:]))
                if guard:
                    log_reject(fam, i, text, guard)
                    i += 1
                    continue
            elif TWO_LINE_HANGUL in depth_of and len(text) == 1 and text in HANGUL_ORD \
                    and not ln.in_table and not ln.is_leader and i + 1 < end:
                nxt = body_lines[i + 1][2]
                nt = nxt.text.strip()
                if nt and len(nt) <= MAX_TITLE_LINE and not _match_sub_any(nt) \
                        and not FOOTER_RE.match(nt) and not LEADER_RE.search(nt) \
                        and not nxt.in_table:
                    fam = TWO_LINE_HANGUL
                    val = HANGUL_ORD.index(text) + 1
                    title_text = f"{text} {nt}"
                    consumed_extra = [i + 1]
            elif "num_dot" in depth_of and fam_sizes.get("num_dot"):
                # 무점 보정(2025-25 "2 법·제도 개선 방향" 실측): 마침표 탈락 헤딩을
                # 기대 순번 정확 일치 + 채택 헤딩 폰트 중위값 ±0.3에서만 구제
                dm = re.match(r"^(\d{1,2})\s+[가-힣A-Za-z(]", text)
                if dm and not _static_guard(ln, len(text[dm.end() - 1:])):
                    sizes = sorted(fam_sizes["num_dot"])
                    if abs(ln.size - sizes[len(sizes) // 2]) <= 0.3:
                        fam = "num_dot"
                        val = int(dm.group(1))
                        is_repair = True
            if fam is None:
                i += 1
                continue

            depth = depth_of[fam]
            # 부모 = 현재 스택에서 depth보다 얕은 가장 깊은 헤딩(없으면 장).
            # 기각될 수도 있으므로 스택은 채택 시에만 갱신(비파괴 계산).
            keep = [h for h in stack if h.depth < depth]
            parent = keep[-1] if keep else chapter
            key = (fam, parent.hid)
            expected = counters.get(key, 0) + 1
            if val != expected:
                if not is_repair:
                    log_reject(fam, i, text, f"순번 불일치(부모 {parent.hid}, 기대 {expected}, 실제 {val})")
                i += 1
                continue
            counters[key] = val
            k = child_count.get(parent.hid, 0) + 1
            child_count[parent.hid] = k
            h = Heading(addr=i, depth=depth, hid=f"{parent.hid}s{k}",
                        text=re.sub(r"\s+", " ", title_text), family=fam, value=val,
                        extra_addrs=consumed_extra)
            headings.append(h)
            stack = keep + [h]
            stats[fam]["accepted"] += 1
            fam_sizes.setdefault(fam, []).append(ln.size)
            if is_repair and diag is not None:
                diag.setdefault("sub_repairs", []).append(
                    {"family": fam, "page": pg + 1, "text": text[:40]})
            i += 1 + len(consumed_extra)
    return headings, depth_of, stats


def detect_structure(scans, part, body_start, cand_profiles, report_id, diag=None, warnings=None):
    """L1 프로파일 확정(본문 시작 페이지의 1장 후보와 결합) → 장 + 하위 헤딩.
    warnings = R4 장 번호 오타 승격 기록 대상(verify 재현 호출은 None)."""
    body_lines = collect_body_lines(scans, part, body_start)
    no_footer = {s.index for s in scans[body_start:part[1] + 1] if s.footer_arabic is None}
    ordered = [p for p in PROFILES if p[0] in cand_profiles]
    chosen = None
    chapters = consumed = None
    attempts = {}
    for profile in ordered:
        chs, cons, _ = walk_l1(body_lines, profile, report_id, no_footer_pages=no_footer,
                            toc=toc_chapter_titles(scans, part, body_start, profile[0]))
        attempts[profile[0]] = len([c for c in chs if c.kind == "normal"])
        if attempts[profile[0]] >= 2:
            chosen, chapters, consumed = profile, chs, cons
            break
    # 로마자 혼용 우선(R1, 2026-09-09): 순수 로마 프로파일이 장 2개 이상을 잡아 먼저 채택돼도, 혼용 판형
    # (2024-07: I·II ASCII + Ⅲ 유니코드 + IV·V)은 뒤 장을 놓친다 — roman_mixed가 더 많은 장을 잡으면 그쪽.
    # 순수 로마 문서는 두 프로파일의 장 수가 같아 무영향.
    if chosen is not None and chosen[0] in ("roman_unicode", "roman_ascii") and "roman_mixed" in cand_profiles:
        mixed = next(p for p in PROFILES if p[0] == "roman_mixed")
        chs, cons, _ = walk_l1(body_lines, mixed, report_id, no_footer_pages=no_footer,
                            toc=toc_chapter_titles(scans, part, body_start, "roman_mixed"))
        attempts["roman_mixed"] = len([c for c in chs if c.kind == "normal"])
        if attempts["roman_mixed"] > len([c for c in chapters if c.kind == "normal"]):
            chosen, chapters, consumed = mixed, chs, cons
    if diag is not None:
        diag["l1_attempts"] = attempts  # 실패(플랫 폴백) 시에도 후보별 정상 장 수를 남긴다
        primary, secondary = toc_pages(scans, part, body_start)  # R6 육안 확인용(1-based 쪽)
        diag["toc_pages"] = {"primary": [i + 1 for i in primary], "secondary": [i + 1 for i in secondary]}
    if chosen is None:
        return None
    # 확정 워크 (진단 수집 포함)
    chapters, consumed, skip_addrs = walk_l1(body_lines, chosen, report_id, diag=diag,
                                 no_footer_pages=no_footer,
                                 toc=toc_chapter_titles(scans, part, body_start, chosen[0]),
                                 warnings=warnings)
    for idx, ch in enumerate(chapters):
        ch.hid = f"{report_id}_c{idx + 1}"
    subs, depth_of, stats = detect_sub_headings(body_lines, chapters, chosen[0], report_id, diag=diag,
                                                skip_addrs=skip_addrs)
    if diag is not None:
        diag["profile"] = chosen[0]
        diag["chapters"] = [{"page": body_lines[c.addr][0] + 1, "kind": c.kind,
                             "id": c.hid, "text": c.text} for c in chapters]
        diag["sub_stats"] = stats
    return {"profile": chosen[0], "body_lines": body_lines, "chapters": chapters,
            "sub_headings": subs, "family_depths": depth_of, "sub_stats": stats}


def collect_l1_suspects(scans, part, start, limit=30):
    """감지 실패 문서의 헤딩 의심 줄 샘플 — 프로파일 승격 판단 재료(진단 전용).

    본문 범위에서 번호 토큰류 줄머리의 짧은 줄을 페이지와 함께 수집한다.
    표 내부·리더런·각주 줄 제외, 동일 텍스트는 첫 출현만, 최대 limit개.
    """
    out, seen = [], set()
    for pg, _j, ln in collect_body_lines(scans, part, start):
        t = ln.text.strip()
        if not t or len(t) > MAX_TITLE_LINE:
            continue
        if ln.in_table or ln.is_leader or ln.is_footnote:
            continue
        if not L1_SUSPECT_RE.match(t) or t in seen:
            continue
        seen.add(t)
        out.append(f"p.{pg + 1}: {t}")
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# 본문 조립
# ---------------------------------------------------------------------------

def bullet_depth_map(body_lines) -> dict:
    """보고서 내 관측 랭크 → 연속 depth 압축."""
    seen = set()
    for pg, j, ln in body_lines:
        if ln.in_table or ln.is_leader or ln.is_footnote:
            continue
        ch = ln.text.strip()[:1]
        if ch in GLYPH_BULLETS:
            seen.add(BULLET_RANK[ch])
        elif ch in DASH_BULLETS and re.match(r"^[-–]\s+\S", ln.text.strip()):
            seen.add(BULLET_RANK[ch])
    ranks = sorted(seen)
    return {r: i + 1 for i, r in enumerate(ranks)}


def classify_bullet(text: str) -> tuple[int, str] | None:
    """(랭크, 내용) 또는 None. ※·*·번호 열거는 불릿으로 치지 않음."""
    t = text.strip()
    if not t:
        return None
    ch = t[0]
    if ch in GLYPH_BULLETS:
        return BULLET_RANK[ch], t[1:].lstrip()
    if ch in DASH_BULLETS and re.match(r"^[-–]\s+\S", t):
        return BULLET_RANK[ch], t[1:].lstrip()
    return None


@dataclasses.dataclass
class PartResult:
    report_id: str
    part_index: int
    n_parts: int
    part_range: tuple
    meta: Meta = None
    body_start: int | None = None
    structure: dict | None = None
    markdown: str | None = None
    status: str = "ok"
    flat: bool = False  # 플랫 청킹 폴백 산출물 (frontmatter 'structure: flat'과 동기)
    warnings: list = dataclasses.field(default_factory=list)
    stats: dict = dataclasses.field(default_factory=dict)
    unknown_glyphs: dict = dataclasses.field(default_factory=dict)
    image_only_pages: list = dataclasses.field(default_factory=list)


def build_body(scans, part, body_start, structure, result: PartResult,
               page_marks: list | None = None) -> list[str]:
    body_lines = structure["body_lines"]
    heading_at = {}
    consumed = set()
    for ch in structure["chapters"]:
        heading_at[ch.addr] = ch
        consumed.update(ch.extra_addrs)
    for h in structure["sub_headings"]:
        heading_at[h.addr] = h
        consumed.update(h.extra_addrs)

    addr_index = {}
    for idx, (pg, j, ln) in enumerate(body_lines):
        addr_index[(pg, j)] = idx

    bmap = bullet_depth_map(body_lines)

    out: list[str] = []
    para: list[str] = []
    para_prefix = ""
    fn_buffer: list[str] = []
    pending_bullet_rank: int | None = None

    def flush_para():
        nonlocal para, para_prefix
        if para:
            joined = join_fragments(para)
            if joined:
                out.append(para_prefix + joined)
                out.append("")
        para, para_prefix = [], ""
        if fn_buffer:
            for fn in fn_buffer:
                out.append(fn)
                out.append("")
            fn_buffer.clear()

    # 이미지 전용 페이지 병합 구간
    img_pages = [s.index for s in scans[body_start:part[1] + 1] if s.image_only]
    result.image_only_pages = [p + 1 for p in img_pages]
    img_set = set(img_pages)
    emitted_img_ranges = set()

    def img_marker(pg):
        # 연속 구간의 시작 페이지에서만 마커 방출
        if pg - 1 in img_set:
            return None
        end = pg
        while end + 1 in img_set:
            end += 1
        rng = f"p.{pg + 1}" if end == pg else f"p.{pg + 1}-{end + 1}"
        if rng in emitted_img_ranges:
            return None
        emitted_img_ranges.add(rng)
        return f"<!-- {rng}: 이미지 전용 페이지, 텍스트 추출 불가 -->"

    n_masked = 0
    n_recovered = 0
    n_tables = 0
    last_pg = None

    for scan in scans[body_start:part[1] + 1]:
        pg = scan.index
        if scan.blank or scan.dropped:
            continue
        if page_marks is not None:
            page_marks.append((len(out), pg + 1))  # 이 페이지 내용의 out 시작 인덱스 (플랫 청킹 라벨용)
        if scan.image_only:
            flush_para()
            # 이미지 전용 페이지에도 헤딩 줄은 있을 수 있음(2025-13 <부록 1> 실측) — 헤딩만 방출
            for j, ln in enumerate(scan.lines):
                idx = addr_index.get((pg, j))
                if idx is not None and idx in heading_at:
                    h = heading_at[idx]
                    out.append(f"{'#' * h.depth} {h.text} <!-- id: {h.hid} -->")
                    out.append("")
            mk = img_marker(pg)
            if mk:
                out.append(mk)
                out.append("")
            continue

        # 페이지 항목(줄 + 표)을 y 순서로 인터리브
        items = []
        for j, ln in enumerate(scan.lines):
            items.append((round(ln.y0, 1), 0, "line", j, ln))
        for tab in scan.tables:
            if tab.markdown is not None:
                items.append((round(tab.bbox[1], 1), 1, "table", -1, tab))
        items.sort(key=lambda it: (it[0], it[1]))

        page_right = max((l.x1 for l in scan.lines if not l.in_table), default=0.0)
        modal_h = None
        heights = sorted((l.y1 - l.y0) for l in scan.lines if not l.in_table)
        if heights:
            modal_h = heights[len(heights) // 2]
        page_tf = None  # 마스킹 안전판용 페이지 국소 표 haystack (지연 계산)

        prev_ln: Line | None = None
        for y, order, kind, j, obj in items:
            if kind == "table":
                flush_para()
                n_tables += 1
                if obj.caption:
                    out.append(obj.caption)
                    out.append("")
                out.append(obj.markdown)
                out.append("")
                prev_ln = None
                continue
            ln: Line = obj
            idx = addr_index.get((pg, j))
            text = ln.text.strip()
            if not text:
                continue
            if ln.in_table:
                if page_tf is None:
                    page_tf = page_table_fold(scan.tables)
                if table_covers(text, page_tf):
                    n_masked += 1
                    continue
                # 마스킹 안전판: find_tables 행렬이 놓친 글자(실측 4권 21건 부류)는
                # 지우지 않고 본문으로 방출 — 표 markdown은 그대로, 소실만 방지
                n_recovered += 1
                result.warnings.append(f"p.{pg + 1} 표 미수록 텍스트 복구: {text[:30]}")
            if idx in consumed:
                continue
            if ln.is_leader:
                continue
            folded = fold(text)
            if any(folded.startswith(t) or t in folded for t in TEMPLATE_FOLD):
                result.warnings.append(f"p.{pg + 1} 템플릿 잔재 삭제: {text[:30]}")
                continue
            if ln.is_footnote:
                if re.match(r"^\d{1,3}\)", text):
                    fn_buffer.append(text)
                    continue
                if fn_buffer:
                    fn_buffer[-1] = join_fragments([fn_buffer[-1], text])
                    continue
                # 버퍼 없는 연속줄 오인 — 본문으로 통과(드롭 금지)
            if idx is not None and idx in heading_at:
                flush_para()
                h = heading_at[idx]
                out.append(f"{'#' * h.depth} {h.text} <!-- id: {h.hid} -->")
                out.append("")
                prev_ln = None
                continue

            # 미지 글리프 로그
            for chch in PUA_RE.findall(text):
                result.unknown_glyphs[f"U+{ord(chch):04X}"] = \
                    result.unknown_glyphs.get(f"U+{ord(chch):04X}", 0) + 1
            if text[0] in SUSPECT_BULLETS:
                result.unknown_glyphs[text[0]] = result.unknown_glyphs.get(text[0], 0) + 1

            # 불릿 통일은 참고문헌/부록에도 적용 (헤딩 감지만 비활성 — 결정 7·11)
            bullet = classify_bullet(text)
            # 단독 불릿 글리프 줄 → 다음 줄과 병합 (2025-32 'Ÿ' 단독 줄)
            if text in GLYPH_BULLETS:
                flush_para()
                pending_bullet_rank = BULLET_RANK[text]
                prev_ln = ln
                continue
            if pending_bullet_rank is not None and bullet is None:
                bullet = (pending_bullet_rank, text)
            pending_bullet_rank = None

            if bullet is not None:
                flush_para()
                rank, content = bullet
                depth = bmap.get(rank, len(bmap) + 1 if bmap else 1)
                para_prefix = "  " * (depth - 1) + "- "
                para = [content]
                prev_ln = ln
                continue
            if CAPTION_RE.match(text):
                flush_para()
                out.append(re.sub(r"\s+", " ", text))
                out.append("")
                prev_ln = None
                continue
            if text.startswith(("※", "*")):
                flush_para()
                para = [text]
                prev_ln = ln
                continue

            # 일반 줄: 문단 진행/개시 판단
            if para:
                broke = False
                if prev_ln is not None and prev_ln is not ln:
                    if page_right and prev_ln.x1 < 0.82 * page_right and last_pg == pg:
                        broke = True
                    if modal_h and prev_ln.y1 and pg == last_pg \
                            and (ln.y0 - prev_ln.y1) > 1.8 * modal_h:
                        broke = True
                if broke:
                    flush_para()
                    para = [text]
                else:
                    para.append(text)
            else:
                para = [text]
            prev_ln = ln
            last_pg = pg

        # 페이지 경계: 마지막 줄이 우측 끝까지 차 있으면 문단 지속, 아니면 종료
        if para and prev_ln is not None:
            if page_right and prev_ln.x1 < 0.82 * page_right:
                flush_para()

    flush_para()
    result.stats["tables"] = n_tables
    result.stats["masked_lines"] = n_masked
    result.stats["recovered_lines"] = n_recovered
    # 끝의 빈 줄 정리
    while out and out[-1] == "":
        out.pop()
    return out


# ---------------------------------------------------------------------------
# 플랫 청킹 폴백 (구조 미감지 문서 수용 — 투 레인의 두 번째 레인)
# ---------------------------------------------------------------------------

FLAT_MAX_CHARS = 4000   # mdio.split_units max_chars와 동일 — 청크=유닛 1:1 전제
FLAT_MIN_CHARS = 200    # split_units min_chars — 미만 꼬리 구간은 직전 구간에 병합
FLAT_GUARD_CHARS = 400  # 본문 텍스트가 이 미만이면 폴백 포기(전면 이미지 문서 등 — OCR 별도)


def chunk_flat(out: list[str], page_marks: list, rid: str, warnings: list) -> tuple[list[str], int]:
    """플랫 본문에 합성 구간 헤딩을 삽입 — 문단(빈 줄) 경계 그리디 누적.

    경계는 항상 build_body가 만든 블록(문단·표·캡션·마커) 사이에만 온다.
    크기 산식은 mdio._span_chars와 동일한 len("\\n".join(...).strip())이라 각 구간이
    그대로 유닛 1개가 된다(상한 초과 단일 블록과 꼬리 병합 구간만 예외 — leaf 통짜 유닛).
    헤딩: '# 구간 {k} (p.{a}-{b}) <!-- id: {rid}_c{k} -->' — k는 방출 순번 1..N 연속,
    페이지는 1-based 인용 라벨(페이지에 걸친 문단은 뒤 페이지로 귀속 — ±1 오차 가능).
    """
    blocks: list[tuple[list[str], int]] = []  # (내용 줄들, out 시작 인덱스)
    i = 0
    while i < len(out):
        if out[i] == "":
            i += 1
            continue
        j = i
        while j < len(out) and out[j] != "":
            j += 1
        blocks.append((out[i:j], i))
        i = j

    marks_idx = [m[0] for m in page_marks]
    marks_pg = [m[1] for m in page_marks]

    def page_of(out_idx: int) -> int:
        k = bisect.bisect_right(marks_idx, out_idx) - 1
        return marks_pg[k] if k >= 0 else 1

    def span(lines_: list[str]) -> int:
        return len("\n".join(lines_).strip())

    # 그리디 청크 경계 (블록 인덱스 반개구간)
    chunks: list[tuple[int, int]] = []
    cur_start = 0
    cur_lines: list[str] = []
    for bi, (bl, _) in enumerate(blocks):
        cand = cur_lines + ([""] if cur_lines else []) + bl
        if cur_lines and span(cand) > FLAT_MAX_CHARS:
            chunks.append((cur_start, bi))
            cur_start, cur_lines = bi, list(bl)
        else:
            cur_lines = cand
    if cur_lines:
        chunks.append((cur_start, len(blocks)))

    def chunk_lines(c: tuple[int, int]) -> list[str]:
        s, e = c
        lines_: list[str] = []
        for bl, _ in blocks[s:e]:
            if lines_:
                lines_.append("")
            lines_.extend(bl)
        return lines_

    # 꼬리 구간이 최소 크기 미만이면 직전 구간에 병합 (200자 미만 유닛 스킵 방지)
    if len(chunks) >= 2 and span(chunk_lines(chunks[-1])) < FLAT_MIN_CHARS:
        chunks[-2:] = [(chunks[-2][0], chunks[-1][1])]

    result_lines: list[str] = []
    for k, c in enumerate(chunks, 1):
        s, e = c
        sp = span(chunk_lines(c))
        if sp > FLAT_MAX_CHARS:
            warnings.append(f"플랫 구간 {k} 크기 {sp}자 > {FLAT_MAX_CHARS} — 단일 블록 초과/꼬리 병합")
        a = page_of(blocks[s][1])
        b = page_of(blocks[e - 1][1])
        label = f"p.{a}" if a == b else f"p.{a}-{b}"
        result_lines.append(f"# 구간 {k} ({label}) <!-- id: {rid}_c{k} -->")
        result_lines.append("")
        for bl, _ in blocks[s:e]:
            result_lines.extend(bl)
            result_lines.append("")
    while result_lines and result_lines[-1] == "":
        result_lines.pop()
    return result_lines, len(chunks)


def _reset_footnote_flags(scans, part) -> None:
    """플랫 폴백 직전 초기화 — L1 경로에서 이미 찍힌 각주/드롭 마킹을 걷어내
    verify.py의 재현(플랫 body 범위 기준 마킹만 수행)과 동일 상태에서 다시 마킹한다."""
    for s in scans[part[0]:part[1] + 1]:
        s.dropped = False
        for ln in s.lines:
            ln.is_footnote = False


def flat_fallback(r: PartResult, scans, part, rid: str, path: str, forced: bool = False) -> bool:
    """구조 미감지 파트의 플랫 청킹 폴백. 성공 시 r.markdown까지 채우고 True.

    forced=True는 --lane flat(감지 생략 강제) — 경고 문구만 다르고 로직 동일.
    """
    flat_start = find_body_start_flat(scans, part)
    if flat_start is None:
        return False
    tmp_warn: list[str] = []
    _reset_footnote_flags(scans, part)
    mark_footnotes(scans, (flat_start, part[1]))
    mark_colophon_pages(scans, part, tmp_warn)
    for w in tmp_warn:
        if w not in r.warnings:
            r.warnings.append(w)
    structure = {"body_lines": collect_body_lines(scans, part, flat_start),
                 "chapters": [], "sub_headings": []}
    page_marks: list = []
    body = build_body(scans, part, flat_start, structure, r, page_marks=page_marks)
    real = "\n".join(l for l in body if not l.startswith("<!--")).strip()
    if len(real) < FLAT_GUARD_CHARS:
        r.warnings.append(f"본문 텍스트 {len(real)}자 < {FLAT_GUARD_CHARS} — 플랫 폴백 포기(이미지 위주 문서? OCR 별도)")
        return False
    body, n_chunks = chunk_flat(body, page_marks, rid, r.warnings)
    r.flat = True
    r.body_start = flat_start
    diag = r.stats.setdefault("diag", {})
    diag["body_start_page"] = flat_start + 1
    diag["profile"] = "flat"
    r.markdown = render_markdown(r, f"pdfs/{Path(path).name}", body)
    r.stats["headings"] = n_chunks
    r.stats["chapters"] = n_chunks
    if forced:
        r.warnings.append(f"구조 감지 생략(--lane flat) — 플랫 청킹 적용 (구간 {n_chunks}개)")
    else:
        r.warnings.append(f"장-절 구조 미감지 — 플랫 청킹 폴백 적용 (구간 {n_chunks}개)")
    return True


# ---------------------------------------------------------------------------
# 렌더링
# ---------------------------------------------------------------------------

def yaml_str(s: str) -> str:
    s = (s or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def render_markdown(result: PartResult, source_pdf: str, body: list[str]) -> str:
    meta = result.meta
    rid = result.report_id
    # 연도: 등록부 rid는 표준형 YYYY-NN…이라 앞 4자리. 비표준 수동 rid와 연도 미상 규약
    # `0000-00-vN`(사용자 결정 2026-09-04)은 공란(결측 허용)
    year = rid[:4] if rid[:4].isdigit() and rid[:4] != "0000" else ""
    lines = ["---"]
    lines.append(f"report_id: {rid}")
    lines.append(f"title: {yaml_str(meta.title)}")
    lines.append(f"title_en: {yaml_str(meta.title_en)}")
    lines.append(f"year: {year}")
    lines.append(f"lead_researcher: {yaml_str(meta.lead_researcher)}")
    lines.append(f"institution: {yaml_str(meta.institution)}")
    lines.append(f"keywords_ko: {json.dumps(meta.keywords_ko, ensure_ascii=False)}")
    lines.append(f"keywords_en: {json.dumps(meta.keywords_en, ensure_ascii=False)}")
    if meta.abstract:
        lines.append("abstract: |")
        for al in meta.abstract.split("\n"):
            lines.append(f"  {al}")
    else:
        lines.append('abstract: ""')
    lines.append(f"source_pdf: {yaml_str(source_pdf)}")
    if result.flat:
        lines.append("structure: flat")  # 플랫 청킹 폴백 표식 — verify·status가 이 키로 분기
    if result.n_parts > 1:
        lines.append(f'pdf_pages: "{result.part_range[0] + 1}-{result.part_range[1] + 1}"')
    # 스탬프 1단 — rid는 등록부 조회(rid_for) 성공으로만 얻으므로 등록은 확인된 사실
    lines.append(f"verified_register: {datetime.date.today().isoformat()}")
    lines.append("---")
    lines.append("")
    lines.extend(body)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# 파일 단위 처리
# ---------------------------------------------------------------------------

def process_pdf(path: str, lane: str = "auto"):
    """PDF 1개 → PartResult 목록(합본이면 여러 개). lane: auto|structured|flat (--lane).

    파트 분할은 여기서 하지 않는다(2026-09-08) — 등록부의 파트 행(rid, pages)을 그대로 쓴다.
    register가 경량 스캔으로 detect_parts를 돌려 행을 만들고, verify가 전체 스캔으로 대조한다.
    """
    reg_parts = parts_for(path)
    if not reg_parts:  # 프리플라이트가 막으므로 방어용
        raise ValueError(f"미등록 PDF — {rid_reason(path)}: {Path(path).name}")

    doc = pymupdf.open(path)
    try:
        scans = scan_document(doc)
    finally:
        doc.close()

    parts = [(rid, rng or (0, len(scans) - 1)) for rid, rng in reg_parts]
    results = []
    for pi, (rid, part) in enumerate(parts):
        r = PartResult(report_id=rid, part_index=pi, n_parts=len(parts), part_range=part)
        r.stats["diag"] = {}
        diag = r.stats["diag"]

        meta = parse_abstract(scans, part)
        fallback_institution(scans, part, meta)
        fallback_title(scans, part, meta)
        r.meta = meta
        r.warnings.extend(meta.warnings)

        body_start, cand_profiles, structure = None, [], None
        if lane != "flat":
            body_start, cand_profiles = find_body_start(scans, part)
            if body_start is not None:
                r.body_start = body_start
                diag["body_start_page"] = body_start + 1
                diag["candidate_profiles"] = cand_profiles

                mark_footnotes(scans, (body_start, part[1]))
                mark_colophon_pages(scans, part, r.warnings)

                structure = detect_structure(scans, part, body_start, cand_profiles, rid, diag=diag,
                                             warnings=r.warnings)

        if structure is None:
            if lane != "flat":
                # 감지 실패 — 승격 판단 재료(헤딩 의심 줄 샘플)를 폴백 여부와 무관하게 남긴다
                sus_start = body_start if body_start is not None else find_body_start_flat(scans, part)
                if sus_start is not None:
                    diag["l1_suspects"] = collect_l1_suspects(scans, part, sus_start)
            # 투 레인: 구조 미감지 → 플랫 청킹 폴백(--lane structured는 억제).
            # 폴백조차 불가하면 종전대로 시끄럽게 스킵.
            if lane == "structured":
                r.warnings.append("플랫 폴백 억제(--lane structured) — 승격 검토 대상")
                fell_back = False
            else:
                fell_back = flat_fallback(r, scans, part, rid, path, forced=(lane == "flat"))
            if not fell_back:
                if lane == "flat":
                    if find_body_start_flat(scans, part) is None:
                        r.status = "skipped_no_body_start"
                        r.warnings.append("본문 시작 페이지를 찾지 못함")
                    else:
                        r.status = "skipped_flat_guard"  # 400자 가드 — 경고는 flat_fallback이 기록
                elif body_start is None:
                    r.status = "skipped_no_body_start"
                    r.warnings.append("본문 시작 페이지를 찾지 못함")
                else:
                    r.status = "skipped_no_profile"
                    r.warnings.append(f"L1 프로파일 검증 실패(후보: {cand_profiles})")
                results.append(r)
                continue
        else:
            r.structure = structure
            body = build_body(scans, part, body_start, structure, r)
            r.markdown = render_markdown(r, f"pdfs/{Path(path).name}", body)
            r.stats["headings"] = len(structure["chapters"]) + len(structure["sub_headings"])
            r.stats["chapters"] = len(structure["chapters"])
        r.stats["footer_runs"] = summarize_footers(scans, part)
        results.append(r)
    return results


def summarize_footers(scans, part) -> list[str]:
    """꼬리말 시퀀스를 런 단위 요약 문자열로. 아라비아 값이 +1 연속이 아니면 런을 끊는다(재시작 가시화)."""
    runs = []
    cur = None
    for s in scans[part[0]:part[1] + 1]:
        tag = ("a", s.footer_arabic) if s.footer_arabic is not None else \
              (("r", s.footer) if s.footer else ("none", None))
        breaking = cur is None or cur[0] != tag[0]
        if not breaking and tag[0] == "a":
            gap = s.index - cur[2]
            if tag[1] != (cur[4] or 0) + gap:
                breaking = True
        if breaking:
            if cur is not None:
                runs.append(cur)
            cur = [tag[0], s.index, s.index, tag[1], tag[1]]
        else:
            cur[2] = s.index
            cur[4] = tag[1]
    if cur is not None:
        runs.append(cur)
    out = []
    for kind, p0, p1, v0, v1 in runs:
        if kind == "a":
            out.append(f"p{p0 + 1}-{p1 + 1}: {v0}..{v1}")
        elif kind == "r":
            out.append(f"p{p0 + 1}-{p1 + 1}: roman({v0}..{v1})")
        else:
            out.append(f"p{p0 + 1}-{p1 + 1}: 없음")
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def scan_report(results, path) -> dict:
    """--scan 진단 덤프(파일 1개분)."""
    out = {"file": Path(path).name, "parts": []}
    for r in results:
        diag = r.stats.get("diag", {})
        meta = r.meta
        out["parts"].append({
            "report_id": r.report_id,
            "status": r.status,
            "range": [r.part_range[0] + 1, r.part_range[1] + 1],
            "footer_runs": r.stats.get("footer_runs", []),
            "abstract_page": (meta.abstract_page + 1) if meta and meta.abstract_page is not None else None,
            "meta": {
                "title": (meta.title[:60] if meta else ""),
                "title_en": (meta.title_en[:60] if meta else ""),
                "lead_researcher": meta.lead_researcher if meta else "",
                "institution": meta.institution if meta else "",
                "keywords_ko": meta.keywords_ko if meta else [],
                "keywords_en": meta.keywords_en if meta else [],
                "abstract_chars": len(meta.abstract) if meta else 0,
            },
            "body_start_page": diag.get("body_start_page"),
            "candidate_profiles": diag.get("candidate_profiles"),
            "profile": diag.get("profile"),
            "l1_attempts": diag.get("l1_attempts"),
            "l1_suspects": diag.get("l1_suspects"),
            "toc_pages": diag.get("toc_pages"),
            "divider_pages": diag.get("divider_pages"),
            "chapters": diag.get("chapters"),
            "family_order": diag.get("family_order"),
            "family_depths": diag.get("family_depths"),
            "sub_stats": diag.get("sub_stats"),
            "l1_rejects": diag.get("l1_rejects", [])[:30],
            "sub_rejects": diag.get("sub_rejects", [])[:60],
            "tables": r.stats.get("tables"),
            "masked_lines": r.stats.get("masked_lines"),
            "image_only_pages": r.image_only_pages,
            "unknown_glyphs": r.unknown_glyphs,
            "warnings": r.warnings,
        })
    return out


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="PDF에서 계층 구조 .md를 추출한다.")
    parser.add_argument("pdf", nargs="+", help="변환할 PDF 파일 경로 (pdfs/*.pdf — 자체 글롭 확장)")
    parser.add_argument("-o", "--out-dir", default="reports", help="출력 디렉터리 (기본: reports)")
    parser.add_argument("--log", default="logs/extract_log.json", help="로그 경로 (기본: logs/extract_log.json)")
    parser.add_argument("--scan", action="store_true", help="진단 모드: md 미작성, 감지 결과만 stdout에 덤프")
    parser.add_argument("--force", action="store_true",
                        help="출력 .md가 이미 있어도 재추출·덮어쓰기 (annotate 요약 블록 소실 주의)")
    parser.add_argument("--lane", choices=["auto", "structured", "flat"], default="auto",
                        help="레인 수동 지정: structured=플랫 폴백 억제(감지 실패 시 시끄럽게 스킵), "
                             "flat=구조 감지 생략·곧장 플랫 청킹 (기본 auto=자동 분기)")
    args = parser.parse_args()

    # PowerShell은 글롭을 확장하지 않으므로 자체 확장
    paths = []
    for p in args.pdf:
        # 실존 파일은 글롭 확장하지 않는다 — "[별권] 우수사례집.pdf"처럼 이름에 [가 들어간
        # 파일이 빈 글롭으로 조용히 사라지던 문제(2026-09-04 실측)
        if not Path(p).is_file() and any(c in p for c in "*?["):
            paths.extend(sorted(globmod.glob(p)))
        else:
            paths.append(p)
    if not paths:
        print("입력 PDF가 없습니다.", file=sys.stderr)
        sys.exit(1)

    # 중복·충돌 프리플라이트(2026-09): 배치 전체의 rid를 먼저 유도해 같은 rid로
    # 모이는 파일을 걸러낸다 — 크기까지 동일하면 중복 다운로드 의심(사전순 첫 파일만
    # 진행), 크기가 다르면 진짜 충돌(전원 스킵·파일명 조정 필요). --scan은 진단
    # 모드이므로 경고만 남기고 전부 처리한다.
    # 등록부 게이트(2026-09-04): rid는 report_ids.tsv 조회만 — 미등록은 --scan에서도
    # 스킵한다(rid 없이는 출력 파일명·헤딩 ID를 만들 수 없음). 사람이 표에 기입 후 재실행.
    by_rid: dict[str, list[str]] = {}
    dropped: dict[str, dict] = {}
    for path in paths:
        parts = parts_for(path)
        if not parts:  # 파트 하나라도 공란이면 파일 전체 미등록
            reason = rid_reason(path)
            dropped[path] = {"status": "skipped_unregistered", "reason": reason}
            print(f"[미등록] {Path(path).name}: {reason}", file=sys.stderr)
            continue
        for rid, _rng in parts:
            by_rid.setdefault(rid, []).append(path)
    for rid, group in sorted(by_rid.items()):
        if len(group) < 2:
            continue
        group = sorted(group)
        if len({Path(p).stat().st_size for p in group}) == 1:
            for p in group[1:]:
                dropped[p] = {"status": "skipped_duplicate", "report_id": rid,
                              "duplicate_of": Path(group[0]).name}
            print(f"[중복] {rid}: 동일 크기 파일 {len(group)}개 — "
                  f"'{Path(group[0]).name}'만 진행, 나머지는 삭제 권장", file=sys.stderr)
        else:
            for p in group:
                dropped[p] = {"status": "skipped_id_collision", "report_id": rid}
            print(f"[충돌] {rid}: 서로 다른 파일 {len(group)}개가 같은 report_id로 유도 — "
                  "파일명 조정 필요, 전원 스킵", file=sys.stderr)

    log_entries = []
    scan_dump = []
    all_ok = True

    for path in paths:
        drop = dropped.get(path)
        if drop is not None and (not args.scan or drop["status"] == "skipped_unregistered"):
            all_ok = False
            log_entries.append({"file": Path(path).name, **drop})
            continue

        # 재실행 가드: 기존 출력이 있으면 스캔 비용 없이 스킵 (--scan/--force 제외)
        rids = [rid for rid, _ in parts_for(path)]
        base_id = rids[0]
        if not args.scan and not args.force:
            existing = find_existing_outputs(Path(args.out_dir), rids)
            if existing:
                log_entries.append({"file": Path(path).name, "report_id": base_id,
                                    "status": "skipped_exists",
                                    "existing": [p.name for p in existing]})
                print(f"[skip] {base_id}: 기존 출력 {len(existing)}개 존재 — 재추출은 --force",
                      file=sys.stderr)
                continue

        try:
            results = process_pdf(path, lane=args.lane)
        except Exception:
            all_ok = False
            log_entries.append({"file": Path(path).name, "status": "error",
                                "error": traceback.format_exc(limit=5)})
            print(f"[error] {Path(path).name}", file=sys.stderr)
            continue

        if args.scan:
            scan_dump.append(scan_report(results, path))
        for r in results:
            entry = {
                "file": Path(path).name,
                "report_id": r.report_id,
                "status": r.status,
                "flat": r.flat,
                "part_range": [r.part_range[0] + 1, r.part_range[1] + 1],
                "profile": r.stats.get("diag", {}).get("profile"),
                "family_depths": r.stats.get("diag", {}).get("family_depths"),
                "body_start": r.stats.get("diag", {}).get("body_start_page"),
                "chapters": r.stats.get("chapters"),
                "headings": r.stats.get("headings"),
                "tables": r.stats.get("tables"),
                "masked_lines": r.stats.get("masked_lines"),
                "recovered_lines": r.stats.get("recovered_lines"),
                "image_only_pages": r.image_only_pages,
                "unknown_glyphs": r.unknown_glyphs,
                "l1_attempts": r.stats.get("diag", {}).get("l1_attempts"),
                "l1_suspects": r.stats.get("diag", {}).get("l1_suspects"),
                "toc_pages": r.stats.get("diag", {}).get("toc_pages"),
                "divider_pages": r.stats.get("diag", {}).get("divider_pages"),
                "warnings": r.warnings,
            }
            log_entries.append(entry)
            if r.status != "ok":
                all_ok = False
            if r.status == "ok" and not args.scan:
                out_path = Path(args.out_dir) / f"{r.report_id}.md"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(r.markdown, encoding="utf-8", newline="\n")
            tag = "scan" if args.scan else ("ok" if r.status == "ok" else r.status)
            print(f"[{tag}] {r.report_id}: profile={entry['profile']} "
                  f"chapters={entry['chapters']} headings={entry['headings']} "
                  f"body_start=p{entry['body_start']}", file=sys.stderr)

    if args.scan:
        print(json.dumps(scan_dump, ensure_ascii=False, indent=1))

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps({
        "run_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "mode": "scan" if args.scan else "extract",
        "lane": args.lane,
        "files": log_entries,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    sys.exit(0 if all_ok else 2)


if __name__ == "__main__":
    main()
