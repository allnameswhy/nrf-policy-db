"""verify D FAIL 요약 줄 삭제 — 수정 경로(요약 줄 삭제 → annotate → verify)의 삭제 단계 도구.

직전 verify 실행 로그(logs/verify_log.json)의 judgements에서 verdict == FAIL인 유닛·보고서
요약의 `> **요약:**` / `> **보고서 요약:**` 줄(+뒤따르는 빈 줄 = 요약 블록)을 .md에서 지운다.
그러면 annotate가 지워진 유닛만 증분 재생성하고(보고서 요약은 유닛이 바뀐 파일마다 재생성),
verify는 판정 캐시 덕에 바뀐 요약만 재판정한다 — 파이프라인 4단계 아님, 사용 레이어 도구.

안전장치: 로그 run_at보다 .md가 최신이면(annotate가 이미 다시 썼을 수 있음) 그 파일은 건너뛴다
(--force로 강행). ERROR(판정불능)는 요약 결함이 아니므로 지우지 않는다. --dry-run은 대상만 표시.
종료 코드 0 = 정상(삭제 0건 포함), 2 = 사용 오류.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import mdio
from verify import summary_line_map


def main() -> None:
    parser = argparse.ArgumentParser(description="verify D FAIL 요약 줄 삭제")
    parser.add_argument("ids", nargs="*", help="대상 report_id (기본: 로그의 전체; 합본은 파트로 확장)")
    parser.add_argument("--log", default="logs/verify_log.json", help="verify 로그 경로")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--dry-run", action="store_true", help="삭제하지 않고 대상만 표시")
    parser.add_argument("--force", action="store_true", help=".md가 로그보다 최신이어도 삭제")
    args = parser.parse_args()

    log_path = Path(args.log)
    if not log_path.is_file():
        print(f"verify 로그 없음: {log_path}", file=sys.stderr)
        sys.exit(2)
    log = json.loads(log_path.read_text(encoding="utf-8"))
    # 기준 시각 = 로그 파일 mtime(verify는 스탬프 기록 뒤에 로그를 쓴다 — run_at은 실행 시작 시각이라
    # verify 자신의 스탬프 쓰기가 "최신"으로 오인됨). 이보다 .md가 최신이면 verify 이후 다른 쓰기가 있었다.
    log_mtime = log_path.stat().st_mtime
    judgements: dict[str, dict] = log.get("judgements") or {}

    def selected(stem: str) -> bool:
        return not args.ids or any(stem == r or stem.startswith(r + "_") for r in args.ids)

    total = 0
    for stem, judg in sorted(judgements.items()):
        if not selected(stem):
            continue
        fails = [hid for hid, v in judg.items() if v.get("verdict") == "FAIL"]
        if not fails:
            continue
        md = Path(args.reports_dir) / f"{stem}.md"
        if not md.is_file():
            print(f"{stem}: .md 없음 — 건너뜀", file=sys.stderr)
            continue
        if md.stat().st_mtime > log_mtime and not args.force:
            print(f"{stem}: .md가 verify 로그보다 최신 — 건너뜀(annotate 재실행 후? --force로 강행)",
                  file=sys.stderr)
            continue
        lines = mdio.read_md_lines(md)
        lmap = summary_line_map(SimpleNamespace(original=lines))
        idxs: list[int] = []
        missing: list[str] = []
        for hid in fails:
            ln = lmap.get(hid)
            (idxs.append(ln - 1) if ln else missing.append(hid))
        for i in sorted(idxs, reverse=True):
            assert mdio.SUMMARY_RE.match(lines[i]), (stem, i + 1, lines[i][:40])
            end = i + 2 if i + 1 < len(lines) and lines[i + 1] == "" else i + 1
            del lines[i:end]
        label = " ".join("보고서요약" if h == mdio.REPORT_KEY else h.split("_")[-1] for h in fails)
        note = f" (이미 없음: {len(missing)})" if missing else ""
        if args.dry_run:
            print(f"{stem}: 삭제 예정 {len(idxs)}건 — {label}{note}")
        else:
            mdio.write_md_lines(md, lines)
            print(f"{stem}: 삭제 {len(idxs)}건 — {label}{note}")
        total += len(idxs)
    print(f"합계: {'삭제 예정' if args.dry_run else '삭제'} {total}건")


if __name__ == "__main__":
    main()
