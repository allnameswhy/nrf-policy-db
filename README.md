# nrf-policy-db — 정책보고서 대화형 DB

기관에서 발간한 정책보고서의 내용을 빠르게 파악할 수 있도록 만든 대화형 검색 시스템입니다. 벡터 임베딩 없이 다음 세 가지를 조합한 RAG 구조를 사용합니다.

1. **계층 구조 마크다운 원본** — PDF에서 추출한 본문을 장/절 구조로 보존 (`reports/*.md`)
2. **SQLite FTS5 전문검색** — trigram 토크나이저 기반 BM25 키워드 검색 (`reports.db`)
3. **LLM 라우팅 에이전트** — 마스터 인덱스와 검색 결과를 보고 관련 보고서·절을 선택

전체 설계 명세는 [PROJECT_NOTES.md](PROJECT_NOTES.md)를 참조하세요.

## 셋업

```
py -3.10 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

LLM 호출(요약 생성)은 `claude-agent-sdk`를 통해 로컬 Claude Code 로그인을 재사용하므로 별도의 API 키 설정이 필요 없습니다. (Claude Code CLI가 설치·로그인되어 있어야 합니다.)

## 파이프라인 실행 순서

원본 PDF를 `pdfs/`에 넣은 뒤:

```
python src/extract.py pdfs/*.pdf     # 1. PDF → 계층 .md (본문만)
python src/annotate.py reports/*.md  # 2. 절 요약·보고서 요약 삽입 (LLM)
python src/build_db.py               # 3. reports.db (FTS5 색인) 빌드
python src/build_index.py            # 4. master_index.md 생성
```

`.md`를 수정했다면 `reports.db`를 지우고 3번부터 다시 실행합니다(파생물 재생성 원칙).

## 저장소 구성

| 경로 | 역할 | git |
|---|---|---|
| `pdfs/` | 원본 PDF (입력, 읽기 전용) | 제외 |
| `reports/` | 계층 .md 본문 + 요약 (**source of truth**) | 포함 |
| `master_index.md` | 보고서 단위 요약 카탈로그 | 포함 |
| `reports.db` | 절 단위 FTS5 색인 (재생성 가능한 파생물) | 제외 |
| `src/` | 파이프라인 스크립트 | 포함 |
