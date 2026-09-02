"""파이프라인 현황판 — 읽기 전용 보조 도구 (파이프라인 4단계 외부).

pdfs/ · reports/ · reports.db · master_index.md의 존재·내용·mtime에서만 상태를
파생 계산한다(별도 상태 파일 없음). 보고서별 추출/요약/보고서요약/검증 현황과
잔여 LLM 호출 수, 파생물 신선도를 표로 출력한다.

- 유닛 계산은 annotate.py와 동일한 mdio.load_report() 재사용 — 잔여 호출 수가
  annotate --scan의 expected_calls와 항상 일치한다.
- 검증 컬럼은 frontmatter 스탬프 사다리(verified_extract/verified_annotate):
  없음=미검증, extract만=extract, 둘 다=annotate(= verify.py D 품질 판정까지 통과).
  스탬프 기록은 verify.py 소관. extract가 frontmatter를 재생성하면 스탬프가 소멸해
  자동 미검증 리셋, annotate가 요약을 새로 쓰면 verified_annotate만 소멸한다.
- 커버리지 완비(잔여 0)인데 verified_annotate가 없는 파일은 "verify 승격 대기"
  비고로 할 일에 반영된다(verify 실행 시 D 품질 판정 후 스탬프 기록).
- DB 컬럼은 reports.db 내 files 원장(build_db 증분 동기화 기록)과 .md sha256을
  대조해 파일별 동기화/미반영을 표시한다 — mtime 추정이 아니라 내용 기준.
- PDF↔.md 신선도 비교는 mtime 기반 best-effort — Windows 복사는 LastWriteTime을
  보존하므로 파일 교체를 놓칠 수 있다. FAT/exFAT 2초 정밀도만큼 허용오차를 둔다.
- 종료 코드: 0 = 완전 동기화, 1 = 할 일·경고 있음, 2 = 사용 오류.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import mdio
from extract import derive_report_id_any

# .md 파일명 = {report_id}.md — 단독본(2025-02) 또는 합본 파트(2025-17_01).
# 슬러그 rid는 밑줄을 포함하지 않으므로 '_NN' 꼬리는 언제나 합본 파트 접미사다.
PART_SUFFIX_RE = re.compile(r"^(.+)_(\d{2})$")
MTIME_TOLERANCE = 2.0  # FAT/exFAT mtime 정밀도


@dataclass
class Row:
    report_id: str
    extracted: str = "완료"
    summary: str = "-"  # "M/N"
    remaining: int = 0
    report_summary: str = "-"  # abstract | 완료 | 대기 | -
    verify: str = "미검증"  # 미검증 | extract | annotate
    db: str = "-"  # 동기화 | 미반영 | -
    notes: list[str] = field(default_factory=list)


def scan_pdfs(pdf_dir: Path) -> tuple[dict[str, Path], dict[str, list[str]]]:
    """({base_id: pdf 경로}, 충돌 그룹). 관리번호 실패 시 슬러그 유도 — extract와 동일 함수 공유.

    같은 base_id로 유도되는 파일이 2개 이상이면(중복 다운로드·파일명 충돌) 매핑에는
    사전순 첫 파일만 남기고(extract 프리플라이트와 동일 선택) 그룹을 따로 반환한다 —
    종전 dict 컴프리헨션은 충돌을 조용히 덮어써 현황판 집계를 오염시켰다.
    """
    groups: dict[str, list[Path]] = {}
    for p in sorted(pdf_dir.glob("*.pdf")):
        groups.setdefault(derive_report_id_any(str(p)), []).append(p)
    dups = {rid: [p.name for p in ps] for rid, ps in groups.items() if len(ps) > 1}
    return {rid: ps[0] for rid, ps in groups.items()}, dups


def build_md_row(path: Path, base_id: str, pdf: Path | None, args) -> Row:
    row = Row(report_id=path.stem)
    try:
        st = mdio.load_report(path, min_chars=args.min_chars, max_chars=args.max_chars)
    except Exception as e:
        row.extracted = "파싱 실패"
        row.notes.append(str(e))
        return row

    if any(st.base[i] == "structure: flat" for i in range(1, st.fm.end_line)):
        row.notes.append("플랫 구조(구간 청킹)")

    fm, units, existing = st.fm, st.units, st.existing
    done = sum(1 for u in units if u.hid in existing)
    new = len(units) - done
    # 잔여 호출 산식 = annotate.run_scan()의 expected_calls와 동일
    row.remaining = new + (
        1 if fm.abstract_empty and mdio.REPORT_KEY not in existing and units else 0
    )
    row.summary = f"{done}/{len(units)}"

    if not fm.abstract_empty:
        row.report_summary = "abstract"
    elif mdio.REPORT_KEY in existing:
        row.report_summary = "완료"
    elif units:
        row.report_summary = "대기"

    if fm.verified_annotate:
        row.verify = "annotate"
        if not fm.verified_extract:
            row.notes.append("스탬프 사다리 위반 — verified_annotate만 존재")
        if row.remaining:
            row.notes.append("검증 후 변경 — 재검증 필요")
    elif fm.verified_extract:
        row.verify = "extract"
        if units and not row.remaining:
            row.notes.append("verify 승격 대기 — D 품질 판정 후 verified_annotate 기록")

    if fm.report_id != path.stem:
        row.notes.append(f"frontmatter report_id({fm.report_id}) ≠ 파일명")
    if not st.roundtrip_ok():
        row.notes.append("요약 배치 규약 불일치 — annotate가 이 파일을 건너뜀")
    if pdf is None:
        row.notes.append("원본 PDF 없음")
    elif pdf.stat().st_mtime > path.stat().st_mtime + MTIME_TOLERANCE:
        row.notes.append("PDF가 .md보다 최신 — 재추출 검토(--force 시 요약 소실 주의)")
    return row


def read_db_ledger(db_path: Path) -> dict[str, str] | None:
    """reports.db의 files 원장(build_db 증분 동기화 기록). DB 없음·읽기 실패 시 None."""
    if not db_path.exists():
        return None
    try:
        con = sqlite3.connect(db_path)
        try:
            return dict(con.execute("SELECT filepath, content_hash FROM files"))
        finally:
            con.close()
    except sqlite3.Error:
        return None


def db_sync_line(db_path: Path, ledger: dict[str, str] | None,
                 digests: dict[str, str]) -> tuple[str, bool]:
    """reports.db 신선도 한 줄 — mtime이 아니라 파일 해시 대조. (표시문, 할 일 여부)."""
    label = "reports.db     "
    if not db_path.exists():
        return f"{label}: 없음 — src/build_db.py 실행 필요", True
    if ledger is None:
        return f"{label}: files 원장 읽기 실패 — src/build_db.py --rebuild 검토", True
    stale = sum(1 for fp, d in digests.items() if ledger.get(fp) != d)
    orphan = len(set(ledger) - set(digests))
    if stale or orphan:
        return f"{label}: 미반영 {stale + orphan}건 — src/build_db.py 실행 필요", True
    return f"{label}: 최신 (해시 동기화)", False


def artifact_line(label: str, path: Path, builder: str, newest_md: float | None) -> tuple[str, bool]:
    """파생물 신선도 한 줄. (표시문, 할 일 여부). 빌더가 .md를 읽은 뒤 쓰므로 >=면 최신."""
    if not path.exists():
        return f"{label}: 없음 — {builder} 실행 필요", True
    if newest_md is not None and path.stat().st_mtime < newest_md:
        return f"{label}: 재빌드 필요 — .md 변경이 더 최신", True
    return f"{label}: 최신", False


def disp_width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def pad(s: str, width: int) -> str:
    return s + " " * max(0, width - disp_width(s))


def render(rows: list[Row], warnings: list[str], artifact_lines: list[str],
           extract_needed: int, remaining_total: int) -> None:
    headers = ["report_id", "추출", "요약", "잔여 호출", "보고서요약", "검증", "DB", "비고"]
    cells = [
        [r.report_id, r.extracted, r.summary, str(r.remaining), r.report_summary,
         r.verify, r.db, "; ".join(r.notes)]
        for r in rows
    ]
    widths = [max(disp_width(h), *(disp_width(c[i]) for c in cells)) if cells else disp_width(h)
              for i, h in enumerate(headers)]
    print("== 보고서별 현황 ==")
    print("  ".join(pad(h, w) for h, w in zip(headers, widths)).rstrip())
    for c in cells:
        print("  ".join(pad(v, w) for v, w in zip(c, widths)).rstrip())

    print()
    print("== 파생물 ==")
    for line in artifact_lines:
        print(line)

    if warnings:
        print()
        print("== 경고 ==")
        for w in warnings:
            print(f"- {w}")

    done_total = sum(int(r.summary.split("/")[0]) for r in rows if "/" in r.summary)
    units_total = sum(int(r.summary.split("/")[1]) for r in rows if "/" in r.summary)
    verify_counts = {k: sum(1 for r in rows if r.verify == k) for k in ("미검증", "extract", "annotate")}
    note_count = sum(len(r.notes) for r in rows) + len(warnings)
    print()
    print("== 합계 ==")
    print(
        f"보고서 {len(rows)} (추출 필요 {extract_needed}) · 유닛 {done_total}/{units_total} · "
        f"잔여 호출 {remaining_total} · 검증: 미검증 {verify_counts['미검증']} / "
        f"extract {verify_counts['extract']} / annotate {verify_counts['annotate']} · "
        f"경고 {note_count}"
    )


@dataclass
class StatusData:
    """collect_status() 결과 — CLI 렌더(main)와 serve.py 관리 API가 공유하는 단일 소스."""
    rows: list[Row]
    new_pdfs: list[str]  # 추출 필요 base_id (= "추출 필요" 행)
    artifacts: list[tuple[str, bool]]  # (표시문, 할 일 여부) — reports.db, master_index.md 순
    remaining_total: int
    note_count: int
    todo: bool
    dup_ids: dict[str, list[str]] = field(default_factory=dict)  # rid → 같은 rid로 유도된 PDF들


def collect_status(args) -> StatusData:
    """아티팩트에서 현황 파생 계산 — 디렉터리 존재 검증은 호출자 몫."""
    pdfs, dup_ids = scan_pdfs(Path(args.pdf_dir))
    db_path = Path(args.db)
    ledger = read_db_ledger(db_path)

    rows: list[Row] = []
    covered_bases: set[str] = set()
    md_mtimes: list[float] = []
    md_digests: dict[str, str] = {}
    for p in sorted(Path(args.reports_dir).glob("*.md")):
        m = PART_SUFFIX_RE.match(p.stem)
        base = m.group(1) if m else p.stem
        covered_bases.add(base)
        md_mtimes.append(p.stat().st_mtime)
        fp = p.as_posix()
        md_digests[fp] = hashlib.sha256(p.read_bytes()).hexdigest()
        row = build_md_row(p, base, pdfs.get(base), args)
        if ledger is not None:
            row.db = "동기화" if ledger.get(fp) == md_digests[fp] else "미반영"
        rows.append(row)

    new_pdfs = sorted(set(pdfs) - covered_bases)
    for base in new_pdfs:
        rows.append(Row(report_id=base, extracted="추출 필요", verify="-"))
    rows.sort(key=lambda r: r.report_id)

    newest_md = max(md_mtimes) if md_mtimes else None
    db_line, db_todo = db_sync_line(db_path, ledger, md_digests)
    idx_line, idx_todo = artifact_line("master_index.md", Path(args.index), "src/build_index.py", newest_md)

    remaining_total = sum(r.remaining for r in rows)
    note_count = sum(len(r.notes) for r in rows)
    todo = bool(new_pdfs or remaining_total or db_todo or idx_todo or note_count or dup_ids)
    return StatusData(rows, new_pdfs, [(db_line, db_todo), (idx_line, idx_todo)],
                      remaining_total, note_count, todo, dup_ids)


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="파이프라인 현황판 (읽기 전용 — 아티팩트에서 상태 파생).")
    parser.add_argument("--pdf-dir", default="pdfs", help="원본 PDF 디렉터리 (기본: pdfs)")
    parser.add_argument("--reports-dir", default="reports", help="원본 .md 디렉터리 (기본: reports)")
    parser.add_argument("--db", default="reports.db", help="FTS5 DB 경로 (기본: reports.db)")
    parser.add_argument("--index", default="master_index.md", help="마스터 인덱스 경로 (기본: master_index.md)")
    parser.add_argument("--min-chars", type=int, default=200, help="유닛 최소 크기 (annotate와 동일해야 함)")
    parser.add_argument("--max-chars", type=int, default=4000, help="유닛 최대 크기 (annotate와 동일해야 함)")
    args = parser.parse_args()

    pdf_dir = Path(args.pdf_dir)
    reports_dir = Path(args.reports_dir)
    if not pdf_dir.is_dir() or not reports_dir.is_dir():
        missing = pdf_dir if not pdf_dir.is_dir() else reports_dir
        print(f"디렉터리가 없습니다: {missing}", file=sys.stderr)
        sys.exit(2)

    st = collect_status(args)
    warnings = [
        f"report_id 충돌: {rid} ← {', '.join(files)} — 중복 삭제 또는 파일명 조정 필요"
        for rid, files in sorted(st.dup_ids.items())
    ]
    render(st.rows, warnings, [t for t, _ in st.artifacts], len(st.new_pdfs), st.remaining_total)
    sys.exit(1 if st.todo else 0)


if __name__ == "__main__":
    main()
