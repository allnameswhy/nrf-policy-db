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
- /admin = DB 관리 페이지(2026-09): 현황판(status.collect_status 재사용) + **필요 단계
  자동 감지(derive_todo)** — 현황에서 extract/verify/annotate/verify2/build_db/build_index
  중 지금 필요한 단계를 도출하고, 단계 완료마다 재도출해 다음 필요 단계를 제안한다
  (--force 재추출 등 파괴적 조치는 자동 실행하지 않고 수동 확인 항목으로 고지만).
  레인 선택 후 파이프라인 단계별 실행, DB 목차·요약 브라우저 + 마스터 카탈로그 뷰.
  단계 실행 = 기존 파이프라인 CLI를 서브프로세스로 호출(로직 재구현 없음, exit 코드가
  특이사항 판정 기준) — 출력은 서버 버퍼(단일 job)에 쌓고 브라우저가 폴링하므로
  새로고침·이탈에도 작업은 계속되고 재접속된다. 질의↔DB 작업은 상호 배제(409).
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
from status import collect_status, read_db_ledger

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO_ROOT / "web" / "index.html"
ADMIN_HTML = REPO_ROOT / "web" / "admin.html"
PING_INTERVAL = 15  # 무이벤트 keep-alive 초 — LAN에서도 사실상 보험
SUBPROC_LINE_LIMIT = 1 << 20  # readline 상한 — 기본 64KB로는 긴 진단 줄이 끊길 수 있음

# 파이프라인 단계 → CLI argv. 표준 절차 순서(CLAUDE.md): extract → verify →
# annotate → verify 재실행 → build_db → build_index. verify2 = verify 재실행(D 판정).
PIPELINE_STEPS = ("extract", "verify", "annotate", "verify2", "build_db", "build_index")


def step_argv(step: str, lane: str | None) -> list[str]:
    py = sys.executable
    if step == "extract":
        # 글롭은 extract가 자체 확장. 기존 출력이 있는 PDF는 기본 스킵되므로
        # 전체 글롭이어도 신규 PDF만 처리된다 — 레인 선택도 신규분에만 적용.
        argv = [py, "src/extract.py", "pdfs/*.pdf"]
        if lane in ("structured", "flat"):
            argv += ["--lane", lane]
        return argv
    if step in ("verify", "verify2"):
        return [py, "src/verify.py"]  # 스탬프 분기가 검사 범위를 스스로 고른다
    if step == "annotate":
        return [py, "src/annotate.py", "reports/*.md"]  # 요약 없는 유닛만 생성(증분)
    if step == "build_db":
        return [py, "src/build_db.py"]  # 증분 동기화
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
        return False, "reports.db 없음 — 파이프라인 ⑤ build_db까지 실행해야 검색할 수 있습니다."
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
        return False, "master_index.md 없음 — 파이프라인 ⑥ build_index 실행 필요."
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
            {"error": f"DB 관리 작업({state.job['step']}) 진행 중 — 완료 후 다시 질의하세요."},
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


def derive_todo(st) -> tuple[list[dict], list[str]]:
    """현황(StatusData)에서 지금 필요한 파이프라인 단계와 수동 확인 항목을 도출한다.

    단계 완료 후 현황을 다시 계산해 재도출하는 전제(예: extract 뒤에야 새 파일의
    verify 필요가 드러남) — 고정 체인이 아니라 현재 상태의 스냅샷. 파괴적 조치
    (--force 재추출 등)가 필요한 문제는 자동 단계에 넣지 않고 manual로 고지만 한다.
    """
    steps: list[dict] = []
    if st.new_pdfs:
        steps.append({"step": "extract",
                      "reason": f"신규 PDF {len(st.new_pdfs)}건: {', '.join(st.new_pdfs)}"})
    unverified = [r.report_id for r in st.rows if r.extracted == "완료" and r.verify == "미검증"]
    if unverified:
        steps.append({"step": "verify",
                      "reason": f"추출 미검증 {len(unverified)}건: {', '.join(unverified)}"})
    need_sum = [r.report_id for r in st.rows if r.remaining > 0]
    if need_sum:
        steps.append({"step": "annotate",
                      "reason": f"요약 잔여 호출 {st.remaining_total}건 ({len(need_sum)}개 파일)"})
    promote = [r.report_id for r in st.rows if r.verify == "extract" and not r.remaining]
    revisit = [r.report_id for r in st.rows if r.verify == "annotate" and r.remaining]
    if promote or revisit:
        parts = ([f"승격 대기 {len(promote)}건"] if promote else []) + \
                ([f"검증 후 변경 {len(revisit)}건(annotate 뒤 재판정)"] if revisit else [])
        steps.append({"step": "verify2", "reason": "D 품질 판정 — " + " · ".join(parts)})
    for (text, todo), step in zip(st.artifacts, ("build_db", "build_index")):
        if todo:
            steps.append({"step": step, "reason": text.split(": ", 1)[-1]})
    # 자동 단계가 처리하는 노트(승격 대기·검증 후 변경)와 정보성 노트(플랫 구조)를 뺀
    # 나머지 = 사람이 봐야 하는 항목 (재추출 검토·파싱 실패·규약 위반류)
    auto_or_info = ("verify 승격 대기", "검증 후 변경", "플랫 구조")
    manual = [f"{r.report_id}: {n}" for r in st.rows for n in r.notes
              if not n.startswith(auto_or_info)]
    return steps, manual


def api_admin_status(request):  # sync def → starlette가 스레드풀에서 실행
    st = collect_status(status_args())
    ok, msg = check_freshness()
    request.app.state.freshness = (ok, msg)  # 현황판 새로고침 = 캐시 갱신 시점
    todo_steps, manual = derive_todo(st)
    return JSONResponse({
        "todo_steps": todo_steps,
        "manual": manual,
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


async def run_job(state, job: dict, argv: list[str]) -> None:
    """파이프라인 CLI 1개를 실행하고 stdout+stderr을 job 버퍼에 줄 단위로 쌓는다."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(REPO_ROOT), env=env, limit=SUBPROC_LINE_LIMIT,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as e:
        job["lines"].append(f"[serve] 실행 실패: {e}")
        job["code"], job["running"] = -1, False
        return
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
        job["code"] = await proc.wait()
    finally:
        job["running"] = False
        state.job_proc = None
        try:  # 파이프라인이 아티팩트를 바꿨을 수 있음 — freshness 캐시 갱신 시점
            state.freshness = await asyncio.to_thread(check_freshness)
        except Exception:
            pass


async def api_admin_run(request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON 본문이 필요합니다."}, status_code=400)
    step = str(payload.get("step", ""))
    lane = payload.get("lane")
    if step not in PIPELINE_STEPS:
        return JSONResponse({"error": f"알 수 없는 단계: {step}"}, status_code=400)
    if lane is not None and lane not in ("structured", "flat", "auto"):
        return JSONResponse({"error": f"알 수 없는 레인: {lane}"}, status_code=400)
    state = request.app.state
    if state.job and state.job["running"]:
        return JSONResponse(
            {"error": f"이미 작업({state.job['step']})이 진행 중입니다."}, status_code=409
        )
    if state.active_searches > 0:
        return JSONResponse(
            {"error": "질의가 진행 중입니다 — 완료 후 DB 작업을 실행하세요."}, status_code=409
        )
    state.job_seq += 1
    job = {
        "id": state.job_seq,
        "step": step,
        "lane": lane if step == "extract" else None,
        "lines": [],
        "code": None,
        "running": True,
    }
    state.job = job
    argv = step_argv(step, job["lane"])
    job["lines"].append("[serve] 실행: " + " ".join(Path(a).name if a == argv[0] else a for a in argv))
    state.job_task = asyncio.create_task(run_job(state, job, argv))
    return JSONResponse({"id": job["id"], "step": step})


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
        "step": job["step"],
        "lane": job["lane"],
        "running": job["running"],
        "code": job["code"],
        "total": len(job["lines"]),
        "lines": job["lines"][after:],
    })


async def api_admin_cancel(request):
    state = request.app.state
    job, proc = state.job, state.job_proc
    if not (job and job["running"] and proc):
        return JSONResponse({"error": "진행 중인 작업이 없습니다."}, status_code=409)
    job["lines"].append("[serve] 중지 요청 — 프로세스 트리 강제 종료(재실행하면 남은 작업부터 이어짐)")
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
        return JSONResponse({"error": "reports.db 없음 — 파이프라인 ⑤ build_db까지 실행하세요."},
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
        return JSONResponse({"error": "master_index.md 없음 — ⑥ build_index 실행 필요."},
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

    for page in (INDEX_HTML, ADMIN_HTML):
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
            Route("/api/search", api_search, methods=["POST"]),
            Route("/api/meta", api_meta),
            Route("/api/admin/status", api_admin_status),
            Route("/api/admin/run", api_admin_run, methods=["POST"]),
            Route("/api/admin/job", api_admin_job),
            Route("/api/admin/cancel", api_admin_cancel, methods=["POST"]),
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
