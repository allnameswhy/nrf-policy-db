"""절 요약·보고서 요약 생성 및 삽입 (파이프라인 2단계).

PROJECT_NOTES.md §3 단계 ②·③ 참조.

- 입력: reports/*.md (본문만 있는 상태)
- 출력: 같은 파일에 요약 삽입
  - 절 요약: 절 본문 1개 → 1~2문장, 헤딩 아래 `> **요약:**` 인용 블록으로 삽입
  - 보고서 요약: 절 요약 전체 → 2~3문장, 문서 상단 `> **보고서 요약:**` 블록
- 본문은 LLM에 보내기만 하고 되돌려받지 않는다(원문 오염 방지, §1 원본 무결성).
- LLM 호출: claude-agent-sdk 경유 — Claude Code 구독 로그인 재사용, API 키 불필요.
"""

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(description="reports/*.md에 절·보고서 요약을 생성해 삽입한다.")
    parser.add_argument("md", nargs="+", help="대상 .md 파일 경로 (reports/*.md)")
    parser.add_argument("--force", action="store_true", help="기존 요약이 있어도 다시 생성")
    args = parser.parse_args()
    raise NotImplementedError("구현 순서 2번 — PROJECT_NOTES.md §9 참조")


if __name__ == "__main__":
    main()
