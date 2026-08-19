# nrf-policy-db

기관 발간 정책보고서를 대화형으로 탐색하는 RAG 시스템. 벡터 검색 없이 **계층 .md 원본 + SQLite FTS5(trigram) BM25 전문검색 + LLM 라우팅 에이전트** 조합으로 구성한다. 상세 설계 명세는 [PROJECT_NOTES.md](PROJECT_NOTES.md) — 작업 전 반드시 읽을 것.

## 핵심 규약 (위반 금지)

- `reports/*.md`가 **원본(source of truth)**, `reports.db`는 언제든 재생성 가능한 파생물(git 미포함). `.md` 수정 시 DB는 삭제 후 전체 재빌드.
- 본문 텍스트는 PDF에서 프로그램(PyMuPDF)으로만 추출. **본문 추출·재생성에 LLM 사용 금지.**
- `.md` 안의 `> **요약:**` / `> **보고서 요약:**` 인용 블록은 LLM 생성물. **챗봇이 원문을 인용할 때 반드시 제외**한다.
- 헤딩 ID 규칙: **감지 서수 경로** `<!-- id: {report_id}_c{i}s{j}… -->` — 표면 번호가 아니라 감지 순서 기준(문법 6종 혼재 코퍼스 대응). 재파싱해도 동일 ID가 나와야 참조가 깨지지 않음.
- `report_id`는 **PDF 파일명**의 `정책연구-YYYY-NN`에서 유도(`2025-02`; 합본 파트는 `2025-17_01`). 초록의 관리번호 필드는 신뢰 불가(공란·표기 불일치 실측)로 미사용.
- frontmatter `abstract`(저자 초록 요약)가 **보고서 요약의 1차 소스.** annotate.py는 하위 단위 요약만 삽입하고, abstract 공란인 보고서에만 `> **보고서 요약:**` LLM fallback을 넣는다.
- 미결 사항(PROJECT_NOTES §8)은 **선구축 후 실측** 원칙 — 사전 최적화하지 않는다.

## 파이프라인 (구현 순서 = 실행 순서)

```
src/extract.py      pdfs/*.pdf → reports/*.md (본문 + frontmatter abstract)
src/annotate.py     하위 단위 요약 삽입 (LLM; 보고서 요약은 abstract 공란 시만)
src/build_db.py     reports/*.md → reports.db (FTS5 trigram)
src/build_index.py  reports/*.md → master_index.md
```

extract.py는 구현 완료(2026-08, 10권 → 11개 .md 검증 통과 — `--scan` 진단 모드 제공). **재실행 시 출력 .md가 이미 있으면 기본 스킵, `--force`로만 재추출·덮어쓴다**(재추출 시 annotate 삽입 요약과 검증 스탬프 소실 주의). annotate.py도 구현·실행 완료(2026-08, 유닛 392개 요약 + 2025-17_02 보고서 요약 fallback 삽입, Haiku 4.5·thinking 비활성, `--scan`/`--smoke`/`--force` 제공. 유닛 분할 = 크기 기반 헤딩 트리 분할, PROJECT_NOTES §3 단계②). frontmatter·헤딩 트리·유닛 분할·요약 블록 처리는 공용 모듈 `src/mdio.py`에 있고(통합 로더 `load_report()`) **build_db.py·build_index.py도 이를 재사용**할 것(파이프라인 4단계 체계는 불변). build_db.py·build_index.py는 argparse 골격만 있는 스텁 상태. PROJECT_NOTES §9 순서대로 구현한다.

보조 도구 `src/status.py`(읽기 전용, 파이프라인 4단계 아님): 보고서별 추출/요약(M/N)/잔여 LLM 호출/검증 스탬프 현황과 reports.db·master_index.md 신선도를 **아티팩트에서만 파생 계산**해 표시한다(별도 상태 저장 없음; exit 0=완전 동기화, 1=할 일). 검증 스탬프(frontmatter `verified_extract`/`verified_annotate`, PROJECT_NOTES §4)는 차기 설계할 검증 스텝이 기록하며, 재추출 시 자연 소멸 = 미검증 리셋.

## 환경

- Python 3.10 venv: `.venv\Scripts\activate` (의존성: `requirements.txt` — pymupdf, claude-agent-sdk)
- annotate의 LLM 호출은 **claude-agent-sdk 경유 → Claude Code 구독 로그인 재사용, API 키 불필요.** 대량 배치에서 사용량 한도가 문제되면 anthropic SDK + `ANTHROPIC_API_KEY`(콘솔 발급, 종량제)로 전환 가능.
- **인증 경로(annotate.py `setup_auth`)**: ① `CLAUDE_CODE_OAUTH_TOKEN` 환경변수 → ② 저장소 루트 `.claude_oauth_token` 파일(git 제외) → ③ CLI 로그인 세션(`~/.claude`). 헤드리스에서는 만료된 로그인 세션이 자동 갱신되지 않으므로("OAuth session expired" 실측), 재로그인 없이 돌리려면 `claude setup-token`(브라우저 승인 1회, **1년 유효**)으로 발급한 토큰을 `.claude_oauth_token`에 저장해 두면 된다. 단 `ANTHROPIC_API_KEY`가 설정돼 있으면 토큰보다 우선 적용됨(종량제 과금 주의).
- SQLite 3.37.2 내장 — FTS5 `tokenize='trigram'` 지원 확인됨. 실측: 3글자 이상 쿼리는 조사 붙은 어절도 정상 매칭("예산이", "탄소중립" OK). **단, 2글자 이하 쿼리("예산")는 MATCH·LIKE 모두 0건** — trigram의 구조적 제약. 검색 흐름 구현 시 2글자 검색어 대응 필요(쿼리 확장, SQLite 업그레이드, 또는 kiwipiepy 전환 — PROJECT_NOTES §8 연계 판단).
- 원본 PDF는 `pdfs/`에 두되 git 제외(.gitignore). `reports/*.md`와 `master_index.md`는 git 포함.
