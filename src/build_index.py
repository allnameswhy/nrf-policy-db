"""reports/*.md → master_index.md 생성 (파이프라인 4단계).

PROJECT_NOTES.md §5 참조.

- 입력: reports/*.md 의 frontmatter + 보고서 요약 블록
- 출력: master_index.md — 보고서 단위 요약 카탈로그
- 라우팅 에이전트가 이 파일을 읽고 후보 보고서를 선택한다(문자열 매칭 아님).
- 파생 생성물이지만 유지 대상(§2) — git에 포함.
"""

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(description="reports/*.md에서 master_index.md를 생성한다.")
    parser.add_argument("-o", "--out", default="master_index.md", help="출력 파일 (기본: master_index.md)")
    parser.add_argument("--reports-dir", default="reports", help="원본 .md 디렉터리 (기본: reports)")
    args = parser.parse_args()
    raise NotImplementedError("구현 순서 4번 — PROJECT_NOTES.md §9 참조")


if __name__ == "__main__":
    main()
