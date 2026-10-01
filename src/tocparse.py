"""목차 대응 라이브러리 (R12, 2026-09-22 — 목차 항목을 본문 줄에 대응시키는 결정론 단계).

  toc_page_set   목차 페이지 집합(extract.toc_pages 재사용, 본문 시작 대신 쪽 상한 + 군집 절단) — 판독(docread)이
                 모델에 보여 줄 쪽을 고를 때 쓴다. 목차 내용 자체는 판독(LLM)이 읽는다.
  page_offsets   인쇄 쪽 → PDF 쪽 대응표(꼬리말 오프셋 최빈값, 펼침 판형 표식)
  body_lines     대응 후보가 되는 본문 줄 목록(번호 없는 러닝헤드 제외, 펼침 판형 읽기 순서)
  anchor_entries 목차 항목을 본문 줄에 대응(쪽 번호 창 → 전역 순서 유지 최장 사슬)

본문 텍스트에는 관여하지 않는다(PyMuPDF 추출 불변). extract.py의 스캔·목차 페이지 인식을 import해 쓴다.
"""
from __future__ import annotations

import dataclasses
import difflib
import re
from collections import Counter

from extract import (
    APPENDIX_RE,
    HANGUL_ORD,
    REF_TITLE_RE,
    ROMAN_UNI,
    fold,
    page_has_front_title,
    page_toc_title,
    roman_ascii_to_int,
    toc_pages,
)

TOC_WINDOW_PAGES = 40       # 파트 시작부터 목차를 찾는 쪽 상한(본문 시작 쪽을 모르는 레인용)
TOC_CLUSTER_GAP = 3         # 목차 쪽 사이 간격이 이보다 크면 뒤 군집은 버린다(본문 안 리더 쪽 오인 방지)
CAND_MAX_LEN = 70           # 포함·유사도 대응 후보 줄 길이 상한(문장 줄 오탐 차단)
CAND_PER_ENTRY = 400        # 항목별 후보 상한 — 위치순 절단이므로 사실상 무제한(30이면 러닝헤드 반복 제목이 앞쪽만 남겨 실제 헤딩 탈락)
FRONT_REGION_PENALTY = 1.0  # 파트 안 마지막 '- 1 -' 꼬리말 쪽 이전(앞부속: 목차 뒤 요약문)의 후보 점수 가산
RUN_HEAD_BAND = 0.15        # 번호 없는 러닝헤드 판정: 쪽 맨 위/맨 아래 15% 띠의 줄이 같은 텍스트로 3쪽 이상 반복
RUN_HEAD_MIN_PAGES = 3
RUN_HEAD_SPAN = 6           # 러닝헤드로 보는 반복의 쪽 폭(이 폭 안에 RUN_HEAD_MIN_PAGES쪽 이상)
REF_LINE_MAX_LEN = 30       # 참고문헌 표제 줄 길이 상한(문장 속 '참고문헌' 오탐 차단)
PREFIX_MIN = 6              # 구두점 뺀 앞부분 일치의 제목 길이 하한
POSITION_TOP_LINES = 3      # 위치 기반 대응이 보는 쪽 맨 위 줄 수
ALNUM_STRIP_RE = re.compile(r"[^0-9A-Za-z가-힣]")
CONTAIN_MIN = 4
SIM_MIN_LEN = 6
SIM_RATIO = 0.8
SHORT_SIM_RATIO = 0.6       # 번호가 같은 짧은 제목의 유사도 하한(5자 중 1자 차이 = 0.8, 4자 중 1자 = 0.75, 3자 중 1자 ≈ 0.67)
FRONT_GUARD_ENTRIES = 3     # 앞부속 룩어헤드 가드를 적용하는 선두 항목 수
LOOKAHEAD = 25
WINDOW_BONUS = 0.5          # 창 안 후보의 사슬 점수 가산(동률에서 창 우선)

LIST_PAGE_MAX_LINES = 15    # 장 구분 페이지(절 제목 목록) 판정: 쪽의 줄 수 상한
LIST_PAGE_MIN_HITS = 3      # 같은 쪽에 서로 다른 항목 후보가 이만큼 이상이면 목록 쪽
LIST_PAGE_PENALTY = 0.3     # 목록 쪽의 하위 항목 후보 점수 가산(실제 헤딩이 동률에서 이기도록)
TOKEN_ONLY_RE = re.compile(r"^(?:제\s*\d{1,2}\s*[장절편부]|[Ⅰ-Ⅻ]|[IVX]{1,4}|\d{1,2}|(?:Chapter|CHAPTER|Part|PART)\s*\d{1,2})\s*\.?$")
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
    label: str = ""             # 판독 파일이 분리해 준 번호 표기 원문(`Ⅱ.1.`) — 위치 기반 대응의 번호 비교용


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
    y0: float = 0.0


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
    # 러닝헤드는 가까운 쪽에서 연달아 되풀이된다 — RUN_HEAD_SPAN쪽 폭 안에 RUN_HEAD_MIN_PAGES쪽 이상 모인 쪽만 제외.
    # 장마다 같은 글자로 쪽 맨 위에 오는 절 제목(`사업 개요`·`종합분석`, 2024-39)은 수십 쪽 간격이라 후보로 남는다.
    run_pages: dict[str, set] = {}
    for key, pages in band_hits.items():
        ps = sorted(pages)
        for i in range(len(ps) - RUN_HEAD_MIN_PAGES + 1):
            if ps[i + RUN_HEAD_MIN_PAGES - 1] - ps[i] < RUN_HEAD_SPAN:
                run_pages.setdefault(key, set()).update(ps[i:i + RUN_HEAD_MIN_PAGES])
    for p, j, key in band_lines:
        if p in run_pages.get(key, ()):
            exclude.add((p, j))
    for p in range(domain_start, part[1] + 1):
        s = scans[p]
        if s.blank or s.dropped:   # 이미지 전용 쪽(장 구분 페이지의 '제1장' + 제목 줄)은 포함 — R8과 동일
            continue
        order = range(len(s.lines))
        if getattr(s, "two_up", False):   # 펼침 판형: 왼쪽 면 → 오른쪽 면(extract.build_body와 같은 순서)
            order = sorted(order, key=lambda j: (1 if s.lines[j].x0 >= s.width / 2 else 0, round(s.lines[j].y0, 1), j))
        for j in order:
            ln = s.lines[j]
            t = ln.text.strip()
            if not t or ln.is_leader or ln.is_footnote or (p, j) in exclude:
                continue
            out.append(LineRef(len(out), p, j, t, fold(t), strip_token_fold(t), ln.in_table,
                               ln.size, ln.y0))
    return out


def _match_tier(e: TocEntry, lines: list[LineRef], k: int, with_sim: bool, in_window: bool = False) -> int | None:
    """항목 e가 lines[k]에 대응되면 방법 번호(1 정확·2 포함·3 유사도·4 여러 줄 결합), 아니면 None.
    in_window = 쪽 번호 창 안의 줄(창 안에서만 허용하는 느슨한 조건용)."""
    ln = lines[k]
    if ln.in_table:
        return None
    tf, ff = e.title_fold, e.full_fold
    nxt = lines[k + 1] if k + 1 < len(lines) and lines[k + 1].page == ln.page else None
    prv = lines[k - 1] if k > 0 and lines[k - 1].page == ln.page else None
    # 참고문헌 항목은 글자 일치 대신 표제 줄 종류로(목차 `참고문헌(References)` / 본문 `참고문헌`, 2025-17) — 참고문헌에만
    if e.kind == "references" and e.depth == 1:
        return 1 if (len(ln.text) <= REF_LINE_MAX_LEN and backmatter_kind(ln.text) == "references") else None
    if len(tf) >= 2 and (ln.lk == tf or ln.lf == ff):
        # 분리형(앞 줄이 '제1장' 번호 줄)이면 앞 줄이 두 줄 결합으로 잡는다 — 앵커 = 번호 줄(정답 매처와 동일 규약)
        if not (ln.lk == tf and ln.lf != ff and prv is not None and prv.lf + ln.lf == ff):
            return 1
    short = len(ln.text) <= CAND_MAX_LEN
    # 줄이 제목의 일부인 방향은 줄이 제목의 절반 이상을 덮을 때만 — 줄바꿈된 목록 줄의 꼬리 조각(`Malysia`)이
    # 긴 제목에 포함 대응돼 실제 헤딩(유사도 대응)을 밀어내던 문제(2024-41, Phase 2)
    if short and len(tf) >= CONTAIN_MIN and (tf in ln.lk or (len(ln.lk) >= CONTAIN_MIN and ln.lk in tf
                                                             and 2 * len(ln.lk) >= len(tf))):
        return 2
    # 두 줄 결합(같은 쪽 앞뒤 줄)
    if nxt is not None and not nxt.in_table:
        if ln.lf + nxt.lf == ff or (len(tf) >= 2 and ln.lk + nxt.lf == tf) \
                or (len(tf) >= 6 and tf in (ln.lk + nxt.lf) and len(ln.text) + len(nxt.text) <= CAND_MAX_LEN):
            return 4
    if prv is not None and not prv.in_table and (ln.lf + prv.lf == ff or (len(tf) >= 2 and ln.lk + prv.lf == tf)):
        return 4
    # 3~JOIN_MAX_LINES줄 결합(정확 일치만) — 구분 쪽에서 제목이 여러 줄로 쪼개진 판형
    # (`Ⅳ.` / `방사선 이용` / `희귀난치질환 대응` / `핵심기술개발사업`, 2024-39). 앵커 = 첫 줄.
    cat_f, cat_k = ln.lf, ln.lk
    for m in range(1, JOIN_MAX_LINES):
        if k + m >= len(lines) or lines[k + m].page != ln.page or lines[k + m].in_table:
            break
        cat_f += lines[k + m].lf
        cat_k += lines[k + m].lf
        if len(cat_f) > len(ff) + 2:
            break
        if m >= 2 and (cat_f == ff or (len(tf) >= 2 and cat_k == tf)):
            return 4
    # 구두점을 뺀 글자로 줄이 목차 제목으로 시작(창 안에서만) — 목차가 줄여 적은 긴 제목
    # (목차 `기술수요조사서 (1-1)` / 본문 `기술수요조사서 (1-1. 방사선 …`, 2025-17-v1)
    # 줄이 글자·숫자로 바로 시작할 때만(불릿 줄 `❍실험실창업지원사업* 현황 조사`가 장 제목 `실험실창업지원사업 현황`에
    # 앞부분 일치로 잡혀 실제 헤딩을 밀어냄 — 2019-54), 방법 번호는 유사도와 같은 3
    if in_window and short and ln.text[:1].isalnum():
        ta = ALNUM_STRIP_RE.sub("", tf)
        if len(ta) >= PREFIX_MIN and ALNUM_STRIP_RE.sub("", ln.lk).startswith(ta):
            return 3
    if with_sim and short and len(tf) >= SIM_MIN_LEN and len(ln.lk) >= SIM_MIN_LEN \
            and abs(len(ln.lk) - len(tf)) <= 0.5 * len(tf):
        sm = difflib.SequenceMatcher(None, tf, ln.lk)
        if sm.quick_ratio() >= SIM_RATIO and sm.ratio() >= SIM_RATIO:
            return 3
    # 같은 번호 + 짧은 제목의 한두 글자 차이(목차 `Ⅲ. 성과 및 진단` / 본문 `III. 성과와 진단`, 2022-65) —
    # 유사도 하한(SIM_MIN_LEN자)에 못 미치는 제목은 번호가 같을 때만 허용
    if with_sim and short and e.token and 3 <= len(tf) and abs(len(ln.lk) - len(tf)) <= max(1, len(tf) // 4):
        fam, tok, _rest = tokenize(ln.text)
        if fam != "none" and fam == e.family and token_value(fam, tok) == token_value(e.family, e.token) \
                and token_value(fam, tok) > 0 \
                and difflib.SequenceMatcher(None, tf, ln.lk).ratio() >= SHORT_SIM_RATIO:
            return 3
    # 같은 번호 + 목차 제목의 낱말이 줄에 순서대로 전부(본문 제목에 괄호 풀이가 끼어든 판형 —
    # 목차 `4. TUM in Singapore` / 본문 `4. TUM (Technische Universität München) in Singapore`, 2024-41)
    if with_sim and short and e.token and len(tf) >= SIM_MIN_LEN:
        fam, tok, rest = tokenize(ln.text)
        if fam != "none" and fam == e.family and tok == e.token:
            words = e.title.split()
            pos = 0
            for w in words:
                pos = rest.find(w, pos)
                if pos < 0:
                    break
                pos += len(w)
            else:
                if len(words) >= 2:
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


JOIN_MAX_LINES = 4           # 헤딩 한 개가 차지하는 줄 수 상한(번호 줄 + 줄바꿈된 제목)


def join_partner(e: TocEntry, lines: list[LineRef], g: int) -> tuple[list[int], str] | None:
    """lines[g]에 대응된 항목의 제목이 같은 쪽의 이웃 줄과 합쳐야 완성되는 경우(분리형 `제1장` + 제목 줄,
    줄바꿈된 제목, 번호만 있는 앞 줄) 그 헤딩이 차지하는 줄들의 g(오름차순, g 포함)와 결합 텍스트.
    한 줄로 충분하면 None. `_match_tier`의 두 줄 조건을 JOIN_MAX_LINES줄까지 넓힌 것."""
    ln = lines[g]
    tf, ff = e.title_fold, e.full_fold
    pg = ln.page

    def ok(k: int) -> bool:
        return 0 <= k < len(lines) and lines[k].page == pg and not lines[k].in_table

    span = None
    if not (ln.lf == ff or (len(tf) >= 2 and ln.lk == tf)):
        best = None
        for width in range(2, JOIN_MAX_LINES + 1):
            for a in range(g - width + 1, g + 1):
                b = a + width - 1
                if not all(ok(k) for k in range(a, b + 1)):
                    continue
                cat_f = "".join(lines[k].lf for k in range(a, b + 1))
                cat_k = lines[a].lk + "".join(lines[k].lf for k in range(a + 1, b + 1))
                exact = cat_f == ff or (len(tf) >= 2 and cat_k == tf)
                rev = width == 2 and (lines[b].lf + lines[a].lf == ff or (len(tf) >= 2 and lines[b].lk + lines[a].lf == tf))
                contain = len(tf) >= 6 and tf in cat_k \
                    and sum(len(lines[k].text) for k in range(a, b + 1)) <= CAND_MAX_LEN \
                    and not any(tf in "".join(lines[k].lf for k in range(a2, b2 + 1))
                                for a2, b2 in ((a + 1, b), (a, b - 1)))
                if exact or rev or contain:
                    rank = 0 if exact else (1 if rev else 2)
                    if best is None or rank < best[0]:
                        best = (rank, a, b, rev)
            if best is not None:
                break
        if best is not None:
            span = (best[1], best[2], best[3])
    a, b, rev = span if span else (g, g, False)
    order = [b, a] if rev else list(range(a, b + 1))
    # 번호만 있는 앞 줄(`Ⅳ`, `제4장`, 큰 글자 `4`) + 번호 없는 제목 줄 — 본문 번호가 목차와 달라도 한 헤딩
    if ok(a - 1) and tokenize(lines[a].text)[0] == "none" and not rev:
        pt = lines[a - 1].text.strip()
        if TOKEN_ONLY_RE.match(pt) and (not pt.rstrip(".").isdigit() or lines[a - 1].size >= lines[a].size - 0.5):
            a -= 1
            order = [a] + order
    # 같은 가로 줄의 번호가 제목 뒤에 읽히는 조판(제목 x가 번호 x보다 오른쪽인데 줄 순서는 제목 먼저 — 2024-39 `사업 개요` / `1`)
    elif ok(b + 1) and tokenize(lines[a].text)[0] == "none" and not rev:
        nt = lines[b + 1]
        if TOKEN_ONLY_RE.match(nt.text.strip()) and abs(nt.y0 - lines[b].y0) < 0.8 * max(lines[b].size, 1.0):
            b += 1
            order = [b] + order
    if a == b == g:
        return None
    return list(range(a, b + 1)), " ".join(lines[k].text for k in order)


LABEL_NUM_RE = re.compile(r"\d{1,3}|[Ⅰ-Ⅻ]|[IVX]{1,4}")


def _entry_number(e: TocEntry) -> tuple[bool, int] | None:
    """항목 번호의 (로마 여부, 값). 겹번호(`Ⅱ.1.`)는 마지막 성분. 번호 없으면 None."""
    parts = LABEL_NUM_RE.findall(e.label or "")
    if parts:
        p = parts[-1]
        if p.isdigit():
            return False, int(p)
        v = ROMAN_UNI.index(p) + 1 if p in ROMAN_UNI else roman_ascii_to_int(p)
        return (True, v) if v else None
    if e.family in TOKEN_FAMILIES and e.family != "num_num":
        v = token_value(e.family, e.token)
        return (e.family == "roman", v) if v else None
    return None


def _position_candidate(e: TocEntry, lines: list[LineRef], page: int) -> int | None:
    """위치 기반 대응(방법 5): 목차 쪽 번호와 꼬리말이 정확히 같은 쪽의 맨 위 POSITION_TOP_LINES줄 안에 항목과
    같은 번호가 있으면 그 줄(번호만 있는 줄이면 이웃한 제목 줄)의 g. 글자가 달라도 쪽과 번호가 함께 맞는 줄 —
    목차와 본문의 제목 문구가 다른 판형(2022-65 Ⅴ장 절), 본문 쪽 오타(2024-39 p19). 호출 조건(창 안 글자 후보 없음)은
    anchor_entries가 본다."""
    num = _entry_number(e)
    if num is None:
        return None
    top = [k for k in range(len(lines)) if lines[k].page == page and not lines[k].in_table][:POSITION_TOP_LINES]
    for i, k in enumerate(top):
        text = lines[k].text.strip()
        fam, tok, rest = tokenize(text)
        if fam in ("none", "num_num"):
            continue
        v = token_value(fam, tok)
        if not v or ((fam == "roman"), v) != num:
            continue
        if rest.strip() and not TOKEN_ONLY_RE.match(text):
            return k                      # 번호 + 글자가 한 줄
        for j in (i + 1, i - 1):          # 번호만 있는 줄 — 이웃한 제목 줄(번호 없는 줄)
            if 0 <= j < len(top) and tokenize(lines[top[j]].text)[0] == "none":
                return top[j]
        return k
    return None


def anchor_entries(scans, part, entries: list[TocEntry], pmap: PageMap, domain_start: int,
                   lines: list[LineRef] | None = None, *, front_guard: bool = True,
                   front_penalty: bool = True, window_fn=None,
                   global_sim: bool = False) -> tuple[list[Anchor], dict]:
    """목차 항목(앞부속 제외)을 본문 줄에 대응. 창(예측 쪽 ±1) 후보 + 전역 후보를 모아 목차 순서를 지키는
    최장 사슬을 고른다. 반환: (항목별 Anchor, 통계).

    Phase 2(판독 파일 레인): 본문 시작을 이미 아는 호출자는 front_guard·front_penalty를 끄고, 창을 직접 준다 —
    window_fn(entry) → (lo, hi, target) | None (0-based PDF 쪽; None이면 pmap 오프셋 창으로)."""
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
        guarded = front_guard and n_lead < FRONT_GUARD_ENTRIES and e.depth == 1 and e.kind == "normal"
        if guarded:
            n_lead += 1
        found: dict[int, tuple[int, bool]] = {}
        e.target = None
        win = window_fn(e) if window_fn is not None else None
        exact_page = False
        if win is not None:
            w_lo, w_hi, e.target = win[:3]
            exact_page = len(win) > 3 and bool(win[3])
        elif pmap.mappable and e.printed_page is not None:
            e.target = e.printed_page + pmap.offset
            w_lo, w_hi = e.target - 1, e.target + 1
        if e.target is not None:
            for k in range(n_lines):
                if not (w_lo <= lines[k].page <= w_hi):
                    continue
                t = _match_tier(e, lines, k, with_sim=True, in_window=True)
                if t is not None:
                    found[k] = (t, True)
            if exact_page and not found:
                g = _position_candidate(e, lines, e.target)
                if g is not None:
                    found[g] = (5, True)
        for k in range(n_lines):
            if k in found:
                continue
            t = _match_tier(e, lines, k, with_sim=global_sim)
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
    for p in range(part[0], part[1] + 1) if front_penalty else ():
        if scans[p].footer_arabic == 1:
            front_end = p

    # 판독 파일 레인(front_guard 꺼짐 = 본문 영역이 확정된 호출): 목록 쪽의 하위 항목 후보는 창 안일 때만 남긴다 —
    # 구분 쪽의 절 목록이 "대응 수 최대" 사슬을 끌어가 실제 헤딩(뒤쪽, 창 안)을 밀어내는 것을 막는다(2024-41 Ⅳ장).
    if not front_guard and list_pages:
        for ei, cl in enumerate(cands):
            if targets[ei].depth >= 2 and targets[ei].target is not None:
                cands[ei] = [c for c in cl if c[2] or lines[c[0]].page not in list_pages]

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
            anchors.append(Anchor(e.order, tier, ln.page, ln.j, g, ln.text, n_c, ambiguous, delta, False))
            stats[tier] += 1
        else:
            anchors.append(Anchor(e.order, "none", None, None, None, "", n_c, ambiguous, None, n_c > 0))
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


# ---------------------------------------------------------------------------
# 6. 장 문법 없는 합본 분할 판정
# ---------------------------------------------------------------------------


