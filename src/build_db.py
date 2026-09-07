"""reports/*.md → reports.db FTS5 색인 (파이프라인 3단계, 증분 동기화).

PROJECT_NOTES.md §6 참조.

- FTS 행 = annotate와 동일한 mdio.split_units() 유닛 + 참고문헌·부록·첨부 장
  (유닛 분할 제외분 — 요약 공란의 장 단위 1행, recall 안전망).
- 증분 동기화(2026-08 규약): DB 내 files 원장(filepath, content_hash)과 .md의
  sha256을 대조해 신규·변경 파일만 행 삭제 후 재삽입, 사라진 파일 행은 제거.
  동기화 기록이 .md가 아니라 DB에 있으므로 DB 삭제·신규 클론 시 자연히 전체
  빌드로 수렴한다(원본은 파생물 상태를 기록하지 않는다).
- 스탬프 3단 게이트(2026-09-07): 변경 감지된 파일에 verified_register·verified_extract·
  verified_annotate 중 하나라도 없으면 싣지 않고 기존 행도 삭제한다 — 재추출로
  section_id가 바뀌었을 수 있어 죽은 참조를 차단. 무변경 파일은 재평가하지 않는다
  (스탬프가 .md 바이트 안에 있어 스탬프 변화 = 해시 변화 = 변경 파일). 따라서
  "files 원장 해시 == 현재 .md 해시"는 곧 세 스탬프가 찍힌 파일이 그대로 색인돼
  있다는 뜻이다(status.is_loaded·/browse가 이 하나만 본다).
  수정 경로: verify → build_db 재실행. 게이트 스킵이 있으면 exit 1.
- --rebuild: DB 파일 삭제 후 전체 재구축(파생물 철학의 안전망).
- fold_text(): 공백 전제거 — body_fold 색인과 검색 쿼리 fold의 대칭 규칙은 이
  모듈이 소유한다(검색 흐름이 import). verify·extract의 fold_g(불릿·표 구두점
  까지 제거)와는 다른 함수다.
"""

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path

import mdio

# §6 스키마 — body_fold는 body 바로 뒤(컬럼 5)라 snippet(sections, 4, …) 예시 불변
SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS sections USING fts5(
    report_title,
    chapter,
    section,
    summary,
    body,
    body_fold,
    section_id UNINDEXED,
    filepath   UNINDEXED,
    year       UNINDEXED,
    lead_researcher,
    institution,
    tokenize='trigram'
);
CREATE TABLE IF NOT EXISTS files(
    filepath TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL
);
"""

INSERT_SQL = "INSERT INTO sections VALUES (?,?,?,?,?,?,?,?,?,?,?)"


def fold_text(s: str) -> str:
    """공백 전제거(개행 포함) — body_fold 색인·쿼리 대칭 규칙."""
    return "".join(s.split())


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def report_rows(state: mdio.ReportState, filepath: str) -> list[tuple]:
    """한 보고서의 sections 행 전체 — heading line 순(= 문서 순서, 목차 조회 (b) 전제)."""
    fm = state.fm
    items: list[tuple[int, mdio.Unit, str]] = [
        (u.heading_line, u, state.existing.get(u.hid, "")) for u in state.units
    ]
    for r in state.roots:
        if r.depth == 1 and mdio.is_excluded_chapter(r.text):
            synth = mdio.Unit(r.hid, "subtree", 1, r.line, [r.text], r.line + 1, r.end_line, 0)
            items.append((r.line, synth, ""))
    items.sort(key=lambda t: t[0])

    rows = []
    for _, unit, summary in items:
        body = mdio.unit_text(unit, state.base)
        rows.append((
            fm.title,
            unit.heading_path[0],
            " > ".join(unit.heading_path[1:]),
            summary,
            body,
            fold_text(body),
            unit.hid,
            filepath,
            fm.year,
            fm.lead_researcher,
            fm.institution,
        ))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="reports/*.md에서 reports.db(FTS5)를 증분 동기화한다.")
    parser.add_argument("--db", default="reports.db", help="출력 DB 경로 (기본: reports.db)")
    parser.add_argument("--reports-dir", default="reports", help="원본 .md 디렉터리 (기본: reports)")
    parser.add_argument("--min-chars", type=int, default=200, help="유닛 최소 크기 (annotate와 동일해야 함)")
    parser.add_argument("--max-chars", type=int, default=4000, help="유닛 최대 크기 (annotate와 동일해야 함)")
    parser.add_argument("--rebuild", action="store_true", help="DB 파일 삭제 후 전체 재구축")
    args = parser.parse_args()

    reports_dir = Path(args.reports_dir)
    if not reports_dir.is_dir():
        print(f"디렉터리가 없습니다: {reports_dir}", file=sys.stderr)
        sys.exit(2)

    db_path = Path(args.db)
    if args.rebuild and db_path.exists():
        db_path.unlink()

    con = sqlite3.connect(db_path)
    try:
        con.executescript(SCHEMA)
        ledger = dict(con.execute("SELECT filepath, content_hash FROM files"))
        seen: set[str] = set()
        updated: list[tuple[str, int]] = []
        synced: list[str] = []
        gated: list[tuple[str, list[str]]] = []  # (filepath, 빠진 스탬프)
        removed: list[str] = []

        for path in sorted(reports_dir.glob("*.md")):
            fp = path.as_posix()
            seen.add(fp)
            digest = file_digest(path)
            if ledger.get(fp) == digest:
                synced.append(fp)
                continue
            state = mdio.load_report(path, min_chars=args.min_chars, max_chars=args.max_chars)
            con.execute("DELETE FROM sections WHERE filepath = ?", (fp,))
            con.execute("DELETE FROM files WHERE filepath = ?", (fp,))
            missing = mdio.missing_stamps(state.fm)
            if missing:
                gated.append((fp, missing))
                continue
            rows = report_rows(state, fp)
            con.executemany(INSERT_SQL, rows)
            con.execute("INSERT INTO files VALUES (?, ?)", (fp, digest))
            updated.append((fp, len(rows)))

        for fp in sorted(set(ledger) - seen):
            con.execute("DELETE FROM sections WHERE filepath = ?", (fp,))
            con.execute("DELETE FROM files WHERE filepath = ?", (fp,))
            removed.append(fp)

        con.commit()
        total = con.execute("SELECT count(*) FROM sections").fetchone()[0]
    finally:
        con.close()

    size_mb = db_path.stat().st_size / 1_000_000
    print(
        f"갱신 {len(updated)} · 동기화 유지 {len(synced)} · 게이트 스킵 {len(gated)} · "
        f"제거 {len(removed)} · 총 {total}행 · {size_mb:.1f}MB"
    )
    for fp, n in updated:
        print(f"  갱신: {fp} ({n}행)")
    for fp in removed:
        print(f"  제거: {fp}")
    for fp, missing in gated:
        print(
            f"경고: {fp} — {'·'.join(missing)} 없음, DB에서 제외(기존 행도 삭제). "
            "verify 통과 후 build_db 재실행 필요.",
            file=sys.stderr,
        )
    sys.exit(1 if gated else 0)


if __name__ == "__main__":
    main()
