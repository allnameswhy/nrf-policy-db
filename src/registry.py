"""관리번호 등록부(report_ids.tsv) 읽기 — 파이프라인 전 단계가 공유하는 **유일한 rid 소스**.

report_id는 파일명에서 유도하지 않는다(2026-09-04 결정 — 파일명 표기가 제각각이라
표지에 인쇄된 관리번호 하나로 통일). `src/register.py`(1단계)가 표지를 읽어 이 표를
채우고, extract·verify·promote·status·serve는 여기서 조회만 한다. 표에 없거나 rid가
공란인 행이 하나라도 있는 PDF는 어느 단계도 지나지 못한다(미등록 = FAIL — 사람이 기입).

- 열(v4, 2026-09-08) = file · pages · report_id · source(cover|manual) · cover_title ·
  registered(rid를 적은 날, ISO) · note. 탭 구분, UTF-8, 헤더 1줄. **행 키 = (file, pages)**:
  file = PDF 파일명(NFC, 디렉터리 무관), pages = 합본 파트의 쪽 범위(`139-233`, 1-based 양끝
  포함; 단독본은 공란 = 파일 전체). 합본은 register가 파트마다 행을 만든다 — 등록 단위 =
  문서(파트도 각자 rid), `_NN` 접미는 폐지.
- rid 형식: ASCII `[A-Za-z0-9-]`만(밑줄·한글·글롭 메타문자 불가). 표준형 =
  `YYYY-NN[-b][-vN]` — 글자 = 같은 번호의 **다른 독립 보고서**(-b, -c…), v = 그 보고서의
  **권·부록·별권**(-v1, -v2…). `2019-31-b-v1` = -b 보고서의 별권. verify의 연도 대조는
  표준형에만 적용. 번호 미상 규약 `YYYY-00-vN`·`0000-00-vN`(register.set_family).
- note는 register가 쓰는 고정 세그먼트(` · ` 구분)라 `parse_note`가 되읽는다 — 공란 행의
  표지 번호(가족 묶음·정렬)와 권 신호·표지 단서(카드 제안).
- 캐시는 파일 mtime 기준 — 장수 프로세스(serve)가 표 변경을 새로고침에서 본다.
- `pdfs/hold/`(2026-09-07) = 아직 적재하지 않을 **보류 PDF**. 파이프라인·status·/admin의
  PDF 스캔은 `pdfs/` 직하만 보므로 보류 파일은 어느 단계에도 잡히지 않고, **등록부에도 없다**
  (register는 hold 경로 인자를 거부하고, 표에 남은 보류 행은 `orphans`·register `--check`가
  "행 삭제 필요"로 표시한다; `held_names`는 그 표시용). 등록은 반출(hold → pdfs/) 후
  register 단계에서 — 반출 순서는 웨이브 계획(WAVE_PLAN §3) 동안만 절차에 포함.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "report_ids.tsv"
COLUMNS = ["file", "pages", "report_id", "source", "cover_title", "registered", "note"]
SOURCES = ("cover", "manual")

RID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
# 표준형 stem: 그룹1 = YYYY-NN(연도 대조·가족 키), 그룹2 = 다른 보고서 글자, 그룹3 = 권 번호
STEM_RE = re.compile(r"^(\d{4}-\d{2})(?:-([b-z]))?(?:-v(\d{1,2}))?$")
PAGES_RE = re.compile(r"^(\d+)-(\d+)$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NOTE_SEP = " · "


@dataclass
class Row:
    file: str
    report_id: str = ""
    source: str = ""
    cover_title: str = ""
    note: str = ""
    pages: str = ""  # 합본 파트 쪽 범위 "lo-hi"(1-based 양끝 포함), 단독본 공란
    registered: str = ""  # rid를 적은 날(ISO), rid 공란이면 공란

    @property
    def key(self) -> tuple[str, str]:
        return (self.file, self.pages)


def norm_name(path) -> str:
    """등록부 키 — 경로의 파일명 부분을 NFC로."""
    return unicodedata.normalize("NFC", Path(str(path)).name)


def parse_pages(s: str) -> tuple[int, int] | None:
    """'139-233'(1-based 양끝 포함) → (138, 232) 0-based. 공란 → None. 형식 위반 → ValueError."""
    s = (s or "").strip()
    if not s:
        return None
    m = PAGES_RE.match(s)
    if not m or int(m.group(1)) < 1 or int(m.group(2)) < int(m.group(1)):
        raise ValueError(f"pages 형식 위반: {s!r}")
    return int(m.group(1)) - 1, int(m.group(2)) - 1


def format_pages(rng: tuple[int, int]) -> str:
    return f"{rng[0] + 1}-{rng[1] + 1}"


def family_of(rid: str) -> str:
    """표준형 rid의 가족 키 YYYY-NN(비표준·공란은 '')."""
    m = STEM_RE.match(rid or "")
    return m.group(1) if m else ""


def _pages_sort(r: Row) -> int:
    try:
        rng = parse_pages(r.pages)
    except ValueError:
        return 10 ** 9
    return -1 if rng is None else rng[0]


def read_rows(path: Path = REGISTRY_PATH) -> list[Row]:
    """표 전체(파일 없음 = 빈 표). 열은 헤더 이름으로 찾는다 — 모자라면 공란, 남으면 무시
    (v3의 5열 파일도 그대로 읽힌다: pages·registered 공란)."""
    if not Path(path).exists():
        return []
    rows: list[Row] = []
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE)
        header = next(reader, None)
        if header is None:
            return []
        idx = {h.strip(): i for i, h in enumerate(header)}
        for rec in reader:
            if not rec or not any(c.strip() for c in rec):
                continue

            def col(name: str) -> str:
                i = idx.get(name)
                return rec[i].strip() if i is not None and i < len(rec) else ""

            rows.append(Row(norm_name(col("file")), col("report_id"), col("source"),
                            col("cover_title"), col("note"), col("pages"), col("registered")))
    return rows


_cache: dict[str, tuple[float, dict[str, list[Row]]]] = {}


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, list[Row]]:
    """{NFC 파일명: [행…](pages 순, 단독 행 먼저)} — mtime이 같으면 캐시 재사용."""
    p = Path(path)
    mtime = p.stat().st_mtime if p.exists() else -1.0
    key = str(p.resolve())
    hit = _cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    table: dict[str, list[Row]] = {}
    for r in read_rows(p):
        table.setdefault(r.file, []).append(r)
    for rows in table.values():
        rows.sort(key=_pages_sort)
    _cache[key] = (mtime, table)
    return table


def rows_for(pdf_path, path: Path = REGISTRY_PATH) -> list[Row]:
    return load_registry(path).get(norm_name(pdf_path), [])


def _rows_problem(rows: list[Row]) -> str:
    """파일의 행들이 파이프라인에 쓸 수 있는 상태가 아니면 사유 문장, 아니면 ''."""
    if not rows:
        return "report_ids.tsv에 없음 — python src/register.py 실행"
    ranges: list[tuple[int, int]] = []
    whole = 0
    for i, r in enumerate(rows, 1):
        try:
            rng = parse_pages(r.pages)
        except ValueError:
            return f"pages 형식 위반({r.pages}) — python src/register.py --drop 후 재등록"
        where = f"파트 {i}({r.pages}쪽) " if rng else ""
        if not r.report_id:
            return f"{where}report_id 공란 — 표에 기입 필요" + (f" ({r.note})" if r.note else "")
        if not RID_RE.match(r.report_id):
            return f"{where}report_id 형식 위반({r.report_id}) — ASCII 영숫자·하이픈만, 밑줄·한글 불가"
        if rng is None:
            whole += 1
        else:
            ranges.append(rng)
    if whole > 1 or (whole and ranges):
        return "pages 행 혼재(단독 행과 파트 행) — python src/register.py --drop 후 재등록"
    for a, b in zip(ranges, ranges[1:]):
        if b[0] <= a[1]:
            return "pages 범위 겹침·역순 — python src/register.py --drop 후 재등록"
    return ""


def parts_for(pdf_path, path: Path = REGISTRY_PATH) -> list[tuple[str, tuple[int, int] | None]]:
    """PDF → [(rid, 0-based 쪽 범위 | None=파일 전체)] 파트 순. 표에 없거나 어느 행이든 공란·
    형식 위반이면 [](= 미등록 — 파트 하나가 비어도 파일 전체 차단). 예외 없음."""
    rows = rows_for(pdf_path, path)
    if _rows_problem(rows):
        return []
    return [(r.report_id, parse_pages(r.pages)) for r in rows]


def rid_for(pdf_path, path: Path = REGISTRY_PATH) -> str | None:
    """PDF → 대표 rid(첫 행). 미등록이면 None. 파트별 작업은 parts_for로."""
    parts = parts_for(pdf_path, path)
    return parts[0][0] if parts else None


def rid_reason(pdf_path, path: Path = REGISTRY_PATH) -> str:
    """미등록 사유(사람이 읽는 문장) — 표에 없음 / 공란(+note) / 형식 위반 / pages 문제."""
    return _rows_problem(rows_for(pdf_path, path))


def rid_file_map(path: Path = REGISTRY_PATH) -> dict[str, str]:
    """{rid: 파일명} — rid가 유효한 모든 행(파트 포함). 현황·카드가 .md 스템을 PDF로 묶을 때."""
    return {r.report_id: f for f, rows in load_registry(path).items()
            for r in rows if r.report_id and RID_RE.match(r.report_id)}


def unregistered(pdf_dir, path: Path = REGISTRY_PATH) -> list[tuple[str, str]]:
    """pdfs/ 직하 PDF 중 미등록 [(파일명, 사유)] — 파일명 정렬."""
    out = []
    for p in sorted(Path(pdf_dir).glob("*.pdf")):
        if rid_for(p, path) is None:
            out.append((norm_name(p), rid_reason(p, path)))
    return out


HOLD_SUBDIR = "hold"


def held_names(pdf_dir) -> set[str]:
    """`pdfs/hold/` 직하 PDF 파일명(NFC) — 보류(미적재)분. 등록 대상이 아니다(표에 있으면 삭제 대상)."""
    return {norm_name(p) for p in Path(pdf_dir, HOLD_SUBDIR).glob("*.pdf")}


def is_held(pdf_path, pdf_dir) -> bool:
    """경로가 `pdfs/hold/` 직하인가 — register가 보류 파일 등록을 거부할 때 사용."""
    try:
        return Path(pdf_path).resolve().parent == Path(pdf_dir, HOLD_SUBDIR).resolve()
    except OSError:
        return False


def orphans(pdf_dir, path: Path = REGISTRY_PATH) -> list[str]:
    """표에는 있는데 pdfs/ 직하에 파일이 없는 행의 파일명(파일 단위) — 지운 파일이든 hold/로
    보류한 파일이든 행은 사람이 정리(register --drop / /admin 행 삭제)."""
    present = {norm_name(p) for p in Path(pdf_dir).glob("*.pdf")}
    return sorted(f for f in load_registry(path) if f not in present)


# ---- note 세그먼트 되읽기 (register가 쓰는 고정 형식) ----

COVER_NO_RE = re.compile(r"^표지 번호 (\d{4}-\d{2})$")
VOL_SEG_RE = re.compile(r"^권 신호 ")
CUE_SEG_RE = re.compile(r"^표지 단서 (.+)$")
VOL_NUM_RE = re.compile(r"제(\d{1,2})권")


@dataclass
class NoteInfo:
    cover_no: str = ""  # 표지에서 읽은 YYYY-NN(rid 공란 행의 가족 키)
    vols: list[int] = field(default_factory=list)  # 표지·속표지 제N권 신호
    cues: list[str] = field(default_factory=list)  # 표지 단서 단어(첨부·별권·로드맵…)
    segments: list[str] = field(default_factory=list)


def parse_note(note: str) -> NoteInfo:
    info = NoteInfo(segments=[s.strip() for s in (note or "").split(NOTE_SEP) if s.strip()])
    for seg in info.segments:
        m = COVER_NO_RE.match(seg)
        if m:
            info.cover_no = m.group(1)
            continue
        if VOL_SEG_RE.match(seg):
            info.vols = [int(v) for v in VOL_NUM_RE.findall(seg)]
            continue
        m = CUE_SEG_RE.match(seg)
        if m:
            info.cues = [c.strip() for c in m.group(1).split("·") if c.strip()]
    return info


def note_cover_no(note: str) -> str:
    return parse_note(note).cover_no


def family_key(row: Row) -> str:
    """행의 가족 키 — rid 표준형이면 그 YYYY-NN, 공란이면 note의 표지 번호, 없으면 ''."""
    return family_of(row.report_id) or (note_cover_no(row.note) if not row.report_id else "")


def rid_sort_key(row: Row) -> tuple:
    """표 정렬 = rid 자연 순서: 표준 rid(연도·번호·글자·권) → 공란+표지 번호(가족 바로 뒤) →
    비표준 rid(문자열) → 번호 없는 공란(파일명). `0000-00-vN`·`YYYY-00-vN`은 자연히 앞."""
    rid = row.report_id
    m = STEM_RE.match(rid) if rid else None
    ps = _pages_sort(row)
    if m:
        y, n = m.group(1).split("-")
        letter = (ord(m.group(2)) - ord("a")) if m.group(2) else 0
        vol = int(m.group(3)) if m.group(3) else 0
        return (0, int(y), int(n), 0, letter, vol, "", row.file, ps)
    if rid:
        return (1, 0, 0, 0, 0, 0, rid, row.file, ps)
    cover = note_cover_no(row.note)
    if cover:
        y, n = cover.split("-")
        return (0, int(y), int(n), 1, 0, 0, "", row.file, ps)
    return (2, 0, 0, 0, 0, 0, "", row.file, ps)
