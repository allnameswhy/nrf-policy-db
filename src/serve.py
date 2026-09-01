"""로컬 검색 테스트 서버 — search.perform_search를 얇게 감싼다 (사용 레이어, 파이프라인 4단계 밖).

브라우저에서 질의→답변을 직접 테스트하기 위한 소형 서버. POST /api/search가
진행 이벤트(턴·툴 요약)를 NDJSON으로 실시간 스트리밍하고 마지막에 답변+출처를 내려준다.

- 바인딩 기본 127.0.0.1 — 구독 인증된 LLM 호출을 함부로 노출하지 않는다.
  팀 테스트는 `--host 0.0.0.0`으로 LAN 개방(기동 로그에 접속 URL 출력; 엔드포인트에
  별도 인증이 없으므로 신뢰망에서만 — 질의 1건 = 실제 LLM 사용량 소모).
- freshness 검사는 **기동 시 1회 + DB 관리 작업(job) 완료·현황판 새로고침 시 캐시 갱신**
  (질의마다 검사하지 않음 — 질의 경로 해시 비용 0, 서버 밖 .md/DB 변경은 재기동이나
  현황판 새로고침으로 감지): 캐시가 불일치를 가리키면 질의를 409로 차단하고 첫 페이지에
  배너를 띄운다(--allow-stale로만 강행). setup_auth는 기동 시 1회.
- 동시 질의 상한 `--concurrency`(기본 1) — 초과는 409 즉시 거부(대기 큐 없음).
- /admin = DB 관리 페이지(2026-09 카드 재설계): **문제 카드 + 일괄 해결** —
  derive_cards가 현황(status.collect_status)·extract 로그에서 파일별 문제 카드를
  도출한다(신규 PDF 레인 선택, 감지 실패 승격 여부, --force 재추출 포함 여부 등
  결정 컨트롤 포함 — 실행 버튼은 카드에 없음). [일괄 해결]은 derive_plan의 **누적
  파이프라인**(단계별 대상 파일이 하류로 누적: promote/extract{결정 파일} →
  verify{+미검증} → annotate{+요약 잔여} → verify{+품질 대기} → build_db →
  build_index)을 단일 job 큐로 순차 실행한다. 단계 사이마다 collect_status를 재계산해
  **탈락 파일은 버리고 생존 파일만 다음 단계로 데려간다**(stage_targets — 로그 포맷이
  아니라 상태·스탬프 기준), 하드 스톱은 verify·promote의 exit 2(사용 오류·인증·한도 =
  공통 장애)만 — extract·annotate의 exit 2는 파일 단위 실패 포함이라 계속. promote(코드
  수정)·--force(요약·스탬프 소실)가 포함된 해결은 루프백 요청만 허용. 출력은 서버
  버퍼(단일 job)에 쌓고 브라우저가 폴링하므로 새로고침·이탈에도 작업은 계속되고
  재접속된다. 질의↔DB 작업은 상호 배제(409). 완료 기록은 결과 박스의 [확인(닫기)]
  ack로 닫는다(진행 중 작업 복원은 그대로).
- /browse = 자료 현황 페이지(2026-09): 보고서별 현황판 표(행 클릭 → 그 보고서의
  목차·요약 로드) + 마스터 카탈로그 뷰 — reports.db 읽기 전용 조회.
- 멀티턴(질의 간 세션 유지)은 범위 밖 — perform_search의 "세션=질의 1건" 구조를
  세션 보관소로 바꿔야 하므로 확장 시 별도 설계.

사용: python src/serve.py [--port 8765] [--host 0.0.0.0] [--concurrency 2]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sqlite3
import sys
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from annotate import AuthError, RetryableError, UsageLimitReached, fatal_msg, setup_auth
from build_db import file_digest
from search import DEFAULT_MODEL, MAX_TURNS, TOP_N, perform_search
from status import PART_SUFFIX_RE, collect_status, read_db_ledger, scan_pdfs

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO_ROOT / "web" / "index.html"
ADMIN_HTML = REPO_ROOT / "web" / "admin.html"
BROWSE_HTML = REPO_ROOT / "web" / "browse.html"
PING_INTERVAL = 15  # 무이벤트 keep-alive 초 — LAN에서도 사실상 보험
SUBPROC_LINE_LIMIT = 1 << 20  # readline 상한 — 기본 64KB로는 긴 진단 줄이 끊길 수 있음

# 일괄 해결 큐의 단계 순서 = 표준 절차(CLAUDE.md): promote/extract → verify(추출 검증)
# → annotate(요약 생성) → verify2(요약 품질 검증) → build_db → build_index.
# verify2도 CLI는 verify.py — 스탬프 분기가 검사 범위(품질 판정)를 스스로 고른다.
STAGE_LABELS = {"verify": "추출 검증", "annotate": "요약 생성", "verify2": "요약 품질 검증",
                "build_db": "DB 동기화", "build_index": "카탈로그 재생성"}


def base_id(rid: str) -> str:
    """합본 파트 report_id(2025-17_01) → base(2025-17). 단독본은 그대로."""
    m = PART_SUFFIX_RE.match(rid)
    return m.group(1) if m else rid


def stage_argv(stage: dict, targets: list[str]) -> list[str]:
    """단계 → CLI argv. targets는 실행 직전 확정된 대상(stage_targets — 늦은 바인딩)."""
    py = sys.executable
    step = stage["step"]
    if step == "promote":
        return [py, "src/promote.py", f"pdfs/{targets[0]}"]
    if step == "extract":
        argv = [py, "src/extract.py", *(f"pdfs/{f}" for f in targets), "--lane", stage["lane"]]
        if stage.get("force"):
            argv.append("--force")
        return argv
    if step in ("verify", "verify2"):
        return [py, "src/verify.py", *targets]  # base id는 verify가 합본 파트로 확장
    if step == "annotate":
        return [py, "src/annotate.py", *(f"reports/{rid}.md" for rid in targets)]
    if step == "build_db":
        return [py, "src/build_db.py"]  # 전역 증분 동기화
    if step == "build_index":
        return [py, "src/build_index.py"]
    raise ValueError(f"unknown step: {step}")


def make_args(base: argparse.Namespace) -> argparse.Namespace:
    """perform_search·make_search_options가 읽는 필드만 요청별 Namespace로 구성."""
    return argparse.Namespace(
        db="reports.db",
        index_path=Path("master_index.md"),  # search.main의 동적 부착 필드와 동일
        top_n=base.top_n,
        trace=False,
        timeout=base.timeout,
        max_turns=base.max_turns,
        model=base.model,
        thinking=base.thinking,
    )


def status_args() -> argparse.Namespace:
    """status.collect_status가 읽는 필드 — 파이프라인 기본값과 동일(min/max는 annotate 기준)."""
    return argparse.Namespace(pdf_dir="pdfs", reports_dir="reports", db="reports.db",
                              index="master_index.md", min_chars=200, max_chars=4000)


def check_freshness() -> tuple[bool, str]:
    """search.freshness_gate의 비종료판 — DB 원장 해시 ↔ .md sha256 + 카탈로그 존재.

    실패해도 서버는 살아 있고(관리 페이지로 복구), 질의만 409로 차단된다.
    """
    db_path = Path("reports.db")
    if not db_path.exists():
        return False, "reports.db 없음 — DB 관리에서 일괄 해결을 실행해야 검색할 수 있습니다."
    ledger = read_db_ledger(db_path)
    if ledger is None:
        return False, "reports.db의 files 원장을 읽을 수 없습니다 — build_db --rebuild 검토."
    digests = {p.as_posix(): file_digest(p) for p in sorted(Path("reports").glob("*.md"))}
    stale = sorted(fp for fp, d in digests.items() if ledger.get(fp) != d)
    orphan = sorted(set(ledger) - set(digests))
    if stale or orphan:
        detail = ", ".join(stale + [f"{fp}(원본 없음)" for fp in orphan])
        return False, f"DB 미반영 {len(stale) + len(orphan)}건: {detail}"
    if not Path("master_index.md").exists():
        return False, "master_index.md 없음 — 카탈로그 재생성 필요."
    return True, ""


def lan_ip() -> str | None:
    """기본 라우트 인터페이스의 IPv4 — UDP connect 트릭(패킷은 실제 전송되지 않음)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return None


def open_db_ro() -> sqlite3.Connection | None:
    if not Path("reports.db").exists():
        return None
    return sqlite3.connect("file:reports.db?mode=ro", uri=True)


async def index(request):
    return FileResponse(INDEX_HTML)


async def admin_page(request):
    return FileResponse(ADMIN_HTML)


async def browse_page(request):
    return FileResponse(BROWSE_HTML)


async def api_meta(request):
    """첫 페이지 배너용 — freshness 캐시·작업 진행 여부 (질의 없이도 조회 가능)."""
    ok, msg = request.app.state.freshness
    job = request.app.state.job
    return JSONResponse({
        "freshness": {"ok": ok, "message": msg},
        "allow_stale": request.app.state.base_args.allow_stale,
        "job_running": bool(job and job["running"]),
    })


async def api_search(request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON 본문이 필요합니다."}, status_code=400)
    question = str(payload.get("question", "")).strip()
    if not question:
        return JSONResponse({"error": "질문이 비어 있습니다."}, status_code=400)
    state = request.app.state
    if state.job and state.job["running"]:
        return JSONResponse(
            {"error": "DB 관리 작업 진행 중 — 완료 후 다시 질의하세요."},
            status_code=409,
        )
    if not state.base_args.allow_stale:
        ok, msg = state.freshness  # 기동/작업 완료 시 갱신되는 캐시 — 질의마다 해시하지 않음
        if not ok:
            return JSONResponse(
                {"error": f"검색 차단: {msg} — DB 관리(/admin)에서 파이프라인을 실행하세요."},
                status_code=409,
            )
    sem: asyncio.Semaphore = state.search_sem
    if sem.locked():  # 카운터 0 = 동시 상한 도달
        return JSONResponse(
            {"error": "동시 질의 상한에 도달했습니다 — 진행 중인 질의가 끝난 뒤 다시 시도하세요."},
            status_code=409,
        )

    queue: asyncio.Queue = asyncio.Queue()

    async def worker() -> None:
        try:
            result = await perform_search(
                question, make_args(state.base_args), emit=queue.put_nowait
            )
            if not result.answer.strip():
                queue.put_nowait(
                    {
                        "type": "error",
                        "kind": "empty",
                        "message": f"답변이 생성되지 않았습니다(subtype={result.subtype}, "
                        f"{result.turns}턴) — 재시도하거나 서버를 --max-turns 상향으로 재기동하세요.",
                    }
                )
            else:
                queue.put_nowait(
                    {
                        "type": "result",
                        "answer": result.answer,
                        "sources": result.sources,
                        "fabricated": result.fabricated,
                        "no_delivery": result.no_delivery,
                        "turns": result.turns,
                        "cost_usd": result.cost_usd,
                    }
                )
        except (UsageLimitReached, AuthError) as e:
            queue.put_nowait({"type": "error", "kind": "fatal", "message": fatal_msg(e)})
        except RetryableError as e:
            queue.put_nowait(
                {
                    "type": "error",
                    "kind": "retryable",
                    "message": f"세션 오류: {e} — 다시 시도하면 새 세션으로 실행됩니다.",
                }
            )
        except Exception as e:  # 예상 밖 오류에도 스트림은 반드시 종료
            queue.put_nowait(
                {"type": "error", "kind": "retryable", "message": f"예상치 못한 오류: {e}"}
            )
        finally:
            queue.put_nowait(None)  # 종료 신호 — put_nowait라 취소 중 finally에서도 안전

    async def stream():
        async with sem:
            state.active_searches += 1  # DB 작업과의 상호 배제 카운터
            task = asyncio.create_task(worker())
            try:
                while True:
                    try:
                        ev = await asyncio.wait_for(queue.get(), timeout=PING_INTERVAL)
                    except asyncio.TimeoutError:
                        yield '{"type":"ping"}\n'
                        continue
                    if ev is None:
                        break
                    yield json.dumps(ev, ensure_ascii=False) + "\n"
            finally:
                # 브라우저 이탈/새로고침 → perform_search의 disconnect가 세션 정리
                state.active_searches -= 1
                task.cancel()

    return StreamingResponse(stream(), media_type="application/x-ndjson")


# ---- DB 관리(/admin) API ----


# 자동 단계가 처리하는 노트(승격 대기·검증 후 변경)와 정보성 노트(플랫 구조)는 카드 제외
AUTO_NOTES = ("verify 승격 대기", "검증 후 변경", "플랫 구조")
REEXTRACT_NOTE = "PDF가 .md보다 최신"
PROMOTABLE = ("skipped_no_profile", "skipped_no_body_start")  # 감지 실패 = 승격 결정 대상


def read_extract_log() -> dict[str, dict]:
    """마지막 extract 로그의 비정상 엔트리 {PDF 파일명: 요약} — 감지 실패·오류 카드 재료.

    l1_attempts·l1_suspects는 extract가 감지 실패 시 남기는 승격 판단 재료다.
    """
    try:
        data = json.loads((REPO_ROOT / "logs" / "extract_log.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if data.get("mode") != "extract":
        return {}
    out: dict[str, dict] = {}
    for e in data.get("files", []):
        status = str(e.get("status", ""))
        if status in ("ok", "skipped_exists") or not e.get("file"):
            continue
        out[str(e["file"])] = {
            "status": status,
            "l1_attempts": e.get("l1_attempts") or {},
            "l1_suspects": e.get("l1_suspects") or [],
            "error": str(e.get("error"))[:2000] if e.get("error") else None,
        }
    return out


def rows_by_base(st) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for r in st.rows:
        grouped.setdefault(base_id(r.report_id), []).append(r)
    return grouped


def derive_cards(st, pdf_names: dict, xlog: dict) -> list[dict]:
    """현황 + extract 로그 → 문제 카드 목록. 카드는 표시 + 결정 컨트롤(실행 버튼 없음).

    control.type: lane(레인 라디오 — 항상 포함) / lane_opt(포함 체크 + 레인, 기본 포함) /
    detect(승격·플랫 수용·보류 라디오, 기본 보류) / force(--force 재추출 포함 체크, 기본 제외).
    derive_plan이 이 control들과 클라이언트 decisions를 대조해 일괄 해결 계획을 만든다.
    """
    cards: list[dict] = []
    name_of = {b: p.name for b, p in pdf_names.items()}

    for b in st.new_pdfs:
        fname = name_of.get(b)
        if not fname:
            continue  # 이론상 불가 — new_pdfs는 pdfs/에서 파생
        e = xlog.get(fname)
        if e is None:
            cards.append({
                "kind": "new_pdf", "severity": "warn", "files": [fname],
                "title": f"신규 PDF — {fname}",
                "detail": "추출 대기 중입니다. 레인을 고르세요 — structured: 기존 정책보고서류"
                          "(장·절 문법 감지, 실패하면 감지 실패 카드로 전환), flat: 타 기관·"
                          "발표자료·백서류(구조 없이 구간 청킹).",
                "control": {"type": "lane", "pdf": fname, "default": "structured"},
            })
        elif e["status"] in PROMOTABLE:
            cards.append({
                "kind": "detect_fail", "severity": "warn", "files": [fname],
                "title": f"장·절 구조 감지 실패 — {fname}",
                "detail": "기존 문법에 맞지 않는 문서입니다. 승격 = 에이전트가 extract.py에"
                          " 이 문서의 문법 규칙을 추가하고 추출·검증·전체 회귀 대조까지 수행"
                          "합니다(수 분 소요 · LLM 사용량 소모 · 서버를 띄운 기기에서만)."
                          " 플랫 수용 = 구조 없이 구간 청킹으로 추출합니다."
                          " 판단이 어려우면 보류하세요(이번 해결에서 제외).",
                "control": {"type": "detect", "pdf": fname, "default": "skip"},
                "extra": {"l1_attempts": e["l1_attempts"], "l1_suspects": e["l1_suspects"]},
            })
        elif e["status"] == "skipped_flat_guard":
            cards.append({
                "kind": "flat_guard", "severity": "warn", "files": [fname],
                "title": f"본문 부족 — {fname}",
                "detail": "본문 텍스트가 400자 미만(이미지 위주 문서)이라 플랫 청킹도 불가"
                          "합니다. OCR은 별도 과제 — 보류하거나 pdfs/에서 빼 두세요.",
                "control": None,
            })
        else:  # error 등 — 문법 문제가 아니라 코드/파일 문제
            cards.append({
                "kind": "extract_error", "severity": "err", "files": [fname],
                "title": f"추출 오류 — {fname}",
                "detail": "지난 추출에서 오류가 났습니다(문법 문제가 아니라 코드/파일 문제)."
                          " 포함하면 이번 해결에서 다시 시도합니다.",
                "control": {"type": "lane_opt", "pdf": fname, "default": "structured"},
                "extra": {"error": e["error"] or ""},
            })

    for b, rs in sorted(rows_by_base(st).items()):
        parse_fail = [r for r in rs if r.extracted == "파싱 실패"]
        stale = [r for r in rs if any(n.startswith(REEXTRACT_NOTE) for n in r.notes)]
        if not parse_fail and not stale:
            continue
        files = [r.report_id for r in rs if r.extracted != "추출 필요"]
        if not name_of.get(b):
            cards.append({
                "kind": "manual_file", "severity": "err" if parse_fail else "warn",
                "files": files,
                "title": f"원본 PDF 없음 — {b}",
                "detail": "재추출이 필요해 보이나 pdfs/에 원본이 없어 자동 조치가 불가합니다."
                          " 원본 PDF를 복원하거나 .md를 직접 확인하세요.",
                "control": None,
                "extra": {"errors": [n for r in parse_fail for n in r.notes[:1]]},
            })
            continue
        lane = "flat" if any("플랫 구조" in n for r in rs for n in r.notes) else "structured"
        cards.append({
            "kind": "reextract", "severity": "err" if parse_fail else "warn",
            "files": files,
            "title": f"재추출 검토 — {b}",
            "detail": ("원본 .md를 읽을 수 없습니다(파싱 실패) — 재추출로만 복구됩니다. "
                       if parse_fail else
                       "PDF가 .md보다 최신입니다(mtime 기반 추정 — Windows 복사는 놓칠 수"
                       " 있음). ")
                      + "포함하면 --force 재추출부터 검증·요약 재생성·DB 반영까지 다시"
                        " 돌립니다. 기존 요약·검증 스탬프가 소실되고 요약 재생성에 LLM"
                        " 사용량이 듭니다 — 기본 제외.",
            "control": {"type": "force", "pdf": name_of[b], "lane": lane, "default": False},
            "extra": {"errors": [n for r in parse_fail for n in r.notes[:1]]},
        })

    unverified = [r.report_id for r in st.rows if r.extracted == "완료" and r.verify == "미검증"]
    if unverified:
        cards.append({
            "kind": "verify_needed", "severity": "warn", "files": unverified,
            "title": f"추출 검증 필요 — {len(unverified)}건",
            "detail": "추출 결과를 PDF와 전수 대조하고 검증 스탬프를 기록합니다"
                      " (일괄 해결에 자동 포함).",
            "control": None,
        })
    need = [r for r in st.rows if r.remaining > 0]
    if need:
        cards.append({
            "kind": "summarize_needed", "severity": "warn",
            "files": [r.report_id for r in need],
            "title": f"요약 생성 필요 — {len(need)}개 파일"
                     f" (잔여 호출 {sum(r.remaining for r in need)}건)",
            "detail": "요약 없는 유닛만 증분 생성합니다 — LLM 사용량이 듭니다"
                      " (일괄 해결에 자동 포함).",
            "control": None,
        })
    quality = [r.report_id for r in st.rows if r.verify == "extract" and not r.remaining]
    if quality:
        cards.append({
            "kind": "quality_wait", "severity": "warn", "files": quality,
            "title": f"요약 품질 검증 대기 — {len(quality)}건",
            "detail": "생성된 요약을 근거 본문과 대조해 품질을 판정하고, 통과하면 검증"
                      " 스탬프를 기록합니다 (일괄 해결에 자동 포함).",
            "control": None,
        })
    db_text, db_todo = st.artifacts[0]
    if db_todo:
        cards.append({
            "kind": "db_stale", "severity": "warn",
            "files": [r.report_id for r in st.rows if r.db == "미반영"],
            "title": "DB 미반영",
            "detail": db_text.split(": ", 1)[-1] + " — 변경된 .md만 증분 재색인합니다"
                      " (일괄 해결에 자동 포함).",
            "control": None,
        })
    idx_text, idx_todo = st.artifacts[1]
    if idx_todo:
        cards.append({
            "kind": "index_stale", "severity": "warn", "files": [],
            "title": "마스터 카탈로그 재생성 필요",
            "detail": idx_text.split(": ", 1)[-1] + " (일괄 해결에 자동 포함).",
            "control": None,
        })
    manual = [f"{r.report_id}: {n}" for r in st.rows if r.extracted == "완료"
              for n in r.notes if not n.startswith(AUTO_NOTES + (REEXTRACT_NOTE,))]
    if manual:
        cards.append({
            "kind": "manual", "severity": "info", "files": [],
            "title": "수동 확인 필요 (일괄 해결 대상 아님)",
            "detail": "자동 조치가 없는 항목입니다 — 원인을 직접 확인하세요.",
            "control": None,
            "extra": {"items": manual},
        })
    return cards


def derive_plan(st, pdf_names: dict, xlog: dict, decisions: dict) -> tuple[list[dict], list[str]]:
    """카드 결정값(decisions) → 하류 누적 실행 계획. 반환 (stages, errors).

    단계별 대상 집합이 하류로 누적된다 — 이번에 추출·재추출되는 파일은 이후 전 단계가
    필요하므로: extract{결정 파일} → verify{+미검증} → annotate{+요약 잔여} →
    verify2{+품질 대기} → build_db → build_index. verify·annotate·verify2의 실제 대상은
    실행 직전 stage_targets가 상태 기준으로 다시 거른다(중도 탈락 처리).
    decisions = {"pdfs": {파일명: structured|flat|promote|skip}, "force": [파일명…]}.
    """
    cards = derive_cards(st, pdf_names, xlog)
    ctls = {c["control"]["pdf"]: c["control"] for c in cards if c.get("control")}
    pdf_dec = decisions.get("pdfs") or {}
    force_sel = decisions.get("force") or []
    if not isinstance(pdf_dec, dict) or not isinstance(force_sel, list):
        return [], ["decisions 형식 오류"]
    allowed = {"lane": {"structured", "flat", "skip"},
               "lane_opt": {"structured", "flat", "skip"},
               "detect": {"promote", "flat", "skip"}}
    errors: list[str] = []
    for f, mode in pdf_dec.items():
        con = ctls.get(f)
        if not con or con["type"] == "force":
            errors.append(f"결정 대상이 아닌 파일: {f}")
        elif mode not in allowed[con["type"]]:
            errors.append(f"허용되지 않는 결정({mode}): {f}")
    for f in force_sel:
        con = ctls.get(f)
        if not con or con["type"] != "force":
            errors.append(f"재추출 대상이 아닌 파일: {f}")
    if errors:
        return [], errors

    promotes: list[str] = []
    groups: dict[tuple[str, bool], list[str]] = {}  # (lane, force) → 파일명들
    for con in ctls.values():
        f = con["pdf"]
        if con["type"] == "force":
            if f in force_sel:
                groups.setdefault((con["lane"], True), []).append(f)
            continue
        mode = pdf_dec.get(f, con["default"])
        if mode == "skip":
            continue
        if mode == "promote":
            promotes.append(f)
        else:
            groups.setdefault((mode, False), []).append(f)

    stages: list[dict] = []
    for f in sorted(promotes):
        stages.append({"step": "promote", "label": f"프로파일 승격 — {f}",
                       "pdf": f, "files": [f]})
    for (lane, force), fs in sorted(groups.items()):
        label = ("재추출 --force" if force else "추출") + f" — {lane} {len(fs)}건"
        stages.append({"step": "extract", "label": label, "lane": lane, "force": force,
                       "pdfs": sorted(fs), "files": sorted(fs)})

    file_base = {p.name: b for b, p in pdf_names.items()}
    entered = {file_base[f] for f in promotes} | \
              {file_base[f] for fs in groups.values() for f in fs}
    v1 = entered | {base_id(r.report_id) for r in st.rows
                    if r.extracted == "완료" and r.verify == "미검증"}
    an = v1 | {base_id(r.report_id) for r in st.rows if r.remaining > 0}
    v2 = an | {base_id(r.report_id) for r in st.rows
               if r.verify == "extract" and not r.remaining}
    for step, bases in (("verify", v1), ("annotate", an), ("verify2", v2)):
        if bases:
            stages.append({"step": step, "label": STAGE_LABELS[step],
                           "bases": sorted(bases), "files": sorted(bases)})
    content = bool(stages)
    if content or st.artifacts[0][1]:
        stages.append({"step": "build_db", "label": STAGE_LABELS["build_db"], "files": []})
    if content or st.artifacts[1][1]:
        stages.append({"step": "build_index", "label": STAGE_LABELS["build_index"], "files": []})
    return stages, []


def stage_targets(step: str, st, bases: list[str]) -> tuple[list[str], list[str]]:
    """단계 실행 직전, 계획된 base들 중 생존분의 CLI 대상과 탈락분을 가른다.

    판정은 로그 포맷이 아니라 현황(상태·스탬프) 기준 — 이전 단계에서 탈락한 파일은
    여기서 걸러져 하류로 내려가지 않는다(예: 추출 실패 → .md 없음, 검증 실패 → 스탬프
    없음, 요약 미완 → remaining>0). 대상 없음(할 일 없음)은 탈락이 아니다.
    """
    grouped = rows_by_base(st)
    targets: list[str] = []
    dropped: list[str] = []
    for b in bases:
        rs = [r for r in grouped.get(b, []) if r.extracted == "완료"]
        if not rs:
            dropped.append(b)
            continue
        if step == "verify":
            targets.append(b)  # verify가 스스로 파트 확장·스탬프 분기
            continue
        if any(r.verify == "미검증" for r in rs):  # 추출 검증 미통과 — LLM 낭비 방지
            dropped.append(b)
            continue
        if step == "annotate":
            targets += [r.report_id for r in rs if r.remaining > 0]
            continue
        # verify2: 요약 미완 파트가 남아 있으면(annotate 중도 실패) 품질 판정 불가
        if any(r.remaining > 0 for r in rs):
            dropped.append(b)
            continue
        targets += [r.report_id for r in rs if r.verify == "extract"]
    return targets, dropped


def api_admin_status(request):  # sync def → starlette가 스레드풀에서 실행
    st = collect_status(status_args())
    ok, msg = check_freshness()
    request.app.state.freshness = (ok, msg)  # 현황판 새로고침 = 캐시 갱신 시점
    cards = derive_cards(st, scan_pdfs(Path("pdfs")), read_extract_log())
    return JSONResponse({
        "cards": cards,
        "rows": [
            {"report_id": r.report_id, "extracted": r.extracted, "summary": r.summary,
             "remaining": r.remaining, "report_summary": r.report_summary,
             "verify": r.verify, "db": r.db, "notes": r.notes}
            for r in st.rows
        ],
        "new_pdfs": st.new_pdfs,
        "artifacts": [{"text": t, "todo": todo} for t, todo in st.artifacts],
        "remaining_total": st.remaining_total,
        "note_count": st.note_count,
        "todo": st.todo,
        "freshness": {"ok": ok, "message": msg},
    })


async def _stream_subprocess(state, job: dict, argv: list[str]) -> int:
    """CLI 1개를 실행하고 stdout+stderr을 job 버퍼에 줄 단위로 쌓는다. 반환 = exit 코드."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(REPO_ROOT), env=env, limit=SUBPROC_LINE_LIMIT,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as e:
        job["lines"].append(f"[serve] 실행 실패: {e}")
        return -1
    state.job_proc = proc
    try:
        assert proc.stdout is not None
        while True:
            try:
                line = await proc.stdout.readline()
            except ValueError:  # LimitOverrunError — 상한 초과 줄은 표시만 하고 계속
                job["lines"].append("[serve] (출력 줄이 너무 길어 일부 생략)")
                continue
            if not line:
                break
            job["lines"].append(line.decode("utf-8", errors="replace").rstrip("\r\n"))
        return await proc.wait()
    finally:
        state.job_proc = None


async def run_job_queue(state, job: dict) -> None:
    """계획된 단계 큐를 순차 실행 — 탈락 파일은 버리고 생존 파일만 끝까지 데려간다.

    단계 exit≠0이어도 큐를 세우지 않는다: 다음 단계 직전 collect_status를 재계산해
    stage_targets가 생존 파일만 데려간다. 예외 = verify·promote의 exit 2(사용 오류·
    인증·한도)는 전 파일 공통 장애라 하드 스톱. 취소(cancel)는 현재 단계 종료 후
    잔여 단계를 건너뛴다.
    """
    n = len(job["stages"])
    try:
        for i, stg in enumerate(job["stages"]):
            if job["cancel"] or job["code"] == 2:
                stg["skipped"] = True
                continue
            job["current"] = i
            targets: list[str] = []
            if stg["step"] in ("verify", "annotate", "verify2"):
                st = await asyncio.to_thread(collect_status, status_args())
                targets, dropped = stage_targets(stg["step"], st, stg["bases"])
                stg["dropped"] = dropped
                if dropped:
                    job["lines"].append(f"[serve] 이전 단계 미통과로 제외 {len(dropped)}건: "
                                        + ", ".join(dropped))
            elif stg["step"] == "extract":
                targets = [f for f in stg["pdfs"] if (REPO_ROOT / "pdfs" / f).is_file()]
                stg["dropped"] = sorted(set(stg["pdfs"]) - set(targets))
            elif stg["step"] == "promote":
                if (REPO_ROOT / "pdfs" / stg["pdf"]).is_file():
                    targets = [stg["pdf"]]
                else:
                    stg["dropped"] = [stg["pdf"]]
            if stg["step"] not in ("build_db", "build_index") and not targets:
                stg["skipped"] = True
                job["lines"].append(f"[serve] ── 단계 {i + 1}/{n}: {stg['label']} — 대상 없음, 생략")
                continue
            job["lines"].append(f"[serve] ── 단계 {i + 1}/{n}: {stg['label']}"
                                + (f" (대상 {len(targets)}건)" if targets else ""))
            argv = stage_argv(stg, targets)
            job["lines"].append("[serve] 실행: "
                                + " ".join(Path(a).name if a == argv[0] else a for a in argv))
            code = await _stream_subprocess(state, job, argv)
            stg["code"] = code
            # exit 2의 의미가 CLI마다 다르다: verify·promote는 사용 오류·인증·한도(공통
            # 장애)지만 extract·annotate는 "일부 파일 실패"를 포함 — 후자는 세우지 않고
            # 상태 기준 탈락 처리(stage_targets)로 생존 파일을 계속 데려간다.
            if code == 2 and stg["step"] in ("verify", "verify2", "promote"):
                job["code"] = 2
                job["lines"].append("[serve] 종료 코드 2(사용 오류·인증·사용량 한도) — "
                                    "공통 장애로 보고 남은 단계를 중단합니다.")
    finally:
        if job["code"] != 2:
            flawed = (job["cancel"]
                      or any(s["code"] not in (0, None) for s in job["stages"])
                      or any(s["dropped"] for s in job["stages"]))
            job["code"] = 1 if flawed else 0
        job["running"] = False
        state.job_proc = None
        try:  # 파이프라인이 아티팩트를 바꿨을 수 있음 — freshness 캐시 갱신 시점
            state.freshness = await asyncio.to_thread(check_freshness)
        except Exception:
            pass


async def api_admin_resolve(request):
    """일괄 해결 — decisions 기반 누적 계획을 만들고(dry_run=미리보기) job 큐로 실행한다."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON 본문이 필요합니다."}, status_code=400)
    decisions = payload.get("decisions") or {}
    if not isinstance(decisions, dict):
        return JSONResponse({"error": "decisions 형식 오류"}, status_code=400)

    def compute():
        st = collect_status(status_args())
        return derive_plan(st, scan_pdfs(Path("pdfs")), read_extract_log(), decisions)

    stages, errors = await asyncio.to_thread(compute)
    if errors:
        return JSONResponse({"error": "; ".join(errors)}, status_code=400)
    public = [{"step": s["step"], "label": s["label"], "files": s["files"]} for s in stages]
    if payload.get("dry_run"):
        return JSONResponse({"stages": public})
    if not stages:
        return JSONResponse({"error": "실행할 단계가 없습니다 — 이미 최신 상태입니다."},
                            status_code=400)
    state = request.app.state
    if state.job and state.job["running"]:
        return JSONResponse({"error": "이미 작업이 진행 중입니다."}, status_code=409)
    if state.active_searches > 0:
        return JSONResponse(
            {"error": "질의가 진행 중입니다 — 완료 후 DB 작업을 실행하세요."}, status_code=409
        )
    if any(s["step"] == "promote" or s.get("force") for s in stages):
        # 코드 수정(promote)·요약 소실(--force)을 수반하는 해결은 서버 기동 기기에서만
        client_host = request.client.host if request.client else ""
        if client_host not in ("127.0.0.1", "::1"):
            return JSONResponse(
                {"error": "프로파일 승격·--force 재추출이 포함된 해결은 서버를 띄운 기기의"
                          " 브라우저에서만 실행할 수 있습니다."}, status_code=403)
    state.job_seq += 1
    job = {
        "id": state.job_seq,
        "stages": [dict(s, code=None, skipped=False, dropped=[]) for s in stages],
        "current": -1,
        "lines": [],
        "code": None,
        "running": True,
        "acked": False,  # [확인]으로 닫은 완료 기록 — 재접속 시 재생하지 않음
        "cancel": False,
    }
    state.job = job
    state.job_task = asyncio.create_task(run_job_queue(state, job))
    return JSONResponse({"id": job["id"], "stages": public})


async def api_admin_job(request):
    """job 버퍼 폴링 — after 커서 이후 줄만 증분 전달(새로고침 시 0부터 재생)."""
    job = request.app.state.job
    if job is None:
        return JSONResponse({"exists": False})
    try:
        after = max(0, int(request.query_params.get("after", 0)))
    except ValueError:
        after = 0
    return JSONResponse({
        "exists": True,
        "id": job["id"],
        "running": job["running"],
        "code": job["code"],
        "acked": job["acked"],
        "current": job["current"],
        "stages": [
            {"step": s["step"], "label": s["label"], "files": s["files"],
             "dropped": s["dropped"], "code": s["code"], "skipped": s["skipped"]}
            for s in job["stages"]
        ],
        "total": len(job["lines"]),
        "lines": job["lines"][after:],
    })


async def api_admin_job_ack(request):
    """완료된 작업 기록을 닫는다 — 이후 새로고침·재접속에서 그 기록을 재생하지 않는다."""
    job = request.app.state.job
    if job is None or job["running"]:
        return JSONResponse({"error": "닫을 완료 작업이 없습니다."}, status_code=409)
    job["acked"] = True
    return JSONResponse({"ok": True})


async def api_admin_cancel(request):
    state = request.app.state
    job, proc = state.job, state.job_proc
    if not (job and job["running"]):
        return JSONResponse({"error": "진행 중인 작업이 없습니다."}, status_code=409)
    job["cancel"] = True  # 현재 단계 종료 후 잔여 단계 건너뜀
    job["lines"].append("[serve] 중지 요청 — 현재 단계 프로세스 트리 강제 종료, 잔여 단계 건너뜀"
                        " (다시 일괄 해결하면 남은 작업부터 이어짐)")
    if proc:
        if sys.platform == "win32":
            # terminate()는 자식(SDK가 띄운 claude CLI)을 남기므로 트리째 종료
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(proc.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            proc.terminate()
    return JSONResponse({"ok": True})


def api_admin_reports(request):
    con = open_db_ro()
    if con is None:
        return JSONResponse({"error": "reports.db 없음 — DB 관리에서 일괄 해결을 실행하세요."},
                            status_code=404)
    try:
        rows = con.execute(
            "SELECT filepath, report_title, year, count(*),"
            " sum(CASE WHEN summary != '' THEN 1 ELSE 0 END)"
            " FROM sections GROUP BY filepath ORDER BY filepath"
        ).fetchall()
    finally:
        con.close()
    return JSONResponse({"reports": [
        {"filepath": fp, "report_id": Path(fp).stem, "title": title, "year": year,
         "sections": n, "summarized": s or 0}
        for fp, title, year, n, s in rows
    ]})


def api_admin_toc(request):
    fp = request.query_params.get("filepath", "")
    con = open_db_ro()
    if con is None:
        return JSONResponse({"error": "reports.db 없음"}, status_code=404)
    try:
        rows = con.execute(
            "SELECT section_id, chapter, section, summary, length(body)"
            " FROM sections WHERE filepath = ? ORDER BY rowid",  # 삽입 순 = 문서 순
            (fp,),
        ).fetchall()
    finally:
        con.close()
    if not rows:
        return JSONResponse({"error": f"DB에 없는 filepath: {fp}"}, status_code=404)
    return JSONResponse({"sections": [
        {"section_id": sid, "chapter": ch, "section": sec, "summary": summ, "body_chars": n}
        for sid, ch, sec, summ, n in rows
    ]})


def api_admin_master_index(request):
    p = Path("master_index.md")
    if not p.exists():
        return JSONResponse({"error": "master_index.md 없음 — DB 관리에서 카탈로그를 재생성하세요."},
                            status_code=404)
    return JSONResponse({"text": p.read_text(encoding="utf-8")})


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(
        description="검색 테스트 웹 서버 — 브라우저에서 질의→답변 (127.0.0.1 전용)."
    )
    parser.add_argument("--port", type=int, default=8765, help="포트 (기본: 8765)")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="바인딩 주소 (기본: 127.0.0.1 = 이 PC 전용; 팀 테스트는 0.0.0.0 = LAN 개방)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="동시 질의 상한 (기본: 1; 초과 요청은 409) — 질의 1건 = LLM 세션 1개 사용량",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"에이전트 모델 (기본: {DEFAULT_MODEL})")
    parser.add_argument("--top-n", type=int, default=TOP_N, help=f"본문 전달 절 수 (기본: {TOP_N})")
    parser.add_argument("--max-turns", type=int, default=MAX_TURNS, help=f"세션 턴 상한 (기본: {MAX_TURNS})")
    parser.add_argument("--timeout", type=float, default=600, help="질의당 타임아웃 초 (기본: 600)")
    parser.add_argument("--thinking", action="store_true", help="모델 thinking 활성화 (기본: 비활성)")
    parser.add_argument("--allow-stale", action="store_true", help="DB가 .md보다 낡아도 강행 (기본: 질의 차단)")
    parser.add_argument(
        "--token-file",
        default=str(REPO_ROOT / ".claude_oauth_token"),
        help="claude setup-token 발급 토큰 파일 (기본: 저장소 루트 .claude_oauth_token)",
    )
    args = parser.parse_args()

    # 상대경로 기본값·ledger 키(reports/x.md)가 CLI와 동일하게 성립하도록 루트 고정
    os.chdir(REPO_ROOT)

    for page in (INDEX_HTML, ADMIN_HTML, BROWSE_HTML):
        if not page.exists():
            print(f"web 페이지 없음({page}).", file=sys.stderr)
            sys.exit(2)
    # DB·카탈로그 부재/불일치는 기동 차단 사유가 아님 — /admin에서 복구 가능.
    # 검사는 기동 시 1회, 이후는 DB 작업 완료·현황판 새로고침 시에만 캐시 갱신.
    ok, msg = check_freshness()
    if not ok:
        print(f"[serve] DB 미동기화 — {msg}", file=sys.stderr)
        print(
            "[serve] 질의는 차단됩니다(--allow-stale로 강행 가능) — 브라우저 /admin에서 파이프라인 실행.",
            file=sys.stderr,
        )

    os.environ.setdefault("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
    setup_auth(args)

    if sys.platform == "win32":
        # SDK가 CLI 서브프로세스를 spawn — Selector 루프(uvicorn 기본 설치 경로)는
        # subprocess 미지원이므로 Proactor를 명시하고, uvicorn.run() 대신
        # Server.serve()를 우리 루프에서 돌려 uvicorn의 정책 덮어쓰기를 우회한다.
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    app = Starlette(
        routes=[
            Route("/", index),
            Route("/admin", admin_page),
            Route("/browse", browse_page),
            Route("/api/search", api_search, methods=["POST"]),
            Route("/api/meta", api_meta),
            Route("/api/admin/status", api_admin_status),
            Route("/api/admin/resolve", api_admin_resolve, methods=["POST"]),
            Route("/api/admin/job", api_admin_job),
            Route("/api/admin/cancel", api_admin_cancel, methods=["POST"]),
            Route("/api/admin/job/ack", api_admin_job_ack, methods=["POST"]),
            Route("/api/admin/reports", api_admin_reports),
            Route("/api/admin/toc", api_admin_toc),
            Route("/api/admin/master-index", api_admin_master_index),
        ]
    )
    app.state.base_args = args
    app.state.freshness = (ok, msg)  # 기동 시 1회 검사 결과 캐시
    app.state.search_sem = asyncio.Semaphore(max(1, args.concurrency))
    app.state.active_searches = 0
    app.state.job = None  # 마지막(또는 진행 중) DB 작업 — 싱글턴
    app.state.job_proc = None
    app.state.job_task = None
    app.state.job_seq = 0

    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
    server = uvicorn.Server(config)
    print(
        f"[serve] http://127.0.0.1:{args.port} — model={args.model}, top_n={args.top_n}, "
        f"timeout={args.timeout:.0f}s, concurrency={max(1, args.concurrency)}. Ctrl+C로 종료.",
        file=sys.stderr,
    )
    if args.host not in ("127.0.0.1", "localhost"):
        ip = lan_ip()
        print(
            f"[serve] LAN 개방({args.host}) — 팀 접속 주소: "
            + (f"http://{ip}:{args.port}" if ip else f"http://<이 PC의 IP>:{args.port}")
            + " (엔드포인트 무인증 — 질의 1건 = LLM 사용량 소모, 신뢰망에서만. "
            "접속 안 되면 Windows 방화벽에서 Python 허용 확인)",
            file=sys.stderr,
        )
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()
