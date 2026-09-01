"""프로파일 승격 러너 — /admin 감지 실패 패널의 [예]가 서브프로세스로 호출 (사용 레이어, 파이프라인 4단계 아님).

`extract --lane structured` 감지 실패 문서에 대해 에이전트(claude-agent-sdk,
annotate와 동일 인증 인프라)가 CLAUDE.md의 프로파일 승격 절차를 수행한다:
진단(--scan) → PROFILES 규칙 추가 → extract → verify 풀 검사(C 목차 대조) →
기존 전체 재추출 회귀 대조(임시 디렉터리). 코드 수정이 수반되므로 serve.py는
이 스텝을 루프백 요청에만 허용한다.

이중 방어: 수칙은 프롬프트에 명시하고, 종료 후 `git status --porcelain` 전후
대조(결정론 사후 가드)로 변경 범위가 src/extract.py + 신규 reports/*.md + logs/
를 벗어나면 성공 주장이라도 실패로 강등한다. git commit/add는 금지(커밋은 사용자).

종료 규약: 에이전트 최종 보고 마지막 줄 `PROMOTE_RESULT: success|rejected|failed — 사유`.
exit 0 = success + 가드 통과, 1 = rejected/failed/가드 위반/타임아웃, 2 = 사용 오류·인증·한도.

사용: python src/promote.py "pdfs/<파일명>.pdf" [--model claude-sonnet-5] [--timeout 1800]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from annotate import (
    AuthError,
    UsageLimitReached,
    _limit_desc,
    fatal_msg,
    setup_auth,
)
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    RateLimitEvent,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)
from extract import derive_report_id_any

REPO_ROOT = Path(__file__).resolve().parent.parent

RESULT_RE = re.compile(r"PROMOTE_RESULT:\s*(success|rejected|failed)", re.IGNORECASE)

SYSTEM_PROMPT = (
    "너는 nrf-policy-db 저장소의 extract 프로파일 승격을 수행하는 엔지니어 에이전트다. "
    "주어진 절차와 수칙을 엄수하고, 확신이 없으면 무리한 승격 대신 부적합(rejected)으로 "
    "종료한다. 모든 검증은 결정론 실행 결과로만 판단한다."
)


def build_prompt(pdf_rel: str, rid: str, scratch: str, diag: dict | None) -> str:
    py = ".venv/Scripts/python.exe" if sys.platform == "win32" else ".venv/bin/python"
    diag_txt = ""
    if diag:
        diag_txt = (
            "\n## 직전 실패 진단 (참고 — ①에서 신선한 진단을 다시 얻어라)\n"
            f"- l1_attempts: {json.dumps(diag.get('l1_attempts'), ensure_ascii=False)}\n"
            f"- l1_suspects: {json.dumps(diag.get('l1_suspects'), ensure_ascii=False)}\n"
        )
    return f"""아래 절차로 프로파일 승격을 수행하라. 작업 디렉터리는 저장소 루트, 셸은 bash다.

## 배경
이 저장소의 src/extract.py는 정책보고서 PDF를 장-절 구조 .md로 추출한다. L1(장) 문법은
extract.py 상단의 PROFILES 선언형 표 — (이름, 컴파일된 정규식, 두줄형 여부) 튜플 목록 —
로 감지하고, 하위 헤딩은 SUB_FAMILY_DEFS 캐스케이드 + detect_sub_headings의 프로파일별
특례로 처리한다. 대상 문서가 기존 프로파일 어디에도 맞지 않아 구조 감지에 실패했다.
새 L1 규칙을 추가해 이 문서를 구조 레인으로 수용하는 것이 과제다.

## 대상
- PDF: {pdf_rel}
- report_id: {rid} (합본이면 {rid}_01 등 파트 접미사)
- 임시 작업 디렉터리(저장소 밖): {scratch}
{diag_txt}
## 절차 (순서 엄수)
0. 백업: cp src/extract.py {scratch}/extract.py.bak
1. 진단: {py} src/extract.py "{pdf_rel}" --scan --lane structured --log {scratch}/scan.json
   출력 JSON의 l1_attempts(후보별 정상 장 수)·l1_suspects(헤딩 의심 줄 샘플, 페이지 포함)로
   이 문서의 장 문법을 파악한다. 부족하면 {py} -c 로 pymupdf를 직접 사용해 해당 페이지
   텍스트를 확인해도 된다(PDF를 다른 도구로 열 수는 없다).
2. 규칙 추가: src/extract.py의 PROFILES에 새 튜플 1개를 추가한다(이름은 문법을 설명하는
   영문 스네이크, 기존 항목의 순서·정규식은 변경 금지). 하위 헤딩 특례가 필요할 때만
   SUB_FAMILY_DEFS·detect_sub_headings에 최소 변경.
3. 추출: {py} src/extract.py "{pdf_rel}" --lane structured
   → reports/에 .md가 생성되고 로그의 profile이 새 프로파일 이름이어야 한다(flat 아님).
4. 검증: {py} src/verify.py {rid} --no-llm
   PASS 필수 — 특히 C 목차 대조가 오분할을 잡는 핵심 안전망이다. FAIL이면 규칙을 고쳐
   3부터 재시도(재추출 전 reports/의 해당 .md 삭제).
5. 회귀: {py} src/extract.py "pdfs/*.pdf" -o {scratch}/reex --log {scratch}/reex.json --force
   로 기존 PDF 전체를 임시 디렉터리에 재추출한 뒤, 대상 문서를 제외한 모든 .md가 기존
   reports/*.md와 동일한지 대조한다 — 기존본에서 검증 스탬프와 요약 인용 블록을 제거한 뒤
   비교해야 한다(src/mdio.py의 read_md_lines·strip_verified_stamps·strip_summary_blocks
   재사용, 대조 스크립트는 {scratch}에 작성). 한 파일이라도 달라지면 실패다.

주의: reports/를 -o 대상으로 --force 재추출하지 마라(기존 요약 소실). 회귀 재추출은
반드시 {scratch}/reex 로만.

## 수칙 (위반 금지)
- git commit / git add 금지 — 커밋은 사용자 몫. git은 status/diff 조회만 허용.
- 수정 허용 범위: src/extract.py 와 신규 reports/{rid}*.md 뿐. 기존 reports/*.md,
  다른 소스 파일, 문서(.md) 수정 금지.
- 문법이 문서 안에서 일관되지 않거나 규칙에 확신이 없으면 승격하지 말고 rejected로 종료.
- rejected/failed로 끝낼 때는 원상 복구를 먼저 한다: cp {scratch}/extract.py.bak src/extract.py
  + 이번에 만든 reports/{rid}*.md 삭제.

## 종료 보고 (마지막 줄 규약 — 반드시 지켜라)
마지막 출력 줄을 정확히 다음 형식으로 쓴다:
PROMOTE_RESULT: success — <추가한 프로파일 이름과 근거 1문장>
PROMOTE_RESULT: rejected — <부적합 사유>
PROMOTE_RESULT: failed — <실패 사유>
"""


def git_porcelain() -> list[str]:
    # core.quotepath=false 필수 — 기본값이면 한글 경로가 8진수 이스케이프로 출력돼
    # 사후 가드의 허용 목록 매칭이 깨진다(승격 E2E 실측: 성공이 위반으로 강등).
    out = subprocess.run(
        ["git", "-c", "core.quotepath=false", "status", "--porcelain"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, encoding="utf-8",
    )
    return [l for l in out.stdout.splitlines() if l.strip()]


def guard_violations(before: list[str], after: list[str], rid: str) -> list[str]:
    """실행 전후 git status 차이 중 허용 범위 밖 변경 — 성공 주장 강등 사유."""
    new = [l for l in after if l not in before]
    bad = []
    for line in new:
        st, path = line[:2], line[3:].strip().strip('"').replace("\\", "/")
        if path == "src/extract.py":
            continue
        if path.startswith("logs/"):
            continue
        if st.strip() == "??" and re.fullmatch(rf"reports/{re.escape(rid)}(_\d{{2}})?\.md", path):
            continue
        bad.append(line)
    return bad


def read_last_diag(pdf_name: str) -> dict | None:
    """logs/extract_log.json에서 해당 파일의 마지막 실패 진단(참고용 주입)."""
    try:
        data = json.loads((REPO_ROOT / "logs" / "extract_log.json").read_text(encoding="utf-8"))
        for e in data.get("files", []):
            if e.get("file") == pdf_name and str(e.get("status", "")).startswith("skipped_"):
                return {"l1_attempts": e.get("l1_attempts"), "l1_suspects": e.get("l1_suspects")}
    except Exception:
        pass
    return None


async def run_agent(prompt: str, args) -> tuple[str | None, str]:
    """에이전트 1세션. 반환: (판정 success|rejected|failed|None, 전체 텍스트)."""
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        allowed_tools=["Read", "Grep", "Glob", "Edit", "Write", "Bash", "TodoWrite"],
        permission_mode="acceptEdits",
        max_turns=args.max_turns,
        model=args.model,
        setting_sources=[],  # CLAUDE.md 유입 차단 — 프롬프트 자기완결(annotate 관례)
        cwd=str(REPO_ROOT),
    )
    texts: list[str] = []
    result_msg: ResultMessage | None = None

    async def consume() -> None:
        nonlocal result_msg
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, RateLimitEvent):
                info = msg.rate_limit_info
                if getattr(info, "status", None) == "rejected":
                    raise UsageLimitReached(_limit_desc(info))
            elif isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        texts.append(block.text)
                        for ln in block.text.splitlines():
                            if ln.strip():
                                print(f"[promote] {ln}", flush=True)
                    elif isinstance(block, ToolUseBlock):
                        arg_s = json.dumps(block.input, ensure_ascii=False)[:160]
                        print(f"[promote] 도구 {block.name} {arg_s}", flush=True)
            elif isinstance(msg, ResultMessage):
                result_msg = msg

    await asyncio.wait_for(consume(), args.timeout)

    full = "\n".join(texts)
    if result_msg is not None and result_msg.is_error:
        detail = str(result_msg.result or result_msg.subtype)
        low = detail.lower()
        if result_msg.api_error_status == 429:
            raise UsageLimitReached("api_error_status=429")
        if "authenticate" in low or "not logged in" in low or "oauth" in low:
            raise AuthError(detail)
        print(f"[promote] 세션 오류: {detail}", flush=True)
        return "failed", full
    matches = RESULT_RE.findall(full)
    return (matches[-1].lower() if matches else None), full


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="extract 프로파일 승격 에이전트 러너")
    parser.add_argument("pdf", help="대상 PDF 경로 (pdfs/<파일명>.pdf)")
    parser.add_argument("--model", default="claude-sonnet-5", help="에이전트 모델 (기본: claude-sonnet-5)")
    parser.add_argument("--timeout", type=float, default=1800, help="세션 전체 타임아웃 초 (기본: 1800)")
    parser.add_argument("--max-turns", type=int, default=150, help="에이전트 최대 턴 (기본: 150)")
    parser.add_argument(
        "--token-file",
        default=str(REPO_ROOT / ".claude_oauth_token"),
        help="장기 OAuth 토큰 파일 (annotate와 동일 규약)",
    )
    args = parser.parse_args()

    pdf = Path(args.pdf)
    if not pdf.is_absolute():
        pdf = REPO_ROOT / pdf
    if not pdf.is_file() or pdf.suffix.lower() != ".pdf":
        print(f"[promote] 대상 PDF가 없습니다: {args.pdf}", file=sys.stderr)
        sys.exit(2)
    pdf_rel = pdf.relative_to(REPO_ROOT).as_posix() if pdf.is_relative_to(REPO_ROOT) else str(pdf)
    rid = derive_report_id_any(str(pdf))

    setup_auth(args)
    scratch = Path(tempfile.mkdtemp(prefix="promote_")).as_posix()
    diag = read_last_diag(pdf.name)
    prompt = build_prompt(pdf_rel, rid, scratch, diag)

    before = git_porcelain()
    print(f"[promote] 승격 시작: {pdf.name} (rid={rid}, 모델={args.model})", flush=True)
    print("[promote] 에이전트가 규칙 추가·추출·검증·회귀 대조를 수행합니다 — 수 분 소요", flush=True)

    try:
        verdict, _full = asyncio.run(run_agent(prompt, args))
    except (AuthError, UsageLimitReached) as e:
        print(fatal_msg(e), file=sys.stderr)
        sys.exit(2)
    except asyncio.TimeoutError:
        print(f"[promote] 타임아웃 {args.timeout:.0f}s — 승격 미완, extract.py 상태를 확인하세요"
              f"(백업: {scratch}/extract.py.bak)", file=sys.stderr)
        sys.exit(1)

    after = git_porcelain()
    bad = guard_violations(before, after, rid)

    if verdict == "success" and not bad:
        print(f"[promote] 승격 성공 — src/extract.py에 프로파일 추가, reports/{rid}*.md 생성. "
              "변경분 커밋은 직접 해주세요.", flush=True)
        sys.exit(0)
    if verdict == "success" and bad:
        print("[promote] 경고: 에이전트가 허용 범위 밖 파일을 변경했습니다 — 검토 필요:", flush=True)
        for l in bad:
            print(f"[promote]   {l}", flush=True)
        print(f"[promote] 백업: {scratch}/extract.py.bak", flush=True)
        sys.exit(1)
    if verdict == "rejected":
        print("[promote] 부적합 판정 — 이 문서는 플랫 청킹 수용을 권장합니다(위 보고 참조).", flush=True)
        sys.exit(1)
    print(f"[promote] 승격 실패({verdict or '보고 규약 미준수'}) — 로그를 확인하세요. "
          f"백업: {scratch}/extract.py.bak", flush=True)
    sys.exit(1)


if __name__ == "__main__":
    main()
