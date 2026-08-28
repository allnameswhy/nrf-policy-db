# nrf-policy-db

기관 발간 정책보고서를 대화형으로 탐색하는 RAG 시스템. 벡터 검색 없이 **계층 .md 원본 + SQLite FTS5(trigram) BM25 전문검색 + LLM 라우팅 에이전트** 조합으로 구성한다. 상세 설계 명세는 [PROJECT_NOTES.md](PROJECT_NOTES.md) — 작업 전 반드시 읽을 것.

## 핵심 규약 (위반 금지)

- `reports/*.md`가 **원본(source of truth)**, `reports.db`는 언제든 재생성 가능한 파생물(git 미포함). `.md` 변경 반영은 build_db **증분 동기화**(2026-08 규약, 종전 통재생성 대체 — DB 내 `files` 해시 원장 대조로 신규·변경 파일만 재색인, `verified_annotate` 없는 변경 파일은 행 삭제 + 시끄러운 스킵·exit 1, 사라진 파일 행 제거), 의심 시 `--rebuild` 통재생성. **동기화 기록을 .md에 두지 않는다**(원본은 파생물 상태를 기록하지 않음 — DB 부재 시 거짓 기록 방지, PROJECT_NOTES §6).
- 본문 텍스트는 PDF에서 프로그램(PyMuPDF)으로만 추출. **본문 추출·재생성에 LLM 사용 금지.**
- `.md` 안의 `> **요약:**` / `> **보고서 요약:**` 인용 블록은 LLM 생성물. **챗봇이 원문을 인용할 때 반드시 제외**한다.
- 헤딩 ID 규칙: **감지 서수 경로** `<!-- id: {report_id}_c{i}s{j}… -->` — 표면 번호가 아니라 감지 순서 기준(문법 6종 혼재 코퍼스 대응). 재파싱해도 동일 ID가 나와야 참조가 깨지지 않음.
- **투 레인**(2026-08): 장-절 문법이 감지되면 현행 구조 추출, 미감지 문서(타 기관·발표자료·백서류)는 **플랫 청킹 폴백** — extract가 문단 경계 크기 청킹으로 합성 헤딩 `# 구간 N (p.a-b)`(페이지 범위 = 인용 좌표)를 방출하고 frontmatter에 `structure: flat` 표식을 남긴다. 다운스트림(mdio/annotate/build_*)은 두 레인을 구분하지 않는다. **레인 수동 지정** `--lane {auto,structured,flat}`(기본 auto): 사용자가 "기존 정책 보고서" 류로 지시하면 `--lane structured`(감지 실패 시 플랫 폴백 억제·시끄러운 스킵 = 승격 검토 신호), "플랫 청킹으로" 류면 `--lane flat`(구조 감지 생략·곧장 구간 청킹, 400자 가드는 유지 → `skipped_flat_guard`). 신규 문법 프로파일 추가는 의무가 아니라 **선택**(승격) — 판단 재료는 감지 실패 시 로그·`--scan`에 남는 `l1_attempts`(후보별 장 수)·`l1_suspects`(헤딩 의심 줄 샘플, PDF 재열람 불필요), 절차는 아래 프로파일 승격 절차.
- `report_id`는 **PDF 파일명**의 `정책연구-YYYY-NN`에서 유도(`2025-02`; 합본 파트는 `2025-17_01`). 비NRF 파일명은 **스템 슬러그**로 유도(`[0-9A-Za-z가-힣-]` 외 문자를 `-`로 접음, 밑줄 제외 — `_NN`은 파트 접미사 전용; 예: `KISTEP AI백서 (2025).pdf` → `KISTEP-AI백서-2025`). 초록의 관리번호 필드는 신뢰 불가(공란·표기 불일치 실측)로 미사용.
- frontmatter `abstract`(저자 초록 요약)가 **보고서 요약의 1차 소스.** annotate.py는 하위 단위 요약만 삽입하고, abstract 공란인 보고서에만 `> **보고서 요약:**` LLM fallback을 넣는다.
- 미결 사항(PROJECT_NOTES §8)은 **선구축 후 실측** 원칙 — 사전 최적화하지 않는다.

## 파이프라인 (구현 순서 = 실행 순서)

```
src/extract.py      pdfs/*.pdf → reports/*.md (본문 + frontmatter abstract)
src/annotate.py     하위 단위 요약 삽입 (LLM; 보고서 요약은 abstract 공란 시만)
src/build_db.py     reports/*.md → reports.db (FTS5 trigram)
src/build_index.py  reports/*.md → master_index.md
```

extract.py는 구현 완료(2026-08, 10권 → 11개 .md 검증 통과 — `--scan` 진단 모드, **표 마스킹 안전판**: 표 정리에 실리지 못한 글자는 지우지 않고 본문으로 방출 — find_tables 행렬 단계 셀 소실 실측 4권 21건 복구. **플랫 청킹 폴백**(2026-08, 투 레인): 구조 미감지 시 문단 경계 그리디 청킹(상한 4,000자 = 유닛 상한 → 청크=유닛 1:1, 200자 미만 꼬리 병합)으로 자동 수용하되, 본문 텍스트 400자 미만(이미지 위주 문서)이면 종전대로 시끄럽게 스킵 — OCR은 별도 과제). **재실행 시 출력 .md가 이미 있으면 기본 스킵, `--force`로만 재추출·덮어쓴다**(재추출 시 annotate 삽입 요약과 검증 스탬프 소실 주의). annotate.py도 구현·실행 완료(2026-08, 유닛 392개 요약 + 2025-17_02 보고서 요약 fallback 삽입, **생성 모델 Sonnet 5**(초기 Haiku 구축분을 품질 감사 도입과 함께 전량 재생성 — 판정은 Haiku, §3 단계②)·thinking 비활성, `--scan`/`--smoke`/`--force` 제공. 유닛 분할 = 크기 기반 헤딩 트리 분할, PROJECT_NOTES §3 단계②. **요약을 새로 쓰는 파일은 `verified_annotate` 스탬프를 함께 제거** — 자동 미감사 리셋). frontmatter·헤딩 트리·유닛 분할·요약 블록 처리는 공용 모듈 `src/mdio.py`에 있고(통합 로더 `load_report()`, frontmatter는 year·연구책임자·기관·abstract 본문까지 파싱) **build_db.py·build_index.py도 이를 재사용**한다(파이프라인 4단계 체계는 불변). build_index.py 구현 완료(2026-08 — §5 카탈로그: abstract 원문 블록/fallback 인라인, 자동 생성 주석, 연구책임자 공란 줄 생략). build_db.py 구현 완료(2026-08 — FTS 행 = 유닛 + 참고문헌·부록 장(요약 공란, 실측 392+14=406행), `body_fold` 공백 접기 섀도 컬럼(`fold_text()` 소유 = build_db.py, 검색 쿼리도 이를 fold해 병행 MATCH), 증분 동기화 + `--rebuild`; 증분 결과 = 통재생성 결과 해시 일치 실측). 남은 구현 = 검색 흐름(PROJECT_NOTES §9 5번).

보조 도구 `src/status.py`(읽기 전용, 파이프라인 4단계 아님): 보고서별 추출/요약(M/N)/잔여 LLM 호출/검증 스탬프/DB 동기화 현황과 reports.db·master_index.md 신선도를 **아티팩트에서만 파생 계산**해 표시한다(별도 상태 저장 없음; DB 컬럼은 mtime 추정이 아니라 build_db의 files 원장 ↔ .md sha256 대조; exit 0=완전 동기화, 1=할 일).

검증 스텝 `src/verify.py`(2026-08 구현·품질 판정 통합, 파이프라인 4단계 아님 — PROJECT_NOTES §3 검증 스텝): **스탬프 상태 분기**로 검사 범위를 고른다 — 스탬프 없음 = 풀 검사 A~C(PDF 재스캔 전수 대조 → `verified_extract`), `verified_extract`만 = PDF 생략, A(.md 단독)+커버리지 확인 후 **D 품질 판정(LLM, 판정 모델 Haiku 4.5)** → 전건 통과 시 `verified_annotate`, 둘 다 = 스킵. D는 유닛 요약을 생성 입력과 동일 절단의 근거 본문과 1:1 대조해 검색·발견 기준으로 판정(**누락**·할루시네이션·왜곡·무관=FAIL — 자동 수정 없이 리포트만, 수정 경로: 요약 줄 삭제 → annotate → verify). `--full`(스탬프 무시 A~C 전수 — PDF 전수 대조 안전망은 이 플래그로만 작동)·`--reaudit`(재판정)·`--no-llm`(D 억제) 제공. 스탬프 기록은 .md에 대한 유일한 쓰기, 회수 정책: 결정론 FAIL은 전체 회수·D FAIL은 `verified_annotate`만(exit 0=전건 PASS/1=FAIL/2=사용 오류·한도 중단, `--no-stamp`는 검사 전용). 스탬프는 재추출 시 자연 소멸 = 미검증 리셋. **플랫 문서**(`structure: flat`)는 풀 검사에서 구조 재현 대신 extract와 공유하는 `find_body_start_flat`로 body 범위를 재현해 **B 전수 대조를 동일 실행**하고, A는 합성 구간 헤딩 전용 검사(비구간 헤딩·하위 헤딩 = FAIL — 레인 위장 방지), C는 생략하되 **L1 구조 신호가 있으면 레인 정합 경고**("body_start 앞 목차 페이지" 검사는 플랫 body가 표지에서 시작해 사문 — 2025-02 실측으로 교체). **신규 PDF 표준 절차(두 레인 동일): pdfs/에 추가 → extract(레인 지시 있으면 `--lane`) → verify → annotate → verify 재실행(D 판정 → verified_annotate 기록, PDF 불필요) → build_db(증분 — 변경 파일만 재색인)·build_index.** **프로파일 승격 절차**(`--lane structured` 실패 시, 에이전트 수행): ① 실패 로그의 `l1_attempts`·`l1_suspects` 검토 → ② extract.py `PROFILES`에 규칙 추가(필요시 `SUB_FAMILY_DEFS`·`detect_sub_headings` 특례) → ③ 해당 문서 extract → verify 풀 검사(C 목차 대조가 핵심 안전망) → ④ 기존 전체 재추출 회귀 대조(scratch, 전량 동일) → ⑤ 통과 시에만 채택, 커밋은 사용자. 판단 재료가 부족하면 플랫 수용이 기본값(완전 자동 승격은 오탐 시 장 분할 오염·재파싱 ID 불안정 사유로 기각 — 2026-08 문답).

## 환경

- Python 3.10 venv: `.venv\Scripts\activate` (의존성: `requirements.txt` — pymupdf, claude-agent-sdk)
- annotate·verify(D 판정)의 LLM 호출은 **claude-agent-sdk 경유 → Claude Code 구독 로그인 재사용, API 키 불필요**(verify는 annotate의 호출 레이어를 import). 대량 배치에서 사용량 한도가 문제되면 anthropic SDK + `ANTHROPIC_API_KEY`(콘솔 발급, 종량제)로 전환 가능.
- **인증 경로(annotate.py `setup_auth`)**: ① `CLAUDE_CODE_OAUTH_TOKEN` 환경변수 → ② 저장소 루트 `.claude_oauth_token` 파일(git 제외) → ③ CLI 로그인 세션(`~/.claude`). 헤드리스에서는 만료된 로그인 세션이 자동 갱신되지 않으므로("OAuth session expired" 실측), 재로그인 없이 돌리려면 `claude setup-token`(브라우저 승인 1회, **1년 유효**)으로 발급한 토큰을 `.claude_oauth_token`에 저장해 두면 된다. 단 `ANTHROPIC_API_KEY`가 설정돼 있으면 토큰보다 우선 적용됨(종량제 과금 주의).
- SQLite 3.37.2 내장 — FTS5 `tokenize='trigram'` 지원 확인됨. 실측: 3글자 이상 쿼리는 조사 붙은 어절도 정상 매칭("예산이", "탄소중립" OK). **단, 2글자 이하 쿼리("예산")는 MATCH·LIKE 모두 0건** — trigram의 구조적 제약. **1차 대응 확정(2026-08)**: 검색 에이전트 프롬프트 규칙 — 2글자 단독 쿼리 금지, 2글자 개념은 조사·복합어 변형 OR로 확장(3글자 이상만 생성). recall 구멍이 실측될 때만 LIKE 풀스캔 폴백 추가, 형태소(kiwipiepy) 전환은 그 다음(IMPLEMENTATION_NOTES §7·§8).
- 원본 PDF는 `pdfs/`에 두되 git 제외(.gitignore). `reports/*.md`와 `master_index.md`는 git 포함.
