"""PDF → 계층 .md 변환 (파이프라인 1단계).

PROJECT_NOTES.md §3 단계 ① 참조. 코퍼스 실측(2026-08, 10권) 기반 설계.

- 입력: pdfs/*.pdf (자체 글롭 확장 — PowerShell 미확장 대응)
- 출력: reports/{report_id}.md (frontmatter + 헤딩 계층 + 본문. LLM 요약 없음)
- 도구: PyMuPDF로만 텍스트 추출 (`import pymupdf` — fitz 별칭은 deprecated). LLM 사용 금지.
- 계층 판별: 북마크 미사용. L1(장) 문법 프로파일 6종 순차 시도 + 하위 패밀리 캐스케이드
  (보고서 전역 첫 등장 순서 → ##~####, 4단계 캡, 부모 범위 내 1부터 단조증가 검증).
- 헤딩 ID: 감지 서수 경로 {report_id}_c{i}s{j}… (PROJECT_NOTES §4)
- 진단: --scan (md 미작성, 감지 결과만 덤프). 로그: logs/extract_log.json
- 프로파일이 잡히지 않는 문서는 스킵 + 로그 후 수동 확인.
- 재실행 가드: 출력 .md가 이미 있으면 기본 스킵, --force로만 재추출·덮어쓰기.
  재추출은 annotate가 삽입한 요약 블록과 frontmatter 검증 스탬프를 소실시킨다
  (스탬프 소실 = 의도된 미검증 리셋). 합본은 파트 일부만 있어도 스킵 — 부분 실패
  재시도는 --force뿐이며 성공 파트의 요약도 함께 소실됨에 주의. --scan은 가드 미적용.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import glob as globmod
import json
import re
import sys
import traceback
import unicodedata
from pathlib import Path

import pymupdf

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

REPORT_ID_RE = re.compile(r"정책연구-(\d{4}-\d{2})")

# L1(장) 프로파일 — (이름, 패턴, 두줄형 여부). 시도 순서 = 이 순서.
PROFILES = [
    ("jang_split", re.compile(r"^제\s+(\d{1,2})\s+장$"), True),
    ("jang", re.compile(r"^제\s*(\d{1,2})\s*장[.\s]+\S"), False),
    ("roman_unicode", re.compile(r"^([Ⅰ-Ⅻ])\.\s*\S"), False),
    ("roman_ascii", re.compile(r"^([IVX]{1,4})\.?\s+\S"), False),
    ("arabic", re.compile(r"^(\d{1,2})\.(?!\s*\d)\s+\S"), False),
    ("bare_digit_split", re.compile(r"^(\d{1,2})$"), True),
]

# 하위 헤딩 패밀리 — 한 줄이 여러 패턴에 걸리면 이 순서의 첫 매치만 인정
SUB_FAMILY_DEFS = [
    ("jeol", re.compile(r"^제\s*(\d{1,2})\s*절[.\s]+\S")),
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


def scan_page(page, index: int) -> PageScan:
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


def scan_document(doc) -> list[PageScan]:
    return [scan_page(page, i) for i, page in enumerate(doc)]


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

def derive_report_id(path: str) -> str | None:
    name = unicodedata.normalize("NFC", Path(path).name)
    m = REPORT_ID_RE.search(name)
    return m.group(1) if m else None


def find_existing_outputs(out_dir: Path, base_id: str) -> list[Path]:
    """단독본({id}.md)과 합본 파트({id}_NN.md) 기존 출력 탐지.

    report_id는 고정폭 7자(\\d{4}-\\d{2}), 파트 접미사는 두 자리 고정이라
    다른 보고서와 접두 충돌이 불가능하다.
    """
    out: list[Path] = []
    single = out_dir / f"{base_id}.md"
    if single.exists():
        out.append(single)
    out.extend(sorted(out_dir.glob(f"{base_id}_[0-9][0-9].md")))
    return out


def split_bundle(scans: list[PageScan]) -> list[tuple[int, int]]:
    """아라비아 꼬리말 런이 1보다 큰 값까지 진행된 뒤, **무꼬리말/로마/목차 페이지를 사이에 두고**
    '- 1 -'로 재시작하면 파트 경계. (앞부속·본문이 연속 아라비아인 판형(2025-02)은 분리 아님 —
    간격 페이지가 있어야 합본으로 본다. 사이 페이지는 새 파트의 앞부속으로 귀속.)
    """
    n = len(scans)
    run_max = 0
    boundaries = [0]
    prev_arabic_page = None

    def frontish(s: PageScan) -> bool:
        return s.blank or s.image_only or s.leader_lines >= 3 or \
            (bool(s.lines) and s.leader_lines / len(s.lines) > 0.3)

    for i, s in enumerate(scans):
        if s.footer_arabic is None:
            continue
        gap = prev_arabic_page is not None and i - prev_arabic_page > 1
        if s.footer_arabic == 1 and run_max > 1 and gap:
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
        prev_arabic_page = i
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
    return int(g)


def line_l1_candidates(ln: Line, next_ln: Line | None) -> list[str]:
    """이 줄에서 1장 후보로 성립하는 프로파일 이름 목록(값=1인 것만)."""
    out = []
    text = ln.text.strip()
    if ln.in_table or ln.is_leader or ln.is_footnote:
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


def page_has_front_title(scan: PageScan) -> bool:
    """표 밖의 앞부속 표제 줄(요약문/SUMMARY/목차/CONTENTS/차례) 존재 여부 — 룩어헤드 가드용."""
    for ln in scan.lines[:8]:
        if ln.in_table:
            continue
        if FRONT_TITLE_RE.match(ln.text.strip()) or ABSTRACT_TITLE_RE.search(ln.text):
            return True
    return False


LOOKAHEAD_PAGES = 25  # 후보 페이지 뒤로 앞부속 표제가 남아 있으면 아직 앞부속(요약문 로마 헤딩 오탐 차단)


def find_body_start(scans: list[PageScan], part: tuple[int, int]) -> tuple[int | None, list[str]]:
    """(본문 시작 페이지, 그 페이지의 1장 후보 프로파일들). 앵커 = 파트 내 아라비아 '- 1 -' 마지막 페이지."""
    anchor = part[0]
    for i in range(part[0], part[1] + 1):
        if scans[i].footer_arabic == 1:
            anchor = i
    for i in range(anchor, part[1] + 1):
        scan = scans[i]
        if scan.blank or scan.image_only or page_is_front(scan):
            continue
        cands = []
        for j, ln in enumerate(scan.lines):
            nxt = scan.lines[j + 1] if j + 1 < len(scan.lines) else None
            cands = line_l1_candidates(ln, nxt)
            if cands:
                break
        if not cands:
            continue
        # 룩어헤드: 뒤 25페이지 안에 앞부속 표제가 남아 있으면 이 후보는 요약문/영문요약 내부
        ahead_end = min(i + LOOKAHEAD_PAGES, part[1])
        if any(page_has_front_title(scans[k]) for k in range(i + 1, ahead_end + 1)):
            continue
        return i, cands
    return None, []


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


def walk_l1(body_lines, profile, report_id: str, diag: dict | None = None,
            no_footer_pages: set | None = None):
    """선택 프로파일로 장 시퀀스 + 뒷부속 감지. (chapters, 소비된 addr 집합) 반환."""
    name, pat, two_line = profile
    no_footer_pages = no_footer_pages or set()
    chapters: list[Heading] = []
    consumed: set[int] = set()
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
        if not text or i in consumed:
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
            if val != expected:
                log_reject(i, text, f"순번 불일치(기대 {expected}, 실제 {val})")
                i += 1
                continue
            title_text = text
            extra = []
            if two_line:
                if i + 1 >= len(body_lines):
                    i += 1
                    continue
                nxt = body_lines[i + 1][2]
                nt = nxt.text.strip()
                if not nt or len(nt) > MAX_TITLE_LINE or FOOTER_RE.match(nt) \
                        or LEADER_RE.search(nt) or nxt.in_table:
                    log_reject(i, text, "두줄형 제목 줄 부적합")
                    i += 1
                    continue
                title_text = f"{text} {nt}"
                extra = [i + 1]
            else:
                # 헤딩 랩 결합: 줄이 컬럼 우측 끝까지 차고 다음 줄이 짧은 비마커 줄이면 이어붙임
                if i + 1 < len(body_lines):
                    nxt_pg, _, nxt = body_lines[i + 1]
                    if nxt_pg == pg and ln.x1 > 0 and nxt.text.strip() \
                            and len(nxt.text.strip()) <= 30 and not nxt.in_table \
                            and not any(p.match(nxt.text.strip()) for _, p, _ in PROFILES) \
                            and not _match_sub_any(nxt.text.strip()) \
                            and _is_full_width(ln, body_lines, pg):
                        title_text = f"{text} {nxt.text.strip()}"
                        extra = [i + 1]
            h = Heading(addr=i, depth=1, hid="", text=re.sub(r"\s+", " ", title_text),
                        family=name, value=val, extra_addrs=extra)
            # 장 제목이 참고문헌이면 references 장으로 (2025-02 "6. 참고문헌")
            if fold(title_text).endswith("참고문헌"):
                h.kind = "references"
                ref_seen = True
                backmatter = True
            chapters.append(h)
            consumed.add(i)
            consumed.update(extra)
            expected += 1
            i += 1 + len(extra)
            continue
        i += 1
    return chapters, consumed


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


def detect_sub_headings(body_lines, chapters, profile_name, report_id, diag=None):
    """정규 장 내부에서 하위 패밀리 발견(전역 첫 등장 순서) + 부모 범위 단조증가 검증."""
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


def detect_structure(scans, part, body_start, cand_profiles, report_id, diag=None):
    """L1 프로파일 확정(본문 시작 페이지의 1장 후보와 결합) → 장 + 하위 헤딩."""
    body_lines = collect_body_lines(scans, part, body_start)
    no_footer = {s.index for s in scans[body_start:part[1] + 1] if s.footer_arabic is None}
    ordered = [p for p in PROFILES if p[0] in cand_profiles]
    chosen = None
    chapters = consumed = None
    for profile in ordered:
        chs, cons = walk_l1(body_lines, profile, report_id, no_footer_pages=no_footer)
        if len([c for c in chs if c.kind == "normal"]) >= 2:
            chosen, chapters, consumed = profile, chs, cons
            break
    if chosen is None:
        return None
    # 확정 워크 (진단 수집 포함)
    chapters, consumed = walk_l1(body_lines, chosen, report_id, diag=diag,
                                 no_footer_pages=no_footer)
    for idx, ch in enumerate(chapters):
        ch.hid = f"{report_id}_c{idx + 1}"
    subs, depth_of, stats = detect_sub_headings(body_lines, chapters, chosen[0], report_id, diag=diag)
    if diag is not None:
        diag["profile"] = chosen[0]
        diag["chapters"] = [{"page": body_lines[c.addr][0] + 1, "kind": c.kind,
                             "id": c.hid, "text": c.text} for c in chapters]
        diag["sub_stats"] = stats
    return {"profile": chosen[0], "body_lines": body_lines, "chapters": chapters,
            "sub_headings": subs, "family_depths": depth_of, "sub_stats": stats}


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
    warnings: list = dataclasses.field(default_factory=list)
    stats: dict = dataclasses.field(default_factory=dict)
    unknown_glyphs: dict = dataclasses.field(default_factory=dict)
    image_only_pages: list = dataclasses.field(default_factory=list)


def build_body(scans, part, body_start, structure, result: PartResult) -> list[str]:
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
# 렌더링
# ---------------------------------------------------------------------------

def yaml_str(s: str) -> str:
    s = (s or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def render_markdown(result: PartResult, source_pdf: str, body: list[str]) -> str:
    meta = result.meta
    rid = result.report_id
    year = int(rid[:4])
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
    if result.n_parts > 1:
        lines.append(f'pdf_pages: "{result.part_range[0] + 1}-{result.part_range[1] + 1}"')
    lines.append("---")
    lines.append("")
    lines.extend(body)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# 파일 단위 처리
# ---------------------------------------------------------------------------

def process_pdf(path: str):
    """PDF 1개 → PartResult 목록(합본이면 여러 개)."""
    base_id = derive_report_id(path)
    if base_id is None:
        r = PartResult(report_id=Path(path).name, part_index=0, n_parts=1, part_range=(0, 0))
        r.status = "skipped_bad_filename"
        r.warnings.append("파일명에서 정책연구-YYYY-NN 패턴을 찾지 못함")
        return [r]

    doc = pymupdf.open(path)
    try:
        scans = scan_document(doc)
    finally:
        doc.close()

    parts = split_bundle(scans)
    results = []
    for pi, part in enumerate(parts):
        rid = base_id if len(parts) == 1 else f"{base_id}_{pi + 1:02d}"
        r = PartResult(report_id=rid, part_index=pi, n_parts=len(parts), part_range=part)
        r.stats["diag"] = {}
        diag = r.stats["diag"]

        meta = parse_abstract(scans, part)
        fallback_institution(scans, part, meta)
        fallback_title(scans, part, meta)
        r.meta = meta
        r.warnings.extend(meta.warnings)

        body_start, cand_profiles = find_body_start(scans, part)
        if body_start is None:
            r.status = "skipped_no_body_start"
            r.warnings.append("본문 시작 페이지를 찾지 못함")
            results.append(r)
            continue
        r.body_start = body_start
        diag["body_start_page"] = body_start + 1
        diag["candidate_profiles"] = cand_profiles

        mark_footnotes(scans, (body_start, part[1]))
        mark_colophon_pages(scans, part, r.warnings)

        structure = detect_structure(scans, part, body_start, cand_profiles, rid, diag=diag)
        if structure is None:
            r.status = "skipped_no_profile"
            r.warnings.append(f"L1 프로파일 검증 실패(후보: {cand_profiles})")
            results.append(r)
            continue
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
    args = parser.parse_args()

    # PowerShell은 글롭을 확장하지 않으므로 자체 확장
    paths = []
    for p in args.pdf:
        if any(c in p for c in "*?["):
            paths.extend(sorted(globmod.glob(p)))
        else:
            paths.append(p)
    if not paths:
        print("입력 PDF가 없습니다.", file=sys.stderr)
        sys.exit(1)

    log_entries = []
    scan_dump = []
    all_ok = True

    for path in paths:
        # 재실행 가드: 기존 출력이 있으면 스캔 비용 없이 스킵 (--scan/--force 제외)
        base_id = derive_report_id(path)
        if not args.scan and not args.force and base_id is not None:
            existing = find_existing_outputs(Path(args.out_dir), base_id)
            if existing:
                log_entries.append({"file": Path(path).name, "report_id": base_id,
                                    "status": "skipped_exists",
                                    "existing": [p.name for p in existing]})
                print(f"[skip] {base_id}: 기존 출력 {len(existing)}개 존재 — 재추출은 --force",
                      file=sys.stderr)
                continue

        try:
            results = process_pdf(path)
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
        "files": log_entries,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    sys.exit(0 if all_ok else 2)


if __name__ == "__main__":
    main()
