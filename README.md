# nrf-policy-db — 정책보고서 대화형 DB

기관에서 발간한 정책보고서의 내용을 빠르게 파악할 수 있도록 만든 대화형 검색 시스템입니다. 벡터 임베딩 없이 다음 세 가지를 조합한 RAG 구조를 사용합니다.

1. **계층 구조 마크다운 원본** — PDF에서 추출한 본문을 장/절 구조로 보존 (`reports/*.md`)
2. **SQLite FTS5 전문검색** — trigram 토크나이저 기반 BM25 키워드 검색 (`reports.db`)
3. **LLM 라우팅 에이전트** — 마스터 인덱스와 검색 결과를 보고 관련 보고서·절을 선택

구조 개요는 [PROJECT_NOTES.md](PROJECT_NOTES.md), 구현 상세(파싱 규칙·검증 항목·스키마·실측 기록)는 [IMPLEMENTATION_NOTES.md](IMPLEMENTATION_NOTES.md), 확장 후보는 [ROADMAP.md](ROADMAP.md)를 참조하세요.

## 셋업

```
py -3.10 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

LLM 호출(요약 생성·품질 판정·검색)은 `claude-agent-sdk`를 통해 로컬 Claude Code 로그인을 재사용하므로 별도의 API 키 설정이 필요 없습니다. (Claude Code CLI가 설치·로그인되어 있어야 합니다.)

헤드리스로 돌릴 때 "OAuth session expired"가 나면 `claude setup-token`으로 발급한 토큰(1년 유효)을 저장소 루트 `.claude_oauth_token`에 저장해 두면 재로그인 없이 동작합니다.

## 자료 구축 — 신규 PDF 표준 절차

원본 PDF를 `pdfs/`에 넣은 뒤 순서대로 실행합니다. **`verify.py`는 선택이 아니라 각 단계의 게이트입니다.**

```
python src/register.py               # 0. 판독(새 PDF마다 LLM 1회 → toc/*.json) + 표지 관리번호 → report_ids.tsv
python src/extract.py pdfs/*.pdf     # 1. PDF → 계층 .md (목차 대응으로 헤딩 결정, LLM 미개입)
python src/verify.py reports/*.md    #    검증 → 통과 시 verified_extract 스탬프 (verified_register는 extract가 기록)
python src/annotate.py reports/*.md  # 2. 단위 요약 삽입 (LLM)
python src/verify.py reports/*.md    #    요약 품질 판정 → verified_annotate 스탬프 (PDF 불필요)
#                                    #    FAIL이면 annotate → verify 반복(annotate가 판정 캐시의 FAIL 유닛만 재생성 — /admin 일괄 해결은 자동, 최대 3바퀴)
#                                    #    3바퀴로도 안 풀리면 /admin "요약 품질 수동 검토" 카드에서 사람이 판단합니다(아래 참조)
python src/build_db.py               # 3. reports.db (FTS5 색인) 증분 동기화
python src/build_index.py            # 4. master_index.md 생성
```

- `.md`를 수정한 뒤에는 `build_db.py`를 그냥 다시 실행하면 됩니다 — DB 안의 해시 원장과 대조해 **바뀐 파일만 다시 색인**합니다. DB를 지울 필요가 없습니다. 색인이 꼬인 것 같으면 `--rebuild`로 통재생성하세요.
- `build_db.py`와 `build_index.py`는 검증 스탬프 3개(`verified_register`·`verified_extract`·`verified_annotate`)가 모두 있는 파일만 싣습니다. 경고가 뜨면 `verify.py`를 먼저 통과시키세요.
- `extract.py`는 `.md`가 이미 있는 문서를 건너뜁니다(합본은 문서 단위). 다시 뽑으려면 `--force`가 필요하며, **이때 삽입된 요약과 verify 스탬프가 함께 사라집니다**(`verified_register`는 새로 찍힙니다).
- 헤딩은 문서의 목차를 본문 줄에 대응시켜 정합니다(등록 단계에서 PDF당 LLM 판독 1회 → `toc/*.json`). 목차가 없거나 대응이 성립하지 않는 문서는 `.md`를 만들지 않고 `/admin` 카드에서 문서별로 플랫(구간 청킹) 수용 여부를 묻습니다 — 명령줄로는 `python src/extract.py "pdfs/<파일>.pdf" --rid <관리번호> --flat`.
- 관리번호가 같은 보고서(본편·별권·다른 보고서)가 둘 이상이면 `register.py`가 자동으로 정하지 않고 비워 둡니다. `/admin` 가족 카드에서 정하거나 `python src/register.py --set "<파일명 일부>=<RID>"`로 기입하세요. **`report_ids.tsv`를 편집기로 직접 고치지 마세요** — 탭이 밀려 값이 다른 열로 들어간 사례가 있습니다.
- 요약 품질 판정이 끝까지 FAIL인 유닛은 `/admin`의 "요약 품질 수동 검토" 카드에 남습니다. 요약을 직접 고친 뒤(또는 그대로 타당하다고 보면) **[사람이 확인함]**을 누르면 이후 판정·재생성에서 빠집니다(명령줄: `python src/verify.py --accept <RID>:<HID>`). 확인 전까지 그 보고서는 DB·카탈로그에 실리지 않습니다.
- 요약은 **본문 서술문만** 근거로 씁니다(2026-10-14) — 표의 수치·명칭은 옮기지 않고 단위도 환산하지 않습니다. 그래서 표에만 있던 수치는 요약에 나타나지 않습니다(검색으로 본문에 도달해 원문에서 읽는 것이 전제).

## 검색 실행

```
python src/search.py "질문"          # CLI (--trace 로 툴 호출 추적)
python src/serve.py                  # 웹 서버 → http://127.0.0.1:8765
serve_lan.cmd                        # 같은 서버를 LAN에 개방 (팀 테스트용, 신뢰망 전용)
```

웹 서버는 세 화면을 제공합니다.

| 경로 | 용도 |
|---|---|
| `/` | 검색 — 진행 타임라인, 답변 내 출처 하이라이트, 출처 표 |
| `/admin` | DB 관리 — 등록·추출·검증·요약·색인에서 막힌 항목을 문제 카드로 보여주고 **[일괄 해결]**로 순차 실행 |
| `/browse` | 자료 현황 — DB에 동기화된 보고서 목록, 행을 누르면 목차·요약·저자 초록 |

`serve.py`는 무인증이고 질의마다 LLM 사용량을 소모하므로 신뢰할 수 있는 망에서만 여세요. 기동 시점의 `.md`/DB 상태를 고정하므로, 자료를 새로 구축했다면 서버를 재기동해야 합니다.

## 현황 확인

```
python src/status.py                 # 보고서별 추출·요약·검증·DB 동기화 현황 (읽기 전용)
```

## 저장소 구성

| 경로 | 역할 | git |
|---|---|---|
| `pdfs/` | 원본 PDF (입력, 읽기 전용) | 제외 |
| `pdfs/hold/` | 보류 PDF — 아직 적재하지 않을 파일. 어느 단계·현황판에도 잡히지 않음 | 제외 |
| `toc/` | 판독 파일 (문서 범위·본문 시작·목차 항목) — 구조의 원천 | 포함 |
| `report_ids.tsv` | 관리번호 등록부 — 보고서 ID의 유일 소스 | 포함 |
| `reports/` | 계층 .md 본문 + 요약 (**source of truth**) | 포함 |
| `master_index.md` | 보고서 단위 요약 카탈로그 | 포함 |
| `reports.db` | 절 단위 FTS5 색인 (재생성 가능한 파생물) | 제외 |
| `src/` | 파이프라인 스크립트 + 검증·검색·현황 도구 | 포함 |
| `web/` | 검색 테스트 UI (정적 1파일) | 포함 |
| `logs/` | 실행 로그 (재생성 가능) | 제외 |
