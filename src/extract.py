"""PDF → 계층 .md 변환 (파이프라인 2단계 — 1단계는 register).

- 입력: pdfs/*.pdf (자체 글롭 확장 — PowerShell 미확장 대응)
- 출력: reports/{report_id}.md (frontmatter + 헤딩 계층 + 본문. LLM 요약 없음)
- 본문·헤딩 텍스트는 PyMuPDF로만 추출한다(`import pymupdf`). 이 모듈은 LLM을 부르지 않는다.
- 문서 단위 = 등록부 행(report_ids.tsv — rid, 쪽 범위). 범위의 원천은 판독 파일 `toc/{PDF stem}.json`
  (register가 src/docread.py로 PDF당 LLM 1회 판독해 만든 문서 범위·본문 시작·목차 항목)이고, 등록부와
  판독이 어긋난 PDF는 등록부 조회 단계에서 미등록 취급된다(registry.toc_problem) → `skipped_unregistered`.
- 구조: **목차 대응**(src/toclane.py) — 판독의 목차 항목을 본문 줄에 대응시켜 그 줄을 헤딩으로 삼는다
  (frontmatter `structure: toc`). 헤딩 ID = 서수 경로 {report_id}_c{i}s{j}…, 판독 파일이 같으면 재추출해도 같다.
  대응이 성립하지 않으면(`skipped_toc_anchor`) 또는 목차 항목이 없으면(`skipped_no_toc`) .md를 만들지 않는다 —
  /admin 카드에서 사람이 그 문서만 플랫으로 수용할지 정한다.
- 플랫(`--flat`): 판독의 본문 시작부터 문단 경계 크기 청킹 → 합성 `# 구간 N (p.a-b)` 헤딩 +
  `structure: flat`. 본문 텍스트 400자 미만(이미지 위주)이면 `skipped_flat_guard`(OCR 별도 과제).
- 대상 지정: `--rid RID`(반복) = 그 문서만. 없으면 주어진 PDF의 모든 문서.
- 재실행 가드: .md가 이미 있는 문서는 **문서 단위로** 건너뛴다(합본의 나머지 문서는 추출). `--force`는
  처리 대상 문서만 덮어쓴다 — annotate 요약과 verify의 verified_extract·verified_annotate 스탬프가 사라진다.
- 진단: --scan (md·로그 미작성, 결과만 stdout 덤프).
- 로그 logs/extract_log.json: 문서(파일, rid)별 **마지막 결과를 병합 보관**(이번 실행이 다룬 문서만 교체) —
  /admin 카드의 재료. 건너뛴 문서(skipped_exists)는 기록하지 않는다.
- 검증 스탬프 1단 verified_register: .md를 만드는 시점에 등록부 rid 조회가 이미 성공했으므로 frontmatter에
  오늘 날짜로 기록한다. 이후 verify가 verified_extract → verified_annotate를 얹는다.
- 종료 코드: 0 = 처리한 문서 전부 성공, 1 = 실패·보류 문서 있음, 2 = 사용 오류.
"""

from __future__ import annotations

import argparse
import bisect
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

# 뒷부속(참고문헌/부록) 마커. '붙임'·'[첨부]'(괄호형)는 본문 한가운데 출현 실측(2025-32)으로 제외.
REF_TITLE_RE = re.compile(r"^\[?\s*참\s*고\s*문\s*헌\s*\]?$")
APPENDIX_RE = re.compile(r"^[<\[]?\s*부\s*록(?=$|[\s\d>\].:_])[>\]]?\s*\.?\s*(.*)$")

# 앞부속 페이지 표제(목차 쪽 판별·판독 프로필 표식용)
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
    """페이지 1장 스캔. tables=False = 표 인식(find_tables) 생략 — 판독(docread)의 쪽 프로필용
    경량 스캔(0.5~1.3초/권, 2026-09-08 실측). 꼬리말·공백·이미지·리더 줄 판정은 표와 무관하다."""
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
# 앞부속 표제 · 목차 페이지 인식
# ---------------------------------------------------------------------------

def page_has_front_title(scan: PageScan, ignore_tables: bool = False) -> bool:
    """표 밖의 앞부속 표제 줄(요약문/SUMMARY/목차/CONTENTS/차례) 존재 여부 — 룩어헤드 가드용."""
    for ln in scan.lines[:8]:
        if ln.in_table and not ignore_tables:
            continue
        if FRONT_TITLE_RE.match(ln.text.strip()) or ABSTRACT_TITLE_RE.search(ln.text):
            return True
    return False


# --- 목차 페이지 인식 (R6, 2026-09-11) --------------------------------------------------------------
# 리더런(`····`) 3줄 이상 페이지에 더해, 목차 표제 페이지(「목차/차례」·「CONTENTS」 — 템플릿 접두·괄호 꼬리
# 「(영문목차)」 허용)와 그 뒤 표제 없는 이어짐 페이지(리더런 3줄 또는 맨몸 숫자 줄 3줄 이상)를 목차로 본다.
# 표·그림 목차 등 다른 앞부속 표제는 이어짐을 끊는다. 쓰는 곳: 판독(docread)이 모델에 보여 줄 목차 쪽을 고를 때
# (tocparse.toc_page_set)와 쪽 프로필 표식.
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
    flat: bool = False  # 플랫 청킹 산출물 (frontmatter 'structure: flat'과 동기)
    structure_tag: str = ""  # 목차 대응 산출물이면 "toc" (frontmatter 'structure: toc')
    error: str = ""  # 이 문서 처리 중 예외(traceback) — status "error"
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
        mid = scan.width / 2 if getattr(scan, "two_up", False) else None

        def half_of(x, mid=mid):
            return 1 if (mid is not None and x >= mid) else 0

        items = []
        for j, ln in enumerate(scan.lines):
            items.append((round(ln.y0, 1), 0, "line", j, ln, half_of(ln.x0)))
        for tab in scan.tables:
            if tab.markdown is not None:
                items.append((round(tab.bbox[1], 1), 1, "table", -1, tab, half_of(tab.bbox[0])))
        # 펼침 판형(한 PDF 쪽에 인쇄 두 쪽 — 목차 대응(toclane)이 scan.two_up 표식): 왼쪽 면을 다 읽고 오른쪽 면
        items.sort(key=lambda it: (it[5], it[0], it[1]))

        page_right = max((l.x1 for l in scan.lines if not l.in_table), default=0.0)
        modal_h = None
        heights = sorted((l.y1 - l.y0) for l in scan.lines if not l.in_table)
        if heights:
            modal_h = heights[len(heights) // 2]
        page_tf = None  # 마스킹 안전판용 페이지 국소 표 haystack (지연 계산)

        prev_ln: Line | None = None
        for y, order, kind, j, obj, _half in items:
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
# 플랫 청킹 (목차 대응이 성립하지 않은 문서를 사람이 수용한 경우 — `--flat`)
# ---------------------------------------------------------------------------

FLAT_MAX_CHARS = 4000   # mdio.split_units max_chars와 동일 — 청크=유닛 1:1 전제
FLAT_MIN_CHARS = 200    # split_units min_chars — 미만 꼬리 구간은 직전 구간에 병합
FLAT_GUARD_CHARS = 400  # 본문 텍스트가 이 미만이면 플랫 청킹 불가(전면 이미지 문서 등 — OCR 별도)


def out_blocks(out: list[str], lo: int = 0, hi: int | None = None) -> list[tuple[list[str], int]]:
    """build_body 출력 out[lo:hi]의 블록(빈 줄 사이 연속 줄 = 문단·표·캡션·마커) → [(내용 줄들, out 시작 인덱스)]."""
    hi = len(out) if hi is None else hi
    blocks: list[tuple[list[str], int]] = []
    i = lo
    while i < hi:
        if out[i] == "":
            i += 1
            continue
        j = i
        while j < hi and out[j] != "":
            j += 1
        blocks.append((out[i:j], i))
        i = j
    return blocks


def page_lookup(page_marks: list):
    """build_body의 page_marks → out 인덱스의 1-based 쪽 조회 함수."""
    marks_idx = [m[0] for m in page_marks]
    marks_pg = [m[1] for m in page_marks]

    def page_of(out_idx: int) -> int:
        k = bisect.bisect_right(marks_idx, out_idx) - 1
        return marks_pg[k] if k >= 0 else 1

    return page_of


def _chunk_lines(blocks, c: tuple[int, int]) -> list[str]:
    s, e = c
    lines_: list[str] = []
    for bl, _ in blocks[s:e]:
        if lines_:
            lines_.append("")
        lines_.extend(bl)
    return lines_


def chunk_span(blocks, c: tuple[int, int]) -> int:
    """구간 크기 — mdio._span_chars와 같은 산식."""
    return len("\n".join(_chunk_lines(blocks, c)).strip())


def chunk_page_label(blocks, c: tuple[int, int], page_of) -> str:
    a = page_of(blocks[c[0]][1])
    b = page_of(blocks[c[1] - 1][1])
    return f"p.{a}" if a == b else f"p.{a}-{b}"


def greedy_chunks(blocks) -> list[tuple[int, int]]:
    """블록 경계 그리디 누적 청크(블록 인덱스 반개구간) — 상한 FLAT_MAX_CHARS, 최소 크기 미만 꼬리는 직전에 병합.
    플랫(chunk_flat)과 목차 대응 산출물의 절 내부 청킹(toclane.chunk_sections)이 같이 쓴다."""
    def span(lines_: list[str]) -> int:
        return len("\n".join(lines_).strip())

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
    # 꼬리 구간이 최소 크기 미만이면 직전 구간에 병합 (200자 미만 유닛 스킵 방지)
    if len(chunks) >= 2 and chunk_span(blocks, chunks[-1]) < FLAT_MIN_CHARS:
        chunks[-2:] = [(chunks[-2][0], chunks[-1][1])]
    return chunks


def chunk_flat(out: list[str], page_marks: list, rid: str, warnings: list) -> tuple[list[str], int]:
    """플랫 본문에 합성 구간 헤딩을 삽입 — 문단(빈 줄) 경계 그리디 누적.

    경계는 항상 build_body가 만든 블록(문단·표·캡션·마커) 사이에만 온다.
    크기 산식은 mdio._span_chars와 동일한 len("\\n".join(...).strip())이라 각 구간이
    그대로 유닛 1개가 된다(상한 초과 단일 블록과 꼬리 병합 구간만 예외 — leaf 통짜 유닛).
    헤딩: '# 구간 {k} (p.{a}-{b}) <!-- id: {rid}_c{k} -->' — k는 방출 순번 1..N 연속,
    페이지는 1-based 인용 라벨(페이지에 걸친 문단은 뒤 페이지로 귀속 — ±1 오차 가능).
    """
    blocks = out_blocks(out)
    page_of = page_lookup(page_marks)
    chunks = greedy_chunks(blocks)

    result_lines: list[str] = []
    for k, c in enumerate(chunks, 1):
        s, e = c
        sp = chunk_span(blocks, c)
        if sp > FLAT_MAX_CHARS:
            warnings.append(f"플랫 구간 {k} 크기 {sp}자 > {FLAT_MAX_CHARS} — 단일 블록 초과/꼬리 병합")
        label = chunk_page_label(blocks, c, page_of)
        result_lines.append(f"# 구간 {k} ({label}) <!-- id: {rid}_c{k} -->")
        result_lines.append("")
        for bl, _ in blocks[s:e]:
            result_lines.extend(bl)
            result_lines.append("")
    while result_lines and result_lines[-1] == "":
        result_lines.pop()
    return result_lines, len(chunks)


def flat_part(r: PartResult, scans, part, rid: str, path: str, body_start: int) -> None:
    """문서 하나를 플랫으로 — 판독의 본문 시작(body_start)부터 구간 청킹해 r.markdown까지 채운다.
    본문 텍스트가 FLAT_GUARD_CHARS 미만이면 skipped_flat_guard. verify가 같은 본문 범위·표식으로 재현한다."""
    mark_footnotes(scans, (body_start, part[1]))
    mark_colophon_pages(scans, part, r.warnings)
    structure = {"body_lines": collect_body_lines(scans, part, body_start),
                 "chapters": [], "sub_headings": []}
    page_marks: list = []
    body = build_body(scans, part, body_start, structure, r, page_marks=page_marks)
    diag = r.stats.setdefault("diag", {})
    diag["body_start_page"] = body_start + 1
    diag["lane"] = "flat"
    r.body_start = body_start
    real = "\n".join(l for l in body if not l.startswith("<!--")).strip()
    if len(real) < FLAT_GUARD_CHARS:
        r.status = "skipped_flat_guard"
        r.warnings.append(f"본문 텍스트 {len(real)}자 < {FLAT_GUARD_CHARS} — 플랫 청킹 불가(이미지 위주 문서? OCR 별도)")
        return
    body, n_chunks = chunk_flat(body, page_marks, rid, r.warnings)
    r.flat = True
    r.markdown = render_markdown(r, f"pdfs/{Path(path).name}", body)
    r.stats["headings"] = n_chunks
    r.stats["chapters"] = n_chunks
    r.warnings.append(f"플랫 청킹 적용 (구간 {n_chunks}개)")


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
        lines.append("structure: flat")  # 플랫 청킹 표식 — verify·status가 이 키로 분기
    elif result.structure_tag:
        lines.append(f"structure: {result.structure_tag}")  # 목차 대응 산출물(toc) — verify가 이 키로 분기
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

def process_pdf(path: str, *, flat_rids=frozenset(), only=None):
    """PDF 1개 → 처리한 문서의 PartResult 목록. only = 처리할 rid 집합(None이면 전부),
    flat_rids = 플랫으로 뽑을 rid 집합(나머지는 목차 대응).

    문서 범위는 등록부 행(rid, pages)을 그대로 쓴다 — 범위의 원천은 판독 파일이고 둘의 일치는 등록부
    조회(parts_for)가 보장한다. 한 문서의 예외는 그 문서만 status "error"로 남긴다.
    """
    reg_parts = parts_for(path)
    if not reg_parts:  # 프리플라이트가 막으므로 방어용
        raise ValueError(f"미등록 PDF — {rid_reason(path)}: {Path(path).name}")
    import toclane  # tocparse가 이 모듈을 import하므로 지연 import(순환 회피)
    data = toclane.load_docread(path)

    doc = pymupdf.open(path)
    try:
        scans = scan_document(doc)
    finally:
        doc.close()

    parts = [(rid, rng or (0, len(scans) - 1)) for rid, rng in reg_parts]
    results = []
    for pi, (rid, part) in enumerate(parts):
        if only is not None and rid not in only:
            continue
        r = PartResult(report_id=rid, part_index=pi, n_parts=len(parts), part_range=part)
        r.stats["diag"] = {}
        try:
            meta = parse_abstract(scans, part)
            fallback_institution(scans, part, meta)
            fallback_title(scans, part, meta)
            r.meta = meta
            r.warnings.extend(meta.warnings)
            docd = toclane.pick_document(data, part) if data else None
            if docd is None:  # 등록 뒤 PDF나 판독 파일이 바뀐 경우
                r.status = "skipped_no_docread"
                r.warnings.append(f"판독 파일에 쪽 범위 {part[0] + 1}-{part[1] + 1}인 문서 없음 — 행 삭제 후 재등록 필요")
            elif rid in flat_rids:
                flat_part(r, scans, part, rid, path, toclane.body_start_of(docd, part))
            else:
                toclane.process_part(r, scans, part, rid, path, docd)
            r.stats["footer_runs"] = summarize_footers(scans, part)
        except Exception:
            r.status = "error"
            r.error = traceback.format_exc(limit=5)
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

def part_entry(r: PartResult, path) -> dict:
    """문서 1개의 로그·진단 항목(로그와 --scan이 같은 꼴을 쓴다)."""
    diag = r.stats.get("diag", {})
    meta = r.meta
    return {
        "file": Path(path).name,
        "report_id": r.report_id,
        "status": r.status,
        "lane": diag.get("lane"),
        "flat": r.flat,
        "at": datetime.datetime.now().isoformat(timespec="seconds"),
        "part_range": [r.part_range[0] + 1, r.part_range[1] + 1],
        "body_start": diag.get("body_start_page"),
        "chapters": r.stats.get("chapters"),
        "headings": r.stats.get("headings"),
        "tables": r.stats.get("tables"),
        "masked_lines": r.stats.get("masked_lines"),
        "recovered_lines": r.stats.get("recovered_lines"),
        "image_only_pages": r.image_only_pages,
        "unknown_glyphs": r.unknown_glyphs,
        "footer_runs": r.stats.get("footer_runs", []),
        "meta": {
            "title": (meta.title[:60] if meta else ""),
            "lead_researcher": meta.lead_researcher if meta else "",
            "institution": meta.institution if meta else "",
            "abstract_chars": len(meta.abstract) if meta else 0,
        },
        "toc_lane": diag.get("toc_lane"),
        "warnings": r.warnings,
        "error": r.error or None,
    }


def merge_log(log_path: Path, new_entries: list[dict]) -> None:
    """로그를 문서별 마지막 결과로 병합 — 이번 실행이 다룬 (파일, rid)만 교체한다. 파일 단위 항목(rid 없음:
    미등록·중복·파일 오류)은 그 파일의 문서 항목을 대신하고, 문서 항목이 생기면 파일 단위 항목은 지운다.
    등록부에서 사라진 rid·없어진 PDF의 항목은 정리한다."""
    from registry import norm_name, rid_file_map
    old: list[dict] = []
    if log_path.is_file():
        try:
            data = json.loads(log_path.read_text(encoding="utf-8"))
            if data.get("mode") == "extract":
                old = [e for e in data.get("files", []) if e.get("file")]
        except (OSError, ValueError):
            old = []
    file_level = {e["file"] for e in new_entries if not e.get("report_id")}
    part_level = {e["file"] for e in new_entries if e.get("report_id")}
    new_keys = {(e["file"], e.get("report_id") or "") for e in new_entries}
    rid_file = rid_file_map()
    kept = []
    for e in old:
        f, rid = e["file"], e.get("report_id") or ""
        if (f, rid) in new_keys or f in file_level or (not rid and f in part_level):
            continue
        if rid and rid_file.get(rid) != norm_name(f):
            continue
        if not rid and not Path(e.get("path") or "").is_file():
            continue
        kept.append(e)
    out = sorted(kept + new_entries, key=lambda e: (e["file"], e.get("report_id") or ""))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = log_path.with_suffix(log_path.suffix + ".tmp")
    tmp.write_text(json.dumps({
        "run_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "mode": "extract",
        "files": out,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(log_path)


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="PDF에서 계층 구조 .md를 추출한다(목차 대응; --flat = 구간 청킹).")
    parser.add_argument("pdf", nargs="+", help="변환할 PDF 파일 경로 (pdfs/*.pdf — 자체 글롭 확장)")
    parser.add_argument("-o", "--out-dir", default="reports", help="출력 디렉터리 (기본: reports)")
    parser.add_argument("--log", default="logs/extract_log.json", help="로그 경로 (기본: logs/extract_log.json)")
    parser.add_argument("--rid", action="append", default=[], metavar="RID",
                        help="이 문서만 처리(반복 가능) — 합본의 한 문서만 다시 뽑을 때. 없으면 PDF의 모든 문서")
    parser.add_argument("--flat", action="store_true",
                        help="처리 대상 문서를 플랫(구간 청킹)으로 추출 — 목차 대응이 성립하지 않은 문서를 수용할 때")
    parser.add_argument("--scan", action="store_true", help="진단 모드: md·로그 미작성, 결과만 stdout에 덤프")
    parser.add_argument("--force", action="store_true",
                        help="처리 대상 문서의 .md가 이미 있어도 재추출·덮어쓰기 (annotate 요약 블록·검증 스탬프 소실)")
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
        sys.exit(2)

    # 등록부 게이트: rid는 report_ids.tsv 조회만 — 미등록(공란·판독 불일치 포함)은 --scan에서도 스킵.
    # 중복·충돌 프리플라이트: 배치 안에서 같은 rid로 모이는 파일 — 크기까지 같으면 중복 다운로드 의심
    # (사전순 첫 파일만 진행), 다르면 진짜 충돌(전원 스킵). --scan은 경고만 남기고 전부 처리한다.
    only = set(args.rid) or None
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
    if only is not None and only - set(by_rid):
        print("주어진 PDF에 없는 rid: " + ", ".join(sorted(only - set(by_rid))), file=sys.stderr)
        sys.exit(2)
    for rid, group in sorted(by_rid.items()):
        if len(group) < 2:
            continue
        group = sorted(group)
        if len({Path(p).stat().st_size for p in group}) == 1:
            for p in group[1:]:
                dropped[p] = {"status": "skipped_duplicate", "report_id_dup": rid,
                              "duplicate_of": Path(group[0]).name}
            print(f"[중복] {rid}: 동일 크기 파일 {len(group)}개 — "
                  f"'{Path(group[0]).name}'만 진행, 나머지는 삭제 권장", file=sys.stderr)
        else:
            for p in group:
                dropped[p] = {"status": "skipped_id_collision", "report_id_dup": rid}
            print(f"[충돌] {rid}: 서로 다른 파일 {len(group)}개가 같은 report_id로 유도 — "
                  "파일명 조정 필요, 전원 스킵", file=sys.stderr)

    log_entries = []
    scan_dump = []
    all_ok = True
    now = datetime.datetime.now().isoformat(timespec="seconds")

    for path in paths:
        drop = dropped.get(path)
        if drop is not None and (not args.scan or drop["status"] == "skipped_unregistered"):
            if only is None or drop["status"] != "skipped_unregistered":
                all_ok = False
                log_entries.append({"file": Path(path).name, "path": str(path), "at": now, **drop})
            continue

        targets = [rid for rid, _ in parts_for(path) if only is None or rid in only]
        if not targets:
            continue
        # 재실행 가드: .md가 이미 있는 문서는 문서 단위로 건너뛴다 (--scan/--force 제외) — 로그는 건드리지 않는다
        if not args.scan and not args.force:
            existing = [rid for rid in targets if (Path(args.out_dir) / f"{rid}.md").exists()]
            for rid in existing:
                print(f"[skip] {rid}: 기존 출력 존재 — 재추출은 --force", file=sys.stderr)
            targets = [rid for rid in targets if rid not in existing]
            if not targets:
                continue

        try:
            results = process_pdf(path, flat_rids=set(targets) if args.flat else frozenset(), only=set(targets))
        except Exception:
            all_ok = False
            log_entries.append({"file": Path(path).name, "path": str(path), "at": now, "status": "error",
                                "error": traceback.format_exc(limit=5)})
            print(f"[error] {Path(path).name}", file=sys.stderr)
            continue

        for r in results:
            entry = part_entry(r, path)
            if args.scan:
                scan_dump.append(entry)
            log_entries.append(entry)
            if r.status != "ok":
                all_ok = False
            if r.status == "ok" and not args.scan:
                out_path = Path(args.out_dir) / f"{r.report_id}.md"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(r.markdown, encoding="utf-8", newline="\n")
            tag = "scan" if args.scan else r.status
            print(f"[{tag}] {r.report_id}: lane={entry['lane']} "
                  f"chapters={entry['chapters']} headings={entry['headings']} "
                  f"body_start=p{entry['body_start']}", file=sys.stderr)

    if args.scan:
        print(json.dumps(scan_dump, ensure_ascii=False, indent=1))
    else:
        merge_log(Path(args.log), log_entries)

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
