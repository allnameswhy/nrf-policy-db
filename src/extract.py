"""PDF → 계층 .md 변환 (파이프라인 1단계).

PROJECT_NOTES.md §3 단계 ① 참조.

- 입력: pdfs/*.pdf
- 출력: reports/*.md (frontmatter + 장/절 헤딩 + 본문. 요약 없음)
- 도구: PyMuPDF로만 텍스트 추출 (`import pymupdf` — fitz 별칭은 deprecated). LLM 사용 금지.
- 계층 판별: 본문의 "제N장 / 제N절" 텍스트 패턴 기준. 북마크 미사용.
- 패턴이 잡히지 않는 문서는 로그로 남기고 수동 확인.
- 헤딩 ID 규칙: {report_id}_c{장}s{절} (재파싱해도 동일해야 함, §4)
"""

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(description="PDF에서 계층 구조 .md를 추출한다.")
    parser.add_argument("pdf", nargs="+", help="변환할 PDF 파일 경로 (pdfs/*.pdf)")
    parser.add_argument("-o", "--out-dir", default="reports", help="출력 디렉터리 (기본: reports)")
    args = parser.parse_args()
    raise NotImplementedError("구현 순서 1번 — PROJECT_NOTES.md §9 참조")


if __name__ == "__main__":
    main()
