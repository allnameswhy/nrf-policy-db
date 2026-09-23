"""목차 앵커 실측 라이브러리 — Phase 1 (2026-09-22).

문서에 내장된 목차를 구조의 원천으로 삼기 위한 단계들:
  toc_page_set   목차 페이지 집합(R6 toc_pages 재사용, 본문 시작 대신 쪽 상한 + 군집 절단)
  toc_rows       목차 페이지의 줄을 y 밴드로 "행"으로 조립(2단 분리, 오른쪽 숫자 조각 결합, 다음 줄 맨몸 숫자)
  parse_entries  행 → 목차 항목(토큰 계열·제목·인쇄 쪽·앞부속 여부·깊이)
  page_offsets   인쇄 쪽 → PDF 쪽 대응표(꼬리말 오프셋 최빈값, 펼침 판형 표식)
  anchor_entries 목차 항목을 본문 줄에 대응(창 ±1 → 전역 순서 유지 최장 사슬)
  md_headings_on_pages 정답셋(.md 헤딩)을 본문 줄에서 찾기(정확히 한 번인가)
  part_has_body_v2 / detect_parts_v2 장 문법 없는 합본 분할 판정(현행 detect_parts와 대조용)

본문 텍스트에는 관여하지 않는다(PyMuPDF 추출 불변). extract.py의 스캔·목차 페이지 인식·앞부속 판정을 import해 쓴다.
"""
from __future__ import annotations

import dataclasses
import difflib
import re
import statistics
from collections import Counter

import extract
from extract import (
    APPENDIX_RE,
    CAPTION_RE,
    FRONT_TITLE_RE,
    HANGUL_ORD,
    REF_TITLE_RE,
    ROMAN_UNI,
    fold,
    page_has_front_title,
    page_is_front,
    page_toc_title,
    roman_ascii_to_int,
    toc_pages,
    toc_title_kind,
)

TOC_WINDOW_PAGES = 40       # 파트 시작부터 목차를 찾는 쪽 상한(본문 시작 쪽을 모르는 레인용)
TOC_CLUSTER_GAP = 3         # 목차 쪽 사이 간격이 이보다 크면 뒤 군집은 버린다(본문 안 리더 쪽 오인 방지)
COL_SPLIT_RATIO = 0.35      # 2단 목차 판정: x0 최대 간격이 쪽 폭의 이 비율 초과
COL_MIN_LINES = 3
BAND_RATIO = 0.5            # 같은 행 판정: y0 차이가 중위 줄 높이의 이 배수 미만
CAND_MAX_LEN = 70           # 포함·유사도 대응 후보 줄 길이 상한(문장 줄 오탐 차단)
CAND_PER_ENTRY = 400        # 항목별 후보 상한 — 위치순 절단이므로 사실상 무제한(30이면 러닝헤드 반복 제목이 앞쪽만 남겨 실제 헤딩 탈락)
FRONT_REGION_PENALTY = 1.0  # 파트 안 마지막 '- 1 -' 꼬리말 쪽 이전(앞부속: 목차 뒤 요약문)의 후보 점수 가산
RUN_HEAD_BAND = 0.15        # 번호 없는 러닝헤드 판정: 쪽 맨 위/맨 아래 15% 띠의 줄이 같은 텍스트로 3쪽 이상 반복
RUN_HEAD_MIN_PAGES = 3
CONTAIN_MIN = 4
SIM_MIN_LEN = 6
SIM_RATIO = 0.8
FRONT_GUARD_ENTRIES = 3     # 앞부속 룩어헤드 가드를 적용하는 선두 항목 수
LOOKAHEAD = 25
WINDOW_BONUS = 0.5          # 창 안 후보의 사슬 점수 가산(동률에서 창 우선)
NO_TOC_MIN_BODY_PAGES = 10  # 목차 없는 구간의 본문 판정: 본문다운 쪽 수 하한
NO_TOC_MIN_CHARS = 300

LEADER_SPLIT_RE = re.compile(r"(?:[·‧․.]\s?){2,}|_{1,}|…+")   # 점·가운뎃점(띄어쓴 `. . .` 포함)·밑줄·줄임표 리더
DIGITS_RE = re.compile(r"^\d{1,3}$")
ROMAN_PAGE_RE = re.compile(r"^[ivxlcIVXLC]{1,6}$")
ROMAN_LOWER_RE = re.compile(r"^[ivxlc]{1,6}$")
TOC_TITLE_WORDS = {"목차", "차례", "표목차", "그림목차", "표차례", "그림차례", "contents", "tableofcontents",
                   "listoftables", "listoffigures", "안내문"}
SUMMARY_ROW_RE = re.compile(r"^\s*[<\[(]?\s*(?:요\s*약|SUMMARY|Summary|Abstract|ABSTRACT|초\s*록)(?:\s*\([^)]*\))?\s*[>\])]?\s*$")
PUA_BULLET_RE = re.compile(r"^[-•◦▪▫■□○●◎◇◆▶▷►\-–—·]+\s*")
WRAP_TAIL_RE = re.compile(r"^\S{1,6}\)$")   # 줄바꿈된 제목 꼬리 조각('학)', '대상)')
PAGE_RANGE_RE = re.compile(r"^(\d{1,3})\s*[-~]\s*\d{1,3}$")
TRAIL_NUM_RE = re.compile(r"\s(\d{1,3})$")
TRAIL_ROMAN_RE = re.compile(r"\s([ivxlc]{1,5})$")
FIG_ROW_RE = re.compile(r"^\s*[\[<(〈［]\s*\d+[-.]\d+\s*[\]>)〉］]")
# 표·그림 목차 행 — extract.CAPTION_RE(아라비아 번호만)에 로마 번호(<표 Ⅱ-1>)·영문 표기를 더한 것
TOC_CAPTION_RE = re.compile(r"^\s*[\[<(〈［]?\s*(?:그림|표|Table|Figure|Fig\.?)\s*[A-Za-z]{0,2}[-.]?\s*[\dⅠ-ⅫIVX]")
HANGUL_RE = re.compile(r"[가-힣]")
LATIN_RE = re.compile(r"[A-Za-z]")
LIST_PAGE_MAX_LINES = 15    # 장 구분 페이지(절 제목 목록) 판정: 쪽의 줄 수 상한(extract.DIVIDER_MAX_LINES와 동일)
LIST_PAGE_MIN_HITS = 3      # 같은 쪽에 서로 다른 항목 후보가 이만큼 이상이면 목록 쪽
LIST_PAGE_PENALTY = 0.3     # 목록 쪽의 하위 항목 후보 점수 가산(실제 헤딩이 동률에서 이기도록)
BACKMATTER_EN_RE = re.compile(r"^(References?|REFERENCES?|Bibliography|Appendi(?:x|ces)|APPENDI(?:X|CES))\b")

# 목차 항목 토큰 계열 — 깊이 추론 전용(순번 검증 없음). 순서 = 우선순위.
TOC_TOKEN_DEFS = [
    ("jang", re.compile(r"^제\s*(\d{1,2})\s*장\s*[.:]?\s*(.*)$")),
    ("jeol", re.compile(r"^제\s*(\d{1,2})\s*절\s*[.:]?\s*(.*)$")),
    ("chapter_en", re.compile(r"^(?:Chapter|CHAPTER|Part|PART)\s+(\d{1,2}|[IVXivx]{1,4})\s*[.:]?\s*(.*)$")),
    ("roman", re.compile(r"^([Ⅰ-Ⅻ])\s*\.?\s*(.*)$")),
    ("roman", re.compile(r"^([IVX]{1,4})(?:\s*\.\s*|\s+)(\S.*)$")),
    ("num_num", re.compile(r"^(\d{1,2}[.\-]\d{1,2})\.?\s*(\S.*)$")),
    ("arabic", re.compile(r"^(\d{1,2})\s*\.(?:\s+|(?!\d))\s*(\S.*)$")),
    ("paren_num", re.compile(r"^\((\d{1,2})\)\s*(\S.*)$")),
    ("num_paren", re.compile(r"^(\d{1,2})\)\s*(\S.*)$")),
    ("ga_dot", re.compile(r"^([가-힣])\s*\.\s*(\S.*)$")),
    ("ga_paren", re.compile(r"^\(?([가-힣])\)\s*(\S.*)$")),
    ("bare_digit", re.compile(r"^(\d{1,2})$")),
]
TOKEN_FAMILIES = ("jang", "jeol", "chapter_en", "roman", "num_num", "arabic", "paren_num", "num_paren",
                  "ga_dot", "ga_paren", "bare_digit")
# 장 계열 우선순위 — 목차에서 "장"이 될 수 있는 계열을 구조적 우선순위로 고른다(최초 출현 순이 아님: 요약문의
# 로마·아라비아 번호가 목차 앞에 오면 최초 출현 순은 장을 3단으로 밀어낸다 — 2021-38 실측).
FAMILY_PRIORITY = ("jang", "chapter_en", "roman", "jeol", "bare_digit", "arabic", "num_num", "paren_num",
                   "num_paren", "ga_dot", "ga_paren")


def token_value(fam: str, tok: str) -> int:
    """토큰의 서수 값(앞부속 목록 절단·순번 진단용). 해석 불가는 0."""
    try:
        if fam in ("jang", "jeol", "arabic", "paren_num", "num_paren", "bare_digit"):
            return int(tok)
        if fam == "num_num":
            return int(re.split(r"[.\-]", tok)[0])
        if fam == "roman":
            return ROMAN_UNI.index(tok) + 1 if tok in ROMAN_UNI else roman_ascii_to_int(tok.upper())
        if fam == "chapter_en":
            return int(tok) if tok.isdigit() else roman_ascii_to_int(tok.upper())
        if fam in ("ga_dot", "ga_paren"):
            return HANGUL_ORD.index(tok) + 1 if tok in HANGUL_ORD else 0
    except (ValueError, IndexError):
        return 0
    return 0


# ---------------------------------------------------------------------------
# 데이터
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class TocRow:
    page: int            # 0-based PDF 쪽
    title_raw: str       # 리더·쪽 번호를 뗀 제목부 원문(토큰 포함)
    page_no: str         # 조립된 인쇄 쪽 번호 문자열('' = 없음)
    roman: bool          # 로마 쪽 번호(앞부속)
    has_leader: bool
    indent: float        # 첫 조각 x0
    lead_spaces: int
    col: int
    y0: float


@dataclasses.dataclass
class TocEntry:
    order: int
    depth: int
    family: str          # TOKEN_FAMILIES | backmatter | none
    token: str
    title: str
    title_fold: str      # 토큰 뗀 제목 접기
    full_fold: str       # 토큰 포함 접기
    printed_page: int | None
    front: bool
    kind: str            # normal | references | appendix
    page: int            # 목차 쪽(0-based)
    indent: float
    joined: bool = False
    target: int | None = None   # 예측 PDF 쪽(anchor_entries가 채움)


@dataclasses.dataclass
class PageMap:
    offset: int | None
    share: float
    n: int
    two_up: bool
    alt_offsets: list
    mappable: bool


@dataclasses.dataclass
class LineRef:
    g: int               # 탐색 영역 내 전역 순번
    page: int
    j: int
    text: str
    lf: str              # fold(text)
    lk: str              # 선두 토큰을 뗀 fold
    in_table: bool
    size: float
    size_delta: float


@dataclasses.dataclass
class Anchor:
    order: int
    tier: str            # "1".."4" | "G1".."G4" | "none"
    page: int | None
    line: int | None
    g: int | None
    text: str
    n_cands: int
    ambiguous: bool
    delta: int | None    # 예측 쪽과의 차이(전역 탐색으로 찾았고 예측이 있을 때)
    dropped_by_order: bool
    size: float | None
    size_delta: float | None


# ---------------------------------------------------------------------------
# 1. 목차 페이지 집합 · 행 조립
# ---------------------------------------------------------------------------

def toc_page_set(scans, part, toc_window: int = TOC_WINDOW_PAGES) -> tuple[list[int], dict]:
    """(목차 쪽 목록, 정보). 1차(국문·리더) 우선, 없으면 2차(영문). 첫 군집 뒤 간격 TOC_CLUSTER_GAP 초과 쪽은 버린다."""
    bound = min(part[0] + toc_window, part[1] + 1)
    primary, secondary = toc_pages(scans, part, bound)
    pages = primary if primary else secondary
    kept: list[int] = []
    dropped = 0
    for p in pages:
        if kept and p - kept[-1] > TOC_CLUSTER_GAP:
            dropped = len(pages) - len(kept)
            break
        kept.append(p)
    kinds = [page_toc_title(scans[p])[0] for p in kept]
    kind = "ko" if "ko" in kinds else ("en" if "en" in kinds else ("leader" if kept else None))
    info = {"kind": kind, "en_only": bool(secondary and not primary), "pages": [p + 1 for p in kept],
            "dropped_pages": dropped, "reached_cap": bool(kept) and kept[-1] >= bound - 1,
            "reach": (kept[-1] - part[0] + 1) if kept else 0}
    return kept, info


def _split_row_text(text: str) -> tuple[str, str, bool, bool]:
    """행 텍스트 → (제목부, 쪽 번호 문자열, 리더 유무, 로마 쪽 번호 여부)."""
    m = LEADER_SPLIT_RE.search(text)
    if m:
        head, tail, has_leader = text[:m.start()].strip(), text[m.end():].strip(), True
    else:
        head, tail, has_leader = text.strip(), "", False
    page_no, roman = "", False
    if tail:
        if DIGITS_RE.match(tail):
            page_no = tail
        elif ROMAN_PAGE_RE.match(tail):
            roman = True
        else:
            mr = PAGE_RANGE_RE.match(tail)
            if mr:
                page_no = mr.group(1)
    else:
        mt = TRAIL_NUM_RE.search(head)
        if mt and len(head[:mt.start()].strip()) >= 2:
            page_no, head = mt.group(1), head[:mt.start()].strip()
        else:
            mr2 = TRAIL_ROMAN_RE.search(head)
            if mr2 and len(head[:mr2.start()].strip()) >= 2:
                roman, head = True, head[:mr2.start()].strip()
    return head, page_no, has_leader, roman


def _text_line(l) -> bool:
    """열 판정에 쓰는 줄 — 맨몸 숫자(쪽 번호 조각)·표제(가운데 정렬) 제외."""
    t = l.text.strip()
    return len(t) >= 2 and not DIGITS_RE.match(t) and toc_title_kind(t) is None


def _columns(lines, width: float) -> tuple[float | None, int]:
    """2단 목차 분리 x 좌표(없으면 None)와 열 수. 분리점 = 오른쪽 열 텍스트의 최소 x0 직전(왼쪽 열의 쪽 번호
    조각이 왼쪽 열에 남도록)."""
    xs = sorted({round(l.x0, 1) for l in lines if _text_line(l)})
    if len(xs) < 2:
        return None, 1
    best_gap, right_x = 0.0, None
    for a, b in zip(xs, xs[1:]):
        if b - a > best_gap:
            best_gap, right_x = b - a, b
    if right_x is None or best_gap <= COL_SPLIT_RATIO * width:
        return None, 1
    split_x = right_x - 2
    left = sum(1 for l in lines if l.x0 < split_x and _text_line(l))
    right = sum(1 for l in lines if l.x0 >= split_x and _text_line(l))
    if left >= COL_MIN_LINES and right >= COL_MIN_LINES:
        return split_x, 2
    return None, 1


COL_TOCISH_MIN = 0.3   # 2단 쪽에서 열의 목차다움(리더·쪽 번호·토큰 행 비율)이 이보다 낮으면 본문 열로 보고 버린다


def _tocish(rows: list[TocRow]) -> float:
    if not rows:
        return 0.0
    n = sum(1 for r in rows if r.has_leader or r.page_no or r.roman or tokenize(r.title_raw)[0] != "none")
    return n / len(rows)


def toc_rows(scans, pages: list[int]) -> tuple[list[TocRow], dict]:
    """목차 페이지들의 줄을 행으로 조립. 정보: 2단 쪽 수, 결합한 숫자 조각 수, 다음 줄 맨몸 숫자 결합 수,
    버린 본문 열 수, 소비한 줄 (page, j) 집합(본문 탐색에서 제외)."""
    rows: list[TocRow] = []
    info = {"two_col_pages": 0, "digit_joins": 0, "next_line_numbers": 0, "dropped_columns": 0, "consumed": set()}
    for p in pages:
        s = scans[p]
        lines = [l for l in s.lines if l.text.strip()]
        if not lines:
            continue
        split_x, ncol = _columns(lines, s.width)
        if ncol == 2:
            info["two_col_pages"] += 1
        heights = [max(l.y1 - l.y0, 0.1) for l in lines]
        band_h = statistics.median(heights) if heights else 10.0
        for col in range(ncol):
            col_lines = [l for l in lines if ncol == 1 or (l.x0 < split_x if col == 0 else l.x0 >= split_x)]
            col_lines.sort(key=lambda l: (round(l.y0, 1), round(l.x0, 1)))
            bands: list[list] = []
            for l in col_lines:
                if bands and abs(l.y0 - bands[-1][0].y0) < BAND_RATIO * band_h:
                    bands[-1].append(l)
                else:
                    bands.append([l])
            col_rows: list[TocRow] = []
            col_x_min = min((l.x0 for l in col_lines if _text_line(l)), default=0.0)
            col_x_max = max((l.x1 for l in col_lines), default=s.width)
            num_x = col_x_min + 0.5 * max(col_x_max - col_x_min, 1.0)
            line_index = {id(l): j for j, l in enumerate(s.lines)}
            for band in bands:
                band.sort(key=lambda l: l.x0)
                frags = [(l, l.text.strip()) for l in band]
                digit_frags = [(l, t) for l, t in frags if DIGITS_RE.match(t)]
                text_frags = [(l, t) for l, t in frags if not DIGITS_RE.match(t)]
                if text_frags and all(ROMAN_LOWER_RE.match(t) for _, t in text_frags) and not digit_frags:
                    # 맨몸 로마 쪽 번호 행(앞부속 항목의 쪽 번호가 다음 줄에 단독) → 윗행을 앞부속으로
                    if col_rows and not col_rows[-1].page_no:
                        col_rows[-1].roman = True
                    continue
                if not text_frags:
                    # 맨몸 숫자 행: 열의 오른쪽 반이면 윗행의 쪽 번호, 아니면 분리형 장 번호 행
                    joined = "".join(t for _, t in digit_frags)
                    first = digit_frags[0][0]
                    if first.x0 > num_x:
                        if col_rows and not col_rows[-1].page_no and not col_rows[-1].roman:
                            col_rows[-1].page_no = joined
                            info["next_line_numbers"] += 1
                        continue
                    col_rows.append(TocRow(p, joined, "", False, False, first.x0,
                                           len(first.text) - len(first.text.lstrip(" ")), col, first.y0))
                    continue
                main = " ".join(t for _, t in text_frags)
                head, page_no, has_leader, roman = _split_row_text(main)
                digits = "".join(t for l, t in digit_frags if l.x0 > text_frags[0][0].x0)
                if digits:
                    if page_no and has_leader:
                        page_no = page_no + digits
                        info["digit_joins"] += 1
                    elif not page_no:
                        page_no = digits
                first = text_frags[0][0]
                col_rows.append(TocRow(p, head, page_no, roman, has_leader, first.x0,
                                       len(first.text) - len(first.text.lstrip(" ")), col, first.y0))
            if ncol == 2 and _tocish(col_rows) < COL_TOCISH_MIN:
                info["dropped_columns"] += 1   # 목차 옆의 본문 열(2022-04-v1 p89 오른쪽 열)
                continue
            rows.extend(col_rows)
            for l in col_lines:
                info["consumed"].add((p, line_index[id(l)]))
    return rows, info


# ---------------------------------------------------------------------------
# 2. 항목 파싱
# ---------------------------------------------------------------------------

def tokenize(text: str) -> tuple[str, str, str]:
    """(계열, 토큰, 제목). 토큰 없으면 ('none', '', text)."""
    t = text.strip()
    for fam, pat in TOC_TOKEN_DEFS:
        m = pat.match(t)
        if m:
            title = m.group(2).strip() if (m.lastindex or 0) >= 2 else ""
            return fam, m.group(1), title
    return "none", "", t


def backmatter_kind(*texts: str) -> str:
    for t in texts:
        t = (t or "").strip()
        if not t:
            continue
        alnum = re.sub(r"[^0-9A-Za-z가-힣]", "", t)
        if REF_TITLE_RE.match(t) or "참고문헌" in alnum:
            return "references"
        if APPENDIX_RE.match(t) or alnum.startswith(("부록", "첨부")):
            return "appendix"
        m = BACKMATTER_EN_RE.match(t)
        if m:
            return "references" if m.group(1).lower().startswith(("ref", "bib")) else "appendix"
    return "normal"


def strip_token_fold(text: str) -> str:
    fam, _tok, title = tokenize(text)
    return fold(title if fam != "none" else text)


def parse_entries(rows: list[TocRow]) -> tuple[list[TocEntry], dict]:
    """행 → 항목. 정보: 제외 수, 결합 수, 계열 순서."""
    info = {"excluded": 0, "joined": 0, "duplicates": 0}
    items: list[dict] = []
    seen: tuple | None = None
    for r in rows:
        text = PUA_BULLET_RE.sub("", r.title_raw.strip(), count=1).strip()
        if not text:
            continue
        kind_t = toc_title_kind(text)
        alnum = re.sub(r"[^0-9A-Za-z가-힣]", "", text).lower()
        if kind_t in ("ko", "en") or alnum in TOC_TITLE_WORDS:
            info["excluded"] += 1
            continue
        if CAPTION_RE.match(text) or TOC_CAPTION_RE.match(text) or FIG_ROW_RE.match(text):
            info["excluded"] += 1
            continue
        if SUMMARY_ROW_RE.match(text):
            kind_t = "other"   # 요약·SUMMARY·초록 행 = 앞부속 항목
        # 줄바꿈된 제목의 꼬리 조각(토큰·리더·번호 없는 짧은 행이 리더·번호 있는 행 뒤에) → 앞 항목 제목에 붙인다
        if items and not r.has_leader and not r.page_no and not r.roman and tokenize(text)[0] == "none" \
                and (WRAP_TAIL_RE.match(text) or (len(text) <= 12 and items[-1]["row"].page == r.page
                                                  and (items[-1]["row"].has_leader or items[-1]["row"].page_no)
                                                  and items[-1]["fam"] in TOKEN_FAMILIES)):
            items[-1]["title"] = (items[-1]["title"] + " " + text).strip()
            info["joined"] += 1
            continue
        fam, tok, title = tokenize(text)
        kind = backmatter_kind(text, title)
        if kind != "normal" and fam == "none":
            fam = "backmatter"
        # 바로 앞 행과 같은 텍스트(같은 쪽)만 중복으로 본다 — 장마다 되풀이되는 '소결'류 항목은 정상
        key = (r.page, fam, tok, fold(title))
        if items and seen == key:
            info["duplicates"] += 1
            continue
        seen = key
        items.append({"row": r, "fam": fam, "tok": tok, "title": title, "kind": kind,
                      "front_title": kind_t == "other" or bool(FRONT_TITLE_RE.match(text)),
                      "joined": False})
    # 줄바꿈 제목 결합
    out: list[dict] = []
    i = 0
    while i < len(items):
        it = items[i]
        nxt = items[i + 1] if i + 1 < len(items) else None
        r = it["row"]
        can_join = nxt is not None and nxt["fam"] == "none" and nxt["kind"] == "normal" \
            and not nxt["front_title"] and nxt["row"].page == r.page
        if can_join and it["fam"] in TOKEN_FAMILIES and it["kind"] == "normal" and (
                it["title"] == "" or (not r.has_leader and not r.page_no and len(it["title"]) >= 12
                                      and nxt["row"].indent >= r.indent - 2)):
            it["title"] = (it["title"] + " " + nxt["title"]).strip()
            nr = nxt["row"]
            it["row"] = TocRow(r.page, r.title_raw + " " + nr.title_raw, r.page_no or nr.page_no,
                               r.roman or nr.roman, r.has_leader or nr.has_leader, r.indent, r.lead_spaces,
                               r.col, r.y0)
            it["joined"] = True
            info["joined"] += 1
            out.append(it)
            i += 2
            continue
        out.append(it)
        i += 1
    items = out
    # 앞부속 1차: 로마 쪽 번호·앞부속 표제·(토큰 있는 문서에서) 첫 토큰 항목 이전의 무토큰 항목
    has_tok = any(it["fam"] in TOKEN_FAMILIES for it in items)
    first_tok = next((k for k, it in enumerate(items) if it["fam"] in TOKEN_FAMILIES or it["kind"] != "normal"),
                     len(items))
    for k, it in enumerate(items):
        r = it["row"]
        it["front"] = r.roman or it["front_title"] or (
            has_tok and it["fam"] == "none" and it["kind"] == "normal" and k < first_tok)
    # 영문 목차 쪽(CONTENTS 번역판)은 대응 대상에서 뺀다 — 쪽 단위 다수결(그 쪽 항목의 70% 이상이 라틴 문자만).
    # 국문 목차 안의 영문 제목 항목(2024-41 4장 사례 대학명)은 남는다.
    info["english_skipped"] = 0
    by_page: dict[int, list[dict]] = {}
    for it in items:
        if not it["front"]:
            by_page.setdefault(it["row"].page, []).append(it)
    korean_pages = {p for p, its in by_page.items() if any(HANGUL_RE.search(it["title"]) for it in its)}
    for p, its in by_page.items():
        eng = [it for it in its if not HANGUL_RE.search(it["title"]) and len(LATIN_RE.findall(it["title"])) >= 3]
        if korean_pages - {p} and len(eng) >= 0.7 * len(its) and len(eng) >= 3:
            for it in its:
                it["front"] = True
                info["english_skipped"] += 1
    # 장 계열 = 구조적 우선순위(FAMILY_PRIORITY)에서 항목 2개 이상이고 값 1이 있는 첫 계열
    chapter_fam = None
    live = [it for it in items if not it["front"] and it["kind"] == "normal" and it["fam"] in TOKEN_FAMILIES]
    present = {it["fam"] for it in live}
    for fam in FAMILY_PRIORITY:
        if fam not in present:
            continue
        vals = [token_value(fam, it["tok"]) for it in live if it["fam"] == fam]
        if len(vals) >= 2 and 1 in vals:
            chapter_fam = fam
            break
    if chapter_fam is None:
        chapter_fam = next((fam for fam in FAMILY_PRIORITY if fam in present), None)
    # 중복 목차(요약 목차 + 세부 목차 — 2024-16·2023-17 실측): 장 계열의 (값, 제목)이 뒤에서 되풀이되면 두 벌.
    # 항목 수가 적은 벌(대개 앞의 요약 목차)을 앞부속으로 돌려 세부 벌만 대응한다.
    info["duplicate_toc"] = 0
    if chapter_fam is not None:
        seen_keys: dict[tuple, int] = {}
        repeats: list[int] = []
        for k, it in enumerate(items):
            if it["front"] or it["kind"] != "normal" or it["fam"] != chapter_fam:
                continue
            key = (token_value(chapter_fam, it["tok"]), fold(it["title"]))
            if key[1] and key in seen_keys:
                repeats.append(k)
            elif key[1]:
                seen_keys[key] = k
        if len(repeats) >= 2:
            j = min(repeats)
            first_ch = next(k for k, it in enumerate(items)
                            if not it["front"] and it["kind"] == "normal" and it["fam"] == chapter_fam)
            a_idx = [k for k in range(first_ch, j) if not items[k]["front"] and items[k]["kind"] == "normal"]
            b_idx = [k for k in range(j, len(items)) if not items[k]["front"] and items[k]["kind"] == "normal"]
            drop = a_idx if len(a_idx) <= len(b_idx) else b_idx
            for k in drop:
                items[k]["front"] = True
            info["duplicate_toc"] = len(drop)
    # 앞부속 2차(목록 절단): 장 계열 값이 1로 되돌아가는 마지막 지점 — 앞 구간(요약문 항목 목록)이 뒤 구간보다
    # 짧을 때만 앞 구간 전체를 앞부속으로. 뒷부속(참고문헌·부록) 이후의 재시작은 부록 번호라 보지 않는다.
    info["front_cut"] = 0
    if chapter_fam is not None:
        first_bm = next((k for k, it in enumerate(items) if it["kind"] != "normal"), len(items))
        seq = [(k, token_value(chapter_fam, it["tok"])) for k, it in enumerate(items)
               if k < first_bm and not it["front"] and it["kind"] == "normal" and it["fam"] == chapter_fam]
        cut = None
        for i in range(1, len(seq)):
            if seq[i][1] == 1 and max(v for _, v in seq[:i]) > 1 and (len(seq) - i) >= i:
                cut = seq[i][0]
        if cut is not None:
            for k, it in enumerate(items):
                if k < cut and it["kind"] == "normal" and not it["front"]:
                    it["front"] = True
                    info["front_cut"] += 1
    # 하위 계열 깊이 = 첫 장 항목 이후 최초 출현 순(2단부터), 무토큰 항목은 들여쓰기 순위(장 계열이 있으면 2단부터)
    fam_order: list[str] = []
    seen_chapter = chapter_fam is None
    for it in items:
        if it["front"] or it["kind"] != "normal":
            continue
        if it["fam"] == chapter_fam:
            seen_chapter = True
            continue
        if seen_chapter and it["fam"] in TOKEN_FAMILIES and it["fam"] not in fam_order:
            fam_order.append(it["fam"])
    if chapter_fam == "jang" and "jeol" in fam_order:   # 제N절은 제N장 바로 아래 — 출현 순서와 무관
        fam_order.remove("jeol")
        fam_order.insert(0, "jeol")
    indents = sorted({round(it["row"].indent / 4) * 4 for it in items if not it["front"]}) or [0]
    entries: list[TocEntry] = []
    for it in items:
        r = it["row"]
        if it["kind"] != "normal" or it["front"] or it["fam"] == chapter_fam:
            depth = 1
        elif it["fam"] in TOKEN_FAMILIES:
            depth = min(2 + fam_order.index(it["fam"]), 4) if it["fam"] in fam_order else 2
        else:
            key = round(r.indent / 4) * 4
            rank = indents.index(key) if key in indents else 0
            depth = min(max(rank + 1, 2 if chapter_fam is not None else 1), 4)
        title = it["title"]
        entries.append(TocEntry(
            order=len(entries) + 1, depth=depth, family=it["fam"], token=it["tok"], title=title,
            title_fold=fold(title), full_fold=fold(r.title_raw),
            printed_page=int(r.page_no) if r.page_no else None,
            front=it["front"], kind=it["kind"], page=r.page, indent=r.indent, joined=it["joined"]))
    info["family_order"] = ([chapter_fam] if chapter_fam else []) + fam_order
    info["chapter_family"] = chapter_fam
    return entries, info


# ---------------------------------------------------------------------------
# 3. 인쇄 쪽 → PDF 쪽
# ---------------------------------------------------------------------------

def page_offsets(scans, part) -> PageMap:
    offs: list[int] = []
    prev = None
    two_up_votes = seq_votes = 0
    for i in range(part[0], part[1] + 1):
        fa = scans[i].footer_arabic
        if fa is None:
            continue
        offs.append(i - fa)
        if prev is not None and i - prev[0] == 1:
            d = fa - prev[1]
            if d == 2:
                two_up_votes += 1
            elif d == 1:
                seq_votes += 1
        prev = (i, fa)
    if not offs:
        return PageMap(None, 0.0, 0, False, [], False)
    cnt = Counter(offs)
    offset, c = cnt.most_common(1)[0]
    share = c / len(offs)
    two_up = two_up_votes >= 3 and two_up_votes > seq_votes
    alt = [o for o, k in cnt.most_common() if o != offset and k >= 3]
    return PageMap(offset, round(share, 3), len(offs), two_up, alt,
                   len(offs) >= 3 and share >= 0.8 and not two_up)


# ---------------------------------------------------------------------------
# 4. 앵커링
# ---------------------------------------------------------------------------

def body_lines(scans, part, domain_start: int, exclude: set | None = None) -> list[LineRef]:
    """탐색 영역의 줄 목록. domain_start = 마지막 목차 쪽(포함) — 목차로 소비된 줄(exclude)은 뺀다(목차 옆 본문 열).
    번호 없는 러닝헤드(쪽 맨 위/맨 아래 띠에서 같은 텍스트가 3쪽 이상 반복 — extract R5는 번호 붙은 것만 제거)도 뺀다."""
    out: list[LineRef] = []
    exclude = set(exclude or set())
    band_hits: dict[str, set] = {}
    band_lines: list[tuple[int, int, str]] = []
    for p in range(domain_start, part[1] + 1):
        s = scans[p]
        if s.blank or s.dropped or not s.lines:
            continue
        top = min(range(len(s.lines)), key=lambda j: s.lines[j].y0)
        bot = max(range(len(s.lines)), key=lambda j: s.lines[j].y1)
        for j, pos in ((top, "top"), (bot, "bot")):
            ln = s.lines[j]
            in_band = ln.y0 < RUN_HEAD_BAND * s.height if pos == "top" else ln.y1 > (1 - RUN_HEAD_BAND) * s.height
            if in_band and ln.text.strip():
                key = pos + ":" + fold(ln.text)
                band_hits.setdefault(key, set()).add(p)
                band_lines.append((p, j, key))
    for p, j, key in band_lines:
        if len(band_hits[key]) >= RUN_HEAD_MIN_PAGES:
            exclude.add((p, j))
    for p in range(domain_start, part[1] + 1):
        s = scans[p]
        if s.blank or s.dropped:   # 이미지 전용 쪽(장 구분 페이지의 '제1장' + 제목 줄)은 포함 — R8과 동일
            continue
        for j, ln in enumerate(s.lines):
            t = ln.text.strip()
            if not t or ln.is_leader or ln.is_footnote or (p, j) in exclude:
                continue
            out.append(LineRef(len(out), p, j, t, fold(t), strip_token_fold(t), ln.in_table,
                               ln.size, ln.size_delta))
    return out


def _match_tier(e: TocEntry, lines: list[LineRef], k: int, with_sim: bool) -> int | None:
    """항목 e가 lines[k]에 대응되면 방법 번호(1 정확·2 포함·3 유사도·4 두 줄), 아니면 None."""
    ln = lines[k]
    if ln.in_table:
        return None
    tf, ff = e.title_fold, e.full_fold
    nxt = lines[k + 1] if k + 1 < len(lines) and lines[k + 1].page == ln.page else None
    prv = lines[k - 1] if k > 0 and lines[k - 1].page == ln.page else None
    if len(tf) >= 2 and (ln.lk == tf or ln.lf == ff):
        # 분리형(앞 줄이 '제1장' 번호 줄)이면 앞 줄이 두 줄 결합으로 잡는다 — 앵커 = 번호 줄(정답 매처와 동일 규약)
        if not (ln.lk == tf and ln.lf != ff and prv is not None and prv.lf + ln.lf == ff):
            return 1
    short = len(ln.text) <= CAND_MAX_LEN
    if short and len(tf) >= CONTAIN_MIN and (tf in ln.lk or (len(ln.lk) >= CONTAIN_MIN and ln.lk in tf)):
        return 2
    # 두 줄 결합(같은 쪽 앞뒤 줄)
    if nxt is not None and not nxt.in_table:
        if ln.lf + nxt.lf == ff or (len(tf) >= 2 and ln.lk + nxt.lf == tf) \
                or (len(tf) >= 6 and tf in (ln.lk + nxt.lf) and len(ln.text) + len(nxt.text) <= CAND_MAX_LEN):
            return 4
    if prv is not None and not prv.in_table and (ln.lf + prv.lf == ff or (len(tf) >= 2 and ln.lk + prv.lf == tf)):
        return 4
    if with_sim and short and len(tf) >= SIM_MIN_LEN and len(ln.lk) >= SIM_MIN_LEN \
            and abs(len(ln.lk) - len(tf)) <= 0.5 * len(tf):
        sm = difflib.SequenceMatcher(None, tf, ln.lk)
        if sm.quick_ratio() >= SIM_RATIO and sm.ratio() >= SIM_RATIO:
            return 3
    return None


class _MaxFenwick:
    def __init__(self, n: int):
        self.n = n
        self.t: list = [None] * (n + 1)

    def update(self, i: int, val):
        while i <= self.n:
            if self.t[i] is None or val[0] > self.t[i][0]:
                self.t[i] = val
            i += i & -i

    def query(self, i: int):
        best = None
        while i > 0:
            v = self.t[i]
            if v is not None and (best is None or v[0] > best[0]):
                best = v
            i -= i & -i
        return best


def anchor_entries(scans, part, entries: list[TocEntry], pmap: PageMap, domain_start: int,
                   lines: list[LineRef] | None = None) -> tuple[list[Anchor], dict]:
    """목차 항목(앞부속 제외)을 본문 줄에 대응. 창(예측 쪽 ±1) 후보 + 전역 후보를 모아 목차 순서를 지키는
    최장 사슬을 고른다. 반환: (항목별 Anchor, 통계)."""
    lines = body_lines(scans, part, domain_start) if lines is None else lines
    targets = [e for e in entries if not e.front]
    n_lines = len(lines)
    front_ahead: dict[int, bool] = {}

    def guard(p: int) -> bool:
        if p not in front_ahead:
            hi = min(p + LOOKAHEAD, part[1])
            front_ahead[p] = any(page_has_front_title(scans[k], True) for k in range(p + 1, hi + 1))
        return front_ahead[p]

    cands: list[list[tuple[int, int, bool]]] = []   # 항목별 [(g, tier, in_window)]
    n_lead = 0
    for e in targets:
        guarded = n_lead < FRONT_GUARD_ENTRIES and e.depth == 1 and e.kind == "normal"
        if guarded:
            n_lead += 1
        found: dict[int, tuple[int, bool]] = {}
        e.target = None
        if pmap.mappable and e.printed_page is not None:
            e.target = e.printed_page + pmap.offset
            for k in range(n_lines):
                if not (e.target - 1 <= lines[k].page <= e.target + 1):
                    continue
                t = _match_tier(e, lines, k, with_sim=True)
                if t is not None:
                    found[k] = (t, True)
        for k in range(n_lines):
            if k in found:
                continue
            t = _match_tier(e, lines, k, with_sim=False)
            if t is not None:
                found[k] = (t, False)
        if not any(t in (1, 2, 4) for t, _w in found.values()):
            for k in range(n_lines):
                if k in found:
                    continue
                t = _match_tier(e, lines, k, with_sim=True)
                if t is not None:
                    found[k] = (t, False)
        cl = [(k, t, w) for k, (t, w) in found.items() if not (guarded and guard(lines[k].page))]
        cl.sort(key=lambda x: (x[1] - (WINDOW_BONUS if x[2] else 0.0), x[0]))
        cands.append(cl[:CAND_PER_ENTRY])
    # 장 구분 페이지(절 제목 목록) 판정: 줄 수 적은 쪽에 서로 다른 항목의 정확·포함 후보가 여럿 — 하위 항목 후보에 가산점
    page_lines = Counter(ln.page for ln in lines if not ln.in_table)
    page_hits: dict[int, set] = {}
    for ei, cl in enumerate(cands):
        for g, t, _w in cl:
            if t <= 2:
                page_hits.setdefault(lines[g].page, set()).add(ei)
    list_pages = {p for p, s in page_hits.items() if len(s) >= LIST_PAGE_MIN_HITS and page_lines[p] <= LIST_PAGE_MAX_LINES}

    # 앞부속 영역: 파트 안 마지막 '- 1 -' 꼬리말 쪽 이전(목차 뒤에 놓인 요약문이 장·절 제목을 되풀이하는 판형 —
    # 2024-16·2023-17 실측: 후보가 요약문 쪽에 몰려 실제 헤딩이 순서 탈락)
    front_end = None
    for p in range(part[0], part[1] + 1):
        if scans[p].footer_arabic == 1:
            front_end = p

    def penalty(ei: int, g: int) -> float:
        pen = LIST_PAGE_PENALTY if (lines[g].page in list_pages and targets[ei].depth >= 2) else 0.0
        if front_end is not None and lines[g].page < front_end:
            pen += FRONT_REGION_PENALTY
        return pen

    # 순서 유지 최장 사슬(펜윅 트리 prefix max)
    fw = _MaxFenwick(n_lines + 1)
    nodes: list[tuple] = []   # (entry_idx, cand, val, parent)
    best_node = None
    for ei, cl in enumerate(cands):
        new_nodes = []
        for (g, t, w) in cl:
            prev = fw.query(g)  # 위치 < g
            score = t - (WINDOW_BONUS if w else 0.0) + penalty(ei, g)
            if prev is None:
                val, parent = (1, -score), None
            else:
                val, parent = (prev[0][0] + 1, prev[0][1] - score), prev[1]
            nid = len(nodes)
            nodes.append((ei, (g, t, w), val, parent))
            new_nodes.append((g, val, nid))
            if best_node is None or val > nodes[best_node][2]:
                best_node = nid
        for g, val, nid in new_nodes:
            fw.update(g + 1, (val, nid))
    chosen: dict[int, tuple[int, int, bool]] = {}
    nid = best_node
    while nid is not None:
        ei, cand, _val, parent = nodes[nid]
        chosen[ei] = cand
        nid = parent
    anchors: list[Anchor] = []
    stats: Counter = Counter()
    for ei, e in enumerate(targets):
        cl = cands[ei]
        n_c = len(cl)
        ambiguous = sum(1 for _g, t, _w in cl if t <= 2) > 1
        if ei in chosen:
            g, t, w = chosen[ei]
            ln = lines[g]
            tier = f"{t}" if w else f"G{t}"
            delta = (ln.page - e.target) if (not w and e.target is not None) else None
            anchors.append(Anchor(e.order, tier, ln.page, ln.j, g, ln.text, n_c, ambiguous, delta, False,
                                  ln.size, ln.size_delta))
            stats[tier] += 1
        else:
            anchors.append(Anchor(e.order, "none", None, None, None, "", n_c, ambiguous, None, n_c > 0,
                                  None, None))
            stats["none"] += 1
            if n_c > 0:
                stats["dropped_by_order"] += 1
    by_order = {e.order: e for e in entries}
    # 구간 크기(대응된 항목 사이 글자 수) — 잎/비잎 구분
    sized = sorted((a for a in anchors if a.g is not None), key=lambda a: a.g)
    sizes = []
    for idx, a in enumerate(sized):
        end_g = sized[idx + 1].g if idx + 1 < len(sized) else n_lines
        chars = sum(len(lines[k].text) for k in range(a.g + 1, end_g))
        leaf = idx + 1 >= len(sized) or by_order[sized[idx + 1].order].depth <= by_order[a.order].depth
        sizes.append((a.order, chars, leaf))
    out = {
        "targets": len(targets), "anchored": len(targets) - stats["none"], "by_tier": dict(stats),
        "list_pages": sorted(p + 1 for p in list_pages),
        "depth1_anchored": sum(1 for a in anchors if a.tier != "none" and by_order[a.order].depth == 1
                               and by_order[a.order].kind == "normal"),
        "unanchored_titles": [by_order[a.order].title[:40] for a in anchors if a.tier == "none"][:10],
        "ambiguous": sum(1 for a in anchors if a.ambiguous),
        "deltas": [a.delta for a in anchors if a.delta is not None],
        "leaf_gt_4000": sum(1 for _o, c, leaf in sizes if leaf and c > 4000),
        "leaf_gt_8000": sum(1 for _o, c, leaf in sizes if leaf and c > 8000),
        "intro_gt_4000": sum(1 for _o, c, leaf in sizes if not leaf and c > 4000),
        "sections": len(sizes),
    }
    return anchors, out


# ---------------------------------------------------------------------------
# 5. 정답셋(.md 헤딩) 매처
# ---------------------------------------------------------------------------

FLAT_HEADING_RE = re.compile(r"^구간 \d+ \(p\.\d+(?:-\d+)?\)$")


def md_headings_on_pages(lines: list[LineRef], headings) -> list[dict]:
    """각 .md 헤딩(depth·text·hid)이 본문 줄에서 몇 번 찾히는지. 장 구분 페이지(절 제목 목록)의 되풀이는 걷어내고
    (`raw_hits`에 원래 횟수), 한 번이면 (page, j, g)를 남긴다."""
    index: dict[str, list[int]] = {}
    for ln in lines:
        if ln.in_table:
            continue
        index.setdefault(ln.lf, []).append(ln.g)
    n = len(lines)
    raw: list[tuple] = []
    for h in headings:
        if FLAT_HEADING_RE.match(h.text):
            raw.append((h, None))
            continue
        key = fold(h.text)
        hits = list(index.get(key, []))
        if len(hits) < 4:
            for g in range(n):
                ln = lines[g]
                if ln.in_table:
                    continue
                nxt = lines[g + 1] if g + 1 < n and lines[g + 1].page == ln.page else None
                prv = lines[g - 1] if g > 0 and lines[g - 1].page == ln.page else None
                if (nxt is not None and ln.lf + nxt.lf == key) or (prv is not None and ln.lf + prv.lf == key):
                    if g not in hits:
                        hits.append(g)
                if len(hits) >= 4:
                    break
        raw.append((h, sorted(hits)))
    # 목록 쪽: 줄 수 적은 쪽에 서로 다른 헤딩이 여럿 찾히는 쪽 — 다른 쪽 후보가 있으면 목록 쪽 후보를 버린다
    page_lines = Counter(ln.page for ln in lines if not ln.in_table)
    page_heads: dict[int, set] = {}
    for hi, (h, hits) in enumerate(raw):
        for g in (hits or []):
            page_heads.setdefault(lines[g].page, set()).add(hi)
    list_pages = {p for p, s in page_heads.items() if len(s) >= LIST_PAGE_MIN_HITS and page_lines[p] <= LIST_PAGE_MAX_LINES}
    out = []
    for h, hits in raw:
        if hits is None:
            out.append({"hid": h.hid, "depth": h.depth, "text": h.text, "hits": -1, "raw_hits": -1, "gs": []})
            continue
        kept = hits
        if len(hits) > 1 and h.depth >= 2:
            non_list = [g for g in hits if lines[g].page not in list_pages]
            if non_list:
                kept = non_list
        rec = {"hid": h.hid, "depth": h.depth, "text": h.text[:60], "hits": len(kept), "raw_hits": len(hits),
               "gs": kept}
        if len(kept) == 1:
            ln = lines[kept[0]]
            rec.update({"page": ln.page, "j": ln.j, "g": ln.g})
        out.append(rec)
    return out


# ---------------------------------------------------------------------------
# 6. 장 문법 없는 합본 분할 판정
# ---------------------------------------------------------------------------

def part_has_body_v2(scans, part) -> bool:
    """구간에 본문이 있는가 — 목차가 있으면 선두 1단 항목이 같은 구간의 목차 뒤에서 찾히는가, 없으면 본문다운 쪽 수."""
    pages, _info = toc_page_set(scans, part)
    if pages:
        rows, rinfo = toc_rows(scans, pages)
        entries, _ = parse_entries(rows)
        heads = [e for e in entries if not e.front and e.depth == 1 and e.kind == "normal"][:5]
        lines = body_lines(scans, part, pages[-1], rinfo["consumed"])
        for e in heads:
            for k in range(len(lines)):
                if _match_tier(e, lines, k, with_sim=False) is not None:
                    return True
        return False
    anchor = part[0]
    for i in range(part[0], part[1] + 1):
        if scans[i].footer_arabic == 1:
            anchor = i
    n_body = 0
    for i in range(anchor, part[1] + 1):
        s = scans[i]
        if s.blank or s.image_only or page_is_front(s) or s.text_chars < NO_TOC_MIN_CHARS:
            continue
        hi = min(i + LOOKAHEAD, part[1])
        if any(page_has_front_title(scans[k], True) for k in range(i + 1, hi + 1)):
            continue
        n_body += 1
        if n_body >= NO_TOC_MIN_BODY_PAGES:
            return True
    return False


def detect_parts_v2(scans) -> list[tuple[int, int]]:
    """현행 detect_parts와 같은 흐름(split_bundle → merge_front_parts)에서 판정 함수만 v2로 바꾼 결과."""
    orig = extract.part_has_body
    extract.part_has_body = part_has_body_v2
    try:
        return extract.detect_parts(scans)
    finally:
        extract.part_has_body = orig
