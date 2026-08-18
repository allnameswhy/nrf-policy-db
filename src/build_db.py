"""reports/*.md → reports.db FTS5 색인 빌드 (파이프라인 3단계).

PROJECT_NOTES.md §6 참조.

- 입력: reports/*.md 전체
- 출력: reports.db — 절 단위 sections 가상 테이블 (fts5, tokenize='trigram')
- 재생성 원칙: .md 수정 시 DB 삭제 후 전체 재빌드. 파생물이므로 git 미포함.
- 스키마·색인 대상 컬럼(summary, lead_researcher, institution 포함)은 §6 그대로.
"""

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(description="reports/*.md에서 reports.db(FTS5)를 재빌드한다.")
    parser.add_argument("--db", default="reports.db", help="출력 DB 경로 (기본: reports.db)")
    parser.add_argument("--reports-dir", default="reports", help="원본 .md 디렉터리 (기본: reports)")
    args = parser.parse_args()
    raise NotImplementedError("구현 순서 3번 — PROJECT_NOTES.md §9 참조")


if __name__ == "__main__":
    main()
