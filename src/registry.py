"""관리번호 등록부(report_ids.tsv) 읽기 — 파이프라인 전 단계가 공유하는 **유일한 rid 소스**.

report_id는 파일명에서 유도하지 않는다(2026-09-04 결정 — 파일명 표기가 제각각이라
표지에 인쇄된 관리번호 하나로 통일). `src/register.py`(1단계)가 표지를 읽어 이 표를
채우고, extract·verify·promote·status·serve는 여기서 조회만 한다. 표에 없거나 rid가
공란인 PDF는 어느 단계도 지나지 못한다(미등록 = FAIL — 사람이 표에 기입).

- 키 = PDF 파일명(NFC, 디렉터리 무관). 열 = file · report_id · source(cover|manual) ·
  cover_title · note. 탭 구분, UTF-8, 헤더 1줄.
- rid 형식: ASCII `[A-Za-z0-9-]`만(밑줄은 합본 파트 접미 `_NN` 전용, 한글·글롭 메타문자
  불가). 표준형 stem = `YYYY-NN[-vN][-b][_NN]` (권수 → 중복 → 파트 순서 고정) —
  verify의 연도 대조는 표준형에만 적용.
- 캐시는 파일 mtime 기준 — 장수 프로세스(serve)가 사용자의 표 편집을 새로고침에서 본다.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "report_ids.tsv"
COLUMNS = ["file", "report_id", "source", "cover_title", "note"]
SOURCES = ("cover", "manual")

RID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
STEM_RE = re.compile(r"^(\d{4}-\d{2})(?:-v\d{1,2})?(?:-[b-z])?(?:_\d{2})?$")


@dataclass
class Row:
    file: str
    report_id: str = ""
    source: str = ""
    cover_title: str = ""
    note: str = ""


def norm_name(path) -> str:
    """등록부 키 — 경로의 파일명 부분을 NFC로."""
    return unicodedata.normalize("NFC", Path(str(path)).name)


def read_rows(path: Path = REGISTRY_PATH) -> list[Row]:
    """표 전체(파일 없음 = 빈 표). 열이 모자라면 공란, 남으면 무시."""
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
                            col("cover_title"), col("note")))
    return rows


_cache: dict[str, tuple[float, dict[str, Row]]] = {}


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Row]:
    """{NFC 파일명: Row} — mtime이 같으면 캐시 재사용."""
    p = Path(path)
    mtime = p.stat().st_mtime if p.exists() else -1.0
    key = str(p.resolve())
    hit = _cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    table = {r.file: r for r in read_rows(p)}
    _cache[key] = (mtime, table)
    return table


def rid_for(pdf_path, path: Path = REGISTRY_PATH) -> str | None:
    """PDF → rid. 표에 없거나 공란·형식 위반이면 None(= 미등록)."""
    row = load_registry(path).get(norm_name(pdf_path))
    if row is None or not row.report_id or not RID_RE.match(row.report_id):
        return None
    return row.report_id


def rid_reason(pdf_path, path: Path = REGISTRY_PATH) -> str:
    """미등록 사유(사람이 읽는 문장) — 표에 없음 / 공란(+note) / 형식 위반."""
    row = load_registry(path).get(norm_name(pdf_path))
    if row is None:
        return "report_ids.tsv에 없음 — python src/register.py 실행"
    if not row.report_id:
        return "report_id 공란 — 표에 기입 필요" + (f" ({row.note})" if row.note else "")
    if not RID_RE.match(row.report_id):
        return f"report_id 형식 위반({row.report_id}) — ASCII 영숫자·하이픈만, 밑줄·한글 불가"
    return ""


def unregistered(pdf_dir, path: Path = REGISTRY_PATH) -> list[tuple[str, str]]:
    """pdfs/ 직하 PDF 중 미등록 [(파일명, 사유)] — 파일명 정렬."""
    out = []
    for p in sorted(Path(pdf_dir).glob("*.pdf")):
        if rid_for(p, path) is None:
            out.append((norm_name(p), rid_reason(p, path)))
    return out


def orphans(pdf_dir, path: Path = REGISTRY_PATH) -> list[str]:
    """표에는 있는데 pdfs/에 파일이 없는 행의 파일명(사용자가 PDF를 지운 경우 — 행은 사람이 정리)."""
    present = {norm_name(p) for p in Path(pdf_dir).glob("*.pdf")}
    return sorted(f for f in load_registry(path) if f not in present)
