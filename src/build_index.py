"""reports/*.md → master_index.md 생성 (파이프라인 4단계).

PROJECT_NOTES.md §5 참조.

- 입력: reports/*.md 의 frontmatter + 보고서 요약 블록
- 출력: master_index.md — 보고서 단위 요약 카탈로그
- 라우팅 에이전트가 이 파일을 읽고 후보 보고서를 선택한다(문자열 매칭 아님).
- 파생 생성물이지만 유지 대상(§2) — git에 포함.

요약 소스는 frontmatter `abstract`(저자 초록 요약)가 1차, 공란인 보고서만
`> **보고서 요약:**` fallback 블록(annotate.py 삽입물)을 쓴다. 둘 다 없으면
빈 값 + 경고(추측 금지).
"""

import argparse
import sys
from pathlib import Path

import mdio


def render_entry(state: mdio.ReportState, warnings: list[str]) -> list[str]:
    fm = state.fm
    lines = [
        f"## {fm.title}",
        f"- 파일: `{state.path.as_posix()}`",
        f"- 발간: {fm.year}",
    ]
    if fm.lead_researcher or fm.institution:
        who = fm.lead_researcher
        if fm.institution:
            who = f"{who} ({fm.institution})" if who else fm.institution
        lines.append(f"- 연구책임자: {who}")

    if not fm.abstract_empty:
        summary_lines = fm.abstract.split("\n")
    else:
        report_summary = state.existing.get(mdio.REPORT_KEY, "")
        if not report_summary:
            warnings.append(f"{fm.report_id}: abstract 공란 + 보고서 요약 블록 없음 — 요약 없이 수록")
        summary_lines = [report_summary] if report_summary else []

    if len(summary_lines) <= 1:
        lines.append(f"- 요약: {summary_lines[0]}" if summary_lines else "- 요약:")
    else:
        lines.append("- 요약:")
        lines.extend(f"  {ln}" for ln in summary_lines)
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="reports/*.md에서 master_index.md를 생성한다.")
    parser.add_argument("-o", "--out", default="master_index.md", help="출력 파일 (기본: master_index.md)")
    parser.add_argument("--reports-dir", default="reports", help="원본 .md 디렉터리 (기본: reports)")
    args = parser.parse_args()

    paths = sorted(Path(args.reports_dir).glob("*.md"))
    if not paths:
        print(f"오류: {args.reports_dir}에 .md가 없습니다", file=sys.stderr)
        sys.exit(2)

    warnings: list[str] = []
    out_lines = [
        "# 정책보고서 마스터 인덱스",
        "",
        "<!-- src/build_index.py가 reports/*.md frontmatter에서 자동 생성 — 직접 수정 금지 -->",
    ]
    for path in paths:
        state = mdio.load_report(path)
        out_lines.append("")
        out_lines.extend(render_entry(state, warnings))

    mdio.write_md_lines(args.out, out_lines)
    print(f"보고서 {len(paths)}건 → {args.out}")
    for w in warnings:
        print(f"경고: {w}", file=sys.stderr)


if __name__ == "__main__":
    main()
