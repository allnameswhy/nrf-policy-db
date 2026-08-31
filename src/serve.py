"""로컬 검색 테스트 서버 — search.perform_search를 얇게 감싼다 (사용 레이어, 파이프라인 4단계 밖).

브라우저에서 질의→답변을 직접 테스트하기 위한 소형 서버. POST /api/search가
진행 이벤트(턴·툴 요약)를 NDJSON으로 실시간 스트리밍하고 마지막에 답변+출처를 내려준다.

- 바인딩 기본 127.0.0.1 — 구독 인증된 LLM 호출을 함부로 노출하지 않는다.
  팀 테스트는 `--host 0.0.0.0`으로 LAN 개방(기동 로그에 접속 URL 출력; 엔드포인트에
  별도 인증이 없으므로 신뢰망에서만 — 질의 1건 = 실제 LLM 사용량 소모).
- freshness_gate·setup_auth는 기동 시 1회 — 가동 중 .md/DB 변경은 감지하지 못한다(재기동 필요).
- 동시 질의 상한 `--concurrency`(기본 1) — 초과는 409 즉시 거부(대기 큐 없음).
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
import sys
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from annotate import AuthError, RetryableError, UsageLimitReached, fatal_msg, setup_auth
from search import DEFAULT_MODEL, MAX_TURNS, TOP_N, freshness_gate, perform_search

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO_ROOT / "web" / "index.html"
PING_INTERVAL = 15  # 무이벤트 keep-alive 초 — LAN에서도 사실상 보험


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


def lan_ip() -> str | None:
    """기본 라우트 인터페이스의 IPv4 — UDP connect 트릭(패킷은 실제 전송되지 않음)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return None


async def index(request):
    return FileResponse(INDEX_HTML)


async def api_search(request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON 본문이 필요합니다."}, status_code=400)
    question = str(payload.get("question", "")).strip()
    if not question:
        return JSONResponse({"error": "질문이 비어 있습니다."}, status_code=400)
    sem: asyncio.Semaphore = request.app.state.search_sem
    if sem.locked():  # 카운터 0 = 동시 상한 도달
        return JSONResponse(
            {"error": "동시 질의 상한에 도달했습니다 — 진행 중인 질의가 끝난 뒤 다시 시도하세요."},
            status_code=409,
        )

    queue: asyncio.Queue = asyncio.Queue()

    async def worker() -> None:
        try:
            result = await perform_search(
                question, make_args(request.app.state.base_args), emit=queue.put_nowait
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
                task.cancel()

    return StreamingResponse(stream(), media_type="application/x-ndjson")


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
    parser.add_argument("--allow-stale", action="store_true", help="DB가 .md보다 낡아도 강행 (기본: 중단)")
    parser.add_argument(
        "--token-file",
        default=str(REPO_ROOT / ".claude_oauth_token"),
        help="claude setup-token 발급 토큰 파일 (기본: 저장소 루트 .claude_oauth_token)",
    )
    args = parser.parse_args()

    # 상대경로 기본값·ledger 키(reports/x.md)가 CLI와 동일하게 성립하도록 루트 고정
    os.chdir(REPO_ROOT)

    if not Path("reports.db").exists():
        print("reports.db 없음 — src/build_db.py 실행 필요.", file=sys.stderr)
        sys.exit(2)
    freshness_gate(Path("reports.db"), Path("reports"), args.allow_stale)  # 실패 시 기동 중단
    if not INDEX_HTML.exists():
        print(f"web/index.html 없음({INDEX_HTML}).", file=sys.stderr)
        sys.exit(2)
    if not Path("master_index.md").exists():
        print("마스터 카탈로그 없음(master_index.md) — src/build_index.py 실행 필요.", file=sys.stderr)
        sys.exit(2)

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
            Route("/api/search", api_search, methods=["POST"]),
        ]
    )
    app.state.base_args = args
    app.state.search_sem = asyncio.Semaphore(max(1, args.concurrency))

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
