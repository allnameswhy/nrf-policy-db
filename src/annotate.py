"""reports/*.md에 절·보고서 요약을 생성해 삽입한다 (파이프라인 2단계).

- 유닛 분할: mdio.split_units — 크기 기반 헤딩 트리 분할 (PROJECT_NOTES §3 단계②)
- 절 요약: 유닛 루트 헤딩 아래 `> **요약:** …` 1~2문장 한 줄
- 보고서 요약: 전 보고서에 `> **보고서 요약:** …` 상시 생성 — 1~2문장 150자 내외
  판별형(카탈로그 라우팅용). 입력 = 유닛 요약 전체 + abstract + frontmatter 키워드
  (§3 단계③). 유닛 요약이 1건이라도 새로 쓰인 파일은 보고서 요약도 재생성(입력 일관성).
- LLM: claude-agent-sdk 경유, Claude Code 구독 로그인 재사용 (API 키 불필요)
- 본문은 LLM에 보내되 돌려받은 요약만 삽입. 쓰기 전마다 왕복 검증
  (요약 블록 제거 시 base와 바이트 일치)으로 원문 무변경을 보장한다.
- 유닛 1건 성공마다 즉시 파일 재작성 → 사용량 한도로 중단돼도 재실행하면 이어서 진행.
- 요약을 1건이라도 새로 쓰는 파일은 verified_annotate 스탬프를 함께 제거한다
  (요약이 바뀌면 자동 미감사 리셋 — 재검사·기록은 verify.py D 품질 판정 소관).
  verified_register·verified_extract는 유지. 단 verified_register가 없는 파일(3단
  도입 전 생성분)은 순서 규칙상 extract도 함께 내린다 — 다음 verify가 풀 검사로 복구.
- 판정 캐시 핸드오프(src/judgecache.py, 2026-09-09): verify가 "현재 요약 텍스트에 대한
  FAIL"로 남긴 유닛(·보고서 요약)은 요약이 없는 것처럼 다시 쓴다(옛 요약은 새 요약이 올
  때까지 파일에 남음 — 재생성 실패 시 파일 불변). 중재가 요약 오류로 본 유닛은 캐시의
  regen_model(Opus)로 재생성하고 소거. 재생성 결과가 이전과 동일하면 streak +1(다음
  verify의 캐시 적중 FAIL 게이트가 중재로 수렴). 잔여 호출 산식 = judgecache.plan.
- 사람 확인(frontmatter `summary_reviewed: hid …`): 요약이 있는 한 다시 쓰지 않는다
  (--force여도). 요약 줄이 지워졌으면 결측으로 다시 만들고 목록에서 뺀다.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import glob as globmod
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import judgecache
import mdio
from claude_agent_sdk import (
    AssistantMessage,
    CLIJSONDecodeError,
    ClaudeAgentOptions,
    ProcessError,
    RateLimitEvent,
    ResultMessage,
    TextBlock,
    query,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

SYSTEM_PROMPT = (
    "너는 한국 정부출연 정책연구보고서의 절 요약자다. 입력된 본문의 내용만으로 "
    "한국어 1~2문장 요약을 작성한다. 규칙: 요약문만 출력한다. 개행 없이 한 줄로 쓴다. "
    "마크다운·불릿·따옴표·머리말을 쓰지 않는다. '이 절은', '본 장에서는' 같은 "
    "자기지시 서두를 피한다. 본문에 없는 정보를 추가하지 않는다."
)


class UsageLimitReached(Exception):
    """구독 사용량 한도 도달 — 전체 실행을 우아하게 중단한다."""


class AuthError(Exception):
    """인증 실패(로그인 만료 등) — 전체 실행을 중단하고 사용자 로그인 필요."""


class RetryableError(Exception):
    """일시 오류(타임아웃·프로세스 오류·형식 불량) — 백오프 후 재시도."""


def fatal_msg(e: Exception) -> str:
    if isinstance(e, AuthError):
        return (
            f"[auth] 인증 실패 — {e}. 터미널에서 claude /login 후 재실행하거나, "
            "claude setup-token으로 장기 토큰(1년)을 발급해 저장소 루트 "
            ".claude_oauth_token 파일에 저장하세요."
        )
    return f"[limit] 사용량 한도 도달 — {e}. 재실행하면 이어서 진행합니다."


# ---- LLM 호출 (verify.py의 D 품질 판정도 이 레이어를 재사용한다) ----

RETRY_DELAYS = [2.0, 8.0]  # RetryableError 백오프


def make_options(model: str, system_prompt: str = SYSTEM_PROMPT) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=system_prompt,
        tools=[],  # 내장 도구 전부 비활성 — 순수 텍스트 생성만
        max_turns=1,
        model=model,
        thinking={"type": "disabled"},  # 짧은 요약·판정에 불필요 — 사용량 절약
        setting_sources=[],  # CLAUDE.md 등 파일시스템 설정 유입 차단
        cwd=str(REPO_ROOT),
    )


def _limit_desc(info) -> str:
    kind = getattr(info, "rate_limit_type", None) or "?"
    resets = getattr(info, "resets_at", None)
    when = (
        datetime.datetime.fromtimestamp(resets).strftime("%m-%d %H:%M")
        if isinstance(resets, (int, float))
        else "?"
    )
    return f"{kind}, 리셋 {when}"


async def call_llm(
    prompt: str, *, model: str, timeout: float, system_prompt: str = SYSTEM_PROMPT
) -> tuple[str, dict]:
    """query() 1회. 반환: (응답 원문, usage 메타). 한도/오류는 예외로 구분."""
    texts: list[str] = []
    result_msg: ResultMessage | None = None

    async def consume() -> None:
        nonlocal result_msg
        async for msg in query(prompt=prompt, options=make_options(model, system_prompt)):
            if isinstance(msg, RateLimitEvent):
                info = msg.rate_limit_info
                if getattr(info, "status", None) == "rejected":
                    raise UsageLimitReached(_limit_desc(info))
            elif isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        texts.append(block.text)
            elif isinstance(msg, ResultMessage):
                result_msg = msg

    stream_error: Exception | None = None
    try:
        await asyncio.wait_for(consume(), timeout)
    except asyncio.TimeoutError as e:
        raise RetryableError(f"타임아웃 {timeout:.0f}s") from e
    except UsageLimitReached:
        raise
    except (ProcessError, CLIJSONDecodeError, Exception) as e:
        # SDK는 CLI의 오류 결과를 일반 Exception으로도 올린다 — result_msg가
        # 더 구체적인 원인을 담고 있으면 아래에서 그걸 우선해 분류한다.
        stream_error = e

    if result_msg is not None and result_msg.is_error:
        detail = str(result_msg.result or result_msg.subtype)
        if result_msg.api_error_status == 429:
            raise UsageLimitReached("api_error_status=429")
        low = detail.lower()
        if "authenticate" in low or "not logged in" in low or "oauth" in low:
            raise AuthError(detail[:200])
        raise RetryableError(f"result error: {detail[:200]}")
    if stream_error is not None:
        raise RetryableError(str(stream_error)[:200]) from stream_error
    text = ""
    if result_msg is not None and isinstance(result_msg.result, str):
        text = result_msg.result
    if not text.strip():
        text = "".join(texts)
    usage = {
        "cost": getattr(result_msg, "total_cost_usd", None) if result_msg else None,
        "usage": getattr(result_msg, "usage", None) if result_msg else None,
    }
    return text, usage


def postprocess(raw: str, max_len: int = 400) -> str:
    """1~2문장 한 줄로 정규화. 형식 복구 불능이면 RetryableError."""
    s = re.sub(r"\s+", " ", raw).strip()
    s = re.sub(r"^(?:[-*•>]\s+)+", "", s)
    s = re.sub(r"^\*\*요약[::]?\*\*\s*", "", s)
    s = re.sub(r"^요약[::]\s*", "", s)
    s = s.strip("\"'“”‘’ ")
    if len(s) > max_len:
        cut = s[:max_len]
        pos = max(cut.rfind("다."), cut.rfind(". "), cut.rfind("함."))
        if pos >= 100:
            s = cut[: pos + 2].rstrip()
        else:
            raise RetryableError(f"요약 {max_len}자 초과({len(s)}자), 문장 경계 없음")
    if not s:
        raise RetryableError("빈 요약")
    return s


def truncate_input(text: str, cap: int) -> tuple[str, bool]:
    """cap 초과 시 head 75% + tail 25% (줄 경계 스냅) + 생략 마커."""
    if len(text) <= cap:
        return text, False
    head_len = int(cap * 0.75)
    head = text[:head_len]
    nl = head.rfind("\n")
    if nl > 0:
        head = head[:nl]
    tail = text[-(cap - head_len) :]
    nl = tail.find("\n")
    if nl >= 0:
        tail = tail[nl + 1 :]
    omitted = len(text) - len(head) - len(tail)
    marker = f"\n\n[... 중략: 전체 {len(text):,}자 중 {omitted:,}자 생략 ...]\n\n"
    return head + marker + tail, True


def build_unit_prompt(fm: mdio.Frontmatter, unit: mdio.Unit, text: str) -> str:
    path = " > ".join(unit.heading_path)
    note = (
        "\n주의: 아래 본문은 이 항목의 도입부(직속 본문)만이며 하위 절 내용은 포함되지 않았다."
        if unit.kind == "intro"
        else ""
    )
    return f"보고서: {fm.title}\n위치: {path}{note}\n\n아래 본문을 1~2문장으로 요약하라.\n\n{text}"


# 보고서 요약 길이: 목표 150자는 프롬프트로, 300자는 postprocess 하드 가드(폭주 방지 —
# 250 실측: 내용 많은 보고서의 252자 개조식 한 문장이 경계 컷 불가로 3회 재시도 실패)
REPORT_SUMMARY_MAX_LEN = 300


def build_report_prompt(fm: mdio.Frontmatter, pairs: list[tuple[str, str]]) -> str:
    listing = "\n".join(f"- {p}: {s}" for p, s in pairs)
    parts = [f"보고서 제목: {fm.title}"]
    if fm.keywords_ko or fm.keywords_en:
        kw = " / ".join(v for v in (fm.keywords_ko, fm.keywords_en) if v)
        parts.append(f"[키워드] {kw}")
    if not fm.abstract_empty:
        parts.append(f"[저자 초록 — 참고 입력]\n{fm.abstract}")
    parts.append(
        "위는 이 보고서의 메타데이터, 아래는 절별 요약 전체다. 이를 근거로 이 보고서를 소개하는 "
        "요약을 1~2문장, 150자 내외, 개행 없이 한 줄로 작성하라(개조식 종결 '~함' 허용). "
        "이 요약만 보고 수백 권 카탈로그에서 이 보고서를 다른 보고서와 구별·발견할 수 있어야 한다. "
        "구체 대상(제도·사업명·기술명·국가 등 고유명사)을 최우선으로 담고, 핵심 결론·제언과 "
        "어떤 수치 데이터가 담겼는지 연상되게 써라. 어느 보고서에나 붙는 범용 문구는 금지한다.\n"
        "나쁜 예(판별력 없음): 본 보고서는 6대 융합신기술과 각 기술별 중장기 로드맵을 제시하였으며, "
        "보다 체계적인 기술 발굴 및 중장기적 투자를 요청하였다.\n"
        "좋은 예: 제론테크, 뇌-신경브릿지, 소프트 로보틱스, 실시간 학습 판단 지능, 첨단 냉각 소재 및 "
        "열제어, IoT 앰비언트 에너지 등 6대 융합신기술을 도출하였고, 이를 바탕으로 상시 발굴-선정-관리 "
        "체계 구축, 도전성 파급효과 중심 평가, 10년 로드맵형 목표 수립, 선제적 투자 체계 확립, 민관 "
        "협의체 상설화 등을 제언함.\n\n[절별 요약 전체]\n" + listing
    )
    return "\n\n".join(parts)


# ---- 파일 처리 ----


@dataclass
class FileEntry:
    file: str
    report_id: str = ""
    status: str = "ok"  # ok | aborted | error
    units_total: int = 0
    units_new: int = 0
    units_skipped_existing: int = 0
    units_failed: int = 0
    units_truncated: int = 0
    report_summary_generated: bool = False
    units_regen: int = 0  # 판정 FAIL 재생성분(units_new에 포함)
    escalated_regen: int = 0  # 그중 승급 모델(regen_model)로 재생성
    reviewed_kept: int = 0  # 사람 확인 요약(요약 있음) — 재생성 제외
    cache_updated: bool = False  # 판정 캐시 되쓰기 발생(호출자가 저장)
    calls: int = 0
    total_cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)


async def call_with_retry(
    prompt: str, args, entry: FileEntry, retries: int = 2, max_len: int = 400,
    model: str | None = None,
) -> str:
    """model = 유닛별 모델(판정 캐시 regen_model 승급) — 기본 args.model."""
    delays = RETRY_DELAYS
    last: RetryableError | None = None
    for attempt in range(retries + 1):
        try:
            raw, usage = await call_llm(prompt, model=model or args.model, timeout=args.timeout)
            entry.calls += 1
            entry.total_cost_usd += usage.get("cost") or 0.0
            return postprocess(raw, max_len)
        except RetryableError as e:
            entry.calls += 1
            last = e
            if attempt < retries:
                await asyncio.sleep(delays[min(attempt, len(delays) - 1)])
    raise last  # type: ignore[misc]


async def process_file(
    path: str, args, sema: asyncio.Semaphore, abort: asyncio.Event, entry: FileEntry,
    bucket: dict | None = None,
) -> None:
    """파일 1개. bucket = 판정 캐시의 이 파일 버킷(없으면 {}) — FAIL 재생성 대상·재생성 모델을
    읽고, 재생성 결과(regen_model 소거·동일 텍스트 streak)를 되쓴다(dirty면 호출자가 저장)."""
    bucket = bucket if bucket is not None else {}
    st = mdio.load_report(path, min_chars=args.min_chars, max_chars=args.max_chars)
    entry.report_id = st.fm.report_id

    # 로드 시 왕복 확인: 기존 요약 배치가 우리 규약과 일치해야 안전하게 재작성 가능
    if not st.roundtrip_ok():
        entry.status = "error"
        entry.errors.append("기존 요약 배치가 규약과 불일치 — 파일을 건드리지 않고 건너뜀")
        return

    base, fm, by_hid, units = st.base, st.fm, st.by_hid, st.units
    reviewed = {h for h in fm.summary_reviewed if h in st.existing}  # 요약 있는 사람 확인만 유효
    stale_reviewed = set(fm.summary_reviewed) - reviewed  # 요약이 지워진 확인 hid — 결측으로 재생성
    if args.force:  # 사람 확인 요약은 --force에도 보존
        existing = {h: t for h, t in st.existing.items() if h in reviewed}
    else:
        existing = st.existing
    regen = judgecache.regen_set(bucket, existing, units, reviewed)
    plan = judgecache.plan(units, existing, regen, reviewed)
    will_write = plan.calls > 0
    entry.reviewed_kept = plan.reviewed_kept

    if will_write and (fm.verified_annotate or stale_reviewed):
        # frontmatter 조정(1회): 요약이 바뀌는 파일은 verified_annotate 리셋(자동 미감사), 요약이
        # 지워진 확인 hid는 목록에서 제거 → 줄 좌표가 바뀌므로 base 기준 파스를 전부 재유도한다.
        if fm.verified_annotate:
            base = mdio.set_verified_stamps(
                base, register=fm.verified_register,
                extract=fm.verified_extract if fm.verified_register else "")
            print(f"[stamp] {fm.report_id}: 요약 갱신 예정 — verified_annotate 리셋", file=sys.stderr)
        if stale_reviewed:
            base = mdio.set_summary_reviewed(base, reviewed)
            print(f"[reviewed] {fm.report_id}: 요약이 지워진 확인 hid 제거 — "
                  f"{' '.join(sorted(stale_reviewed))}", file=sys.stderr)
        fm = mdio.parse_frontmatter(base)
        roots = mdio.parse_heading_tree(base)
        by_hid = {h.hid: h for h in mdio.iter_headings(roots)}
        units = mdio.split_units(roots, base, min_chars=args.min_chars, max_chars=args.max_chars)
        regen = judgecache.regen_set(bucket, existing, units, reviewed)
        plan = judgecache.plan(units, existing, regen, reviewed)
    entry.units_total = len(units)
    todo_ids = set(plan.units_todo)
    todo = [u for u in units if u.hid in todo_ids]
    entry.units_skipped_existing = len(units) - len(todo)

    summaries = dict(existing)  # FAIL 요약은 새 요약이 올 때까지 파일에 남는다(재생성 실패 시 불변)
    write_lock = asyncio.Lock()

    def write_now() -> None:
        candidate = mdio.insert_summaries(base, by_hid, fm, summaries)
        if mdio.strip_summary_blocks(candidate) != base:
            raise RuntimeError("왕복 검증 실패 — 쓰기 중단")
        mdio.write_md_lines(path, candidate)

    def model_for(hid: str) -> str:
        return (bucket.get(hid) or {}).get("regen_model") or args.model

    def note_regen(hid: str, summary: str) -> None:
        """재생성 결과를 캐시에 되쓴다 — 동일 텍스트면 streak +1, 승급 모델을 썼으면 regen_model 소거."""
        ent = bucket.get(hid)
        if not ent:
            return
        if hid in regen:
            entry.units_regen += 1
            entry.cache_updated = True
            if ent.get("summary_sha") == judgecache.summary_sha(summary):
                ent["fail_streak"] = int(ent.get("fail_streak") or 0) + 1
                print(f"[fail-regen] {fm.report_id} {hid}: 재생성 결과가 이전과 동일 — "
                      f"streak {ent['fail_streak']}", file=sys.stderr)
        if ent.get("regen_model"):
            ent.pop("regen_model", None)
            entry.escalated_regen += 1
            entry.cache_updated = True

    def regen_line(hid: str, model: str) -> None:
        if hid in regen:
            streak = (bucket.get(hid) or {}).get("fail_streak", 0)
            where = "보고서 요약" if hid == mdio.REPORT_KEY else hid
            print(f"[fail-regen] {fm.report_id} {where} (streak {streak}, model {model})", file=sys.stderr)

    async def do_unit(u: mdio.Unit) -> None:
        if abort.is_set():
            return
        text, truncated = truncate_input(mdio.unit_text(u, base), args.input_cap)
        if truncated:
            entry.units_truncated += 1
        model = model_for(u.hid)
        regen_line(u.hid, model)
        try:
            summary = await call_with_retry(build_unit_prompt(fm, u, text), args, entry, model=model)
        except (UsageLimitReached, AuthError) as e:
            if not abort.is_set():
                abort.set()
                print(fatal_msg(e), file=sys.stderr)
            return
        except RetryableError as e:
            entry.units_failed += 1
            entry.errors.append(f"{u.hid}: {e}")
            print(f"[fail] {fm.report_id} {u.hid}: {e}", file=sys.stderr)
            return
        async with write_lock:
            summaries[u.hid] = summary
            entry.units_new += 1
            note_regen(u.hid, summary)
            write_now()
        print(
            f"[ok] {fm.report_id} {u.hid} ({u.kind}, {u.char_count:,}자 → {len(summary)}자)"
            + (" [재생성]" if u.hid in regen else ""),
            file=sys.stderr,
        )

    async def with_sema(u: mdio.Unit) -> None:
        async with sema:
            await do_unit(u)

    await asyncio.gather(*(with_sema(u) for u in todo))

    if abort.is_set():
        entry.status = "aborted"
        return

    # 보고서 요약 (§3 단계③): 전 유닛 요약 완료 시 상시 생성. 유닛 요약이 새로 쓰인 파일은
    # 기존 보고서 요약도 낡은 입력 기반이므로 덮어 재생성(입력 일관성). FAIL 재생성 대상이면
    # 유닛이 그대로여도 재생성. 사람 확인 보고서 요약은 유지.
    all_done = all(u.hid in summaries for u in units)
    report_todo = mdio.REPORT_KEY not in reviewed and (
        mdio.REPORT_KEY not in summaries or entry.units_new > 0 or mdio.REPORT_KEY in regen)
    if all_done and units and report_todo:
        pairs = [(" > ".join(u.heading_path), summaries[u.hid]) for u in units]
        model = model_for(mdio.REPORT_KEY)
        regen_line(mdio.REPORT_KEY, model)
        try:
            async with sema:
                summary = await call_with_retry(
                    build_report_prompt(fm, pairs), args, entry,
                    max_len=REPORT_SUMMARY_MAX_LEN, model=model,
                )
            async with write_lock:
                summaries[mdio.REPORT_KEY] = summary
                note_regen(mdio.REPORT_KEY, summary)
                write_now()
            entry.report_summary_generated = True
            print(f"[ok] {fm.report_id} 보고서 요약 삽입", file=sys.stderr)
        except (UsageLimitReached, AuthError) as e:
            abort.set()
            entry.status = "aborted"
            print(fatal_msg(e), file=sys.stderr)
        except RetryableError as e:
            entry.units_failed += 1
            entry.errors.append(f"보고서 요약: {e}")

    if entry.units_failed:
        entry.status = "error"


# ---- 모드별 실행 ----


def run_scan(paths: list[str], args) -> tuple[int, list[dict]]:
    """dry-run: 유닛 목록·예상 호출 수 출력(judgecache.plan — FAIL 재생성·사람 확인 반영). LLM 미호출·무수정."""
    reports = []
    tot_units = tot_calls = tot_over = tot_regen = tot_reviewed = 0
    tot_uncovered = tot_excluded = 0
    cache = judgecache.load(args.judge_cache)
    for p in paths:
        st = mdio.load_report(p, min_chars=args.min_chars, max_chars=args.max_chars)
        fm, units, existing, stats = st.fm, st.units, st.existing, st.stats
        plan = judgecache.plan_for(st, cache.get(Path(p).stem))
        over = sum(1 for u in units if u.char_count > args.input_cap)
        reports.append(
            {
                "report_id": fm.report_id,
                "abstract_empty": fm.abstract_empty,
                "totals": {
                    "units": len(units),
                    "expected_calls": plan.calls,
                    "fail_regen": len(plan.regen),
                    "reviewed": plan.reviewed_kept,
                    "over_input_cap": over,
                    "uncovered_chars": stats["uncovered_chars"],
                    "excluded_chapter_chars": stats["excluded_chapter_chars"],
                },
                "units": [
                    {
                        "hid": u.hid,
                        "kind": u.kind,
                        "depth": u.depth,
                        "chars": u.char_count,
                        "truncated_expected": u.char_count > args.input_cap,
                        "has_existing": u.hid in existing,
                        "fail_regen": u.hid in plan.regen,
                        "reviewed": u.hid in fm.summary_reviewed and u.hid in existing,
                    }
                    for u in units
                ],
            }
        )
        tot_units += len(units)
        tot_calls += plan.calls
        tot_regen += len(plan.regen)
        tot_reviewed += plan.reviewed_kept
        tot_over += over
        tot_uncovered += stats["uncovered_chars"]
        tot_excluded += stats["excluded_chapter_chars"]
    print(json.dumps(reports, ensure_ascii=False, indent=1))
    print(
        f"[scan] 파일 {len(paths)}개: 유닛 {tot_units} / 예상 호출 {tot_calls} / "
        f"FAIL 재생성 {tot_regen} / 사람 확인 {tot_reviewed} / "
        f"입력 상한 초과 {tot_over} / 미커버 {tot_uncovered:,}자 / "
        f"제외 장 {tot_excluded:,}자",
        file=sys.stderr,
    )
    return 0, reports


async def run_smoke(args) -> int:
    """인증·모델·usage 회수 스모크 1회."""
    prompt = (
        "아래 본문을 1~2문장으로 요약하라.\n\n"
        "본 연구는 국가 연구개발 예산의 부처별 배분 구조를 분석하고, "
        "기초연구 투자 비중 확대를 위한 세 가지 정책 대안을 제시하였다. "
        "특히 연구자 주도 자유공모형 사업의 비중을 2030년까지 35%로 확대하는 "
        "로드맵을 제안하였다."
    )
    try:
        raw, usage = await call_llm(prompt, model=args.model, timeout=args.timeout)
        summary = postprocess(raw)
    except (UsageLimitReached, AuthError) as e:
        print(fatal_msg(e), file=sys.stderr)
        return 2
    except RetryableError as e:
        print(f"[fail] 스모크 실패: {e}", file=sys.stderr)
        return 2
    print(f"[ok] model={args.model}", file=sys.stderr)
    print(f"[ok] 요약: {summary}", file=sys.stderr)
    print(f"[ok] usage: {json.dumps(usage, ensure_ascii=False)}", file=sys.stderr)
    return 0


async def run_annotate(paths: list[str], args) -> tuple[int, list[FileEntry]]:
    sema = asyncio.Semaphore(args.concurrency)
    abort = asyncio.Event()
    entries: list[FileEntry] = []
    cache = judgecache.load(args.judge_cache)  # 파생물 — 없으면 재생성 대상 없음(현행 동작)
    for p in paths:
        entry = FileEntry(file=p)
        entries.append(entry)
        if abort.is_set():
            entry.status = "aborted"
            continue
        stem = Path(p).stem
        bucket = cache.get(stem) or {}
        try:
            await process_file(p, args, sema, abort, entry, bucket)
        except Exception as e:  # 파일 단위 오류는 다음 파일 진행
            entry.status = "error"
            entry.errors.append(f"{type(e).__name__}: {e}")
            print(f"[error] {p}: {e}", file=sys.stderr)
        finally:
            if entry.cache_updated and args.judge_cache:  # 파일별 저장 — 한도 중단에도 되쓴 분 보존
                cache[stem] = bucket
                judgecache.save(args.judge_cache, cache)
    ok = all(e.status == "ok" for e in entries)
    new = sum(e.units_new for e in entries)
    regen = sum(e.units_regen for e in entries)
    cost = sum(e.total_cost_usd for e in entries)
    print(
        f"[done] 파일 {len(entries)}개: 신규 요약 {new}건"
        + (f"(FAIL 재생성 {regen}건 포함)" if regen else "")
        + f", 호출 {sum(e.calls for e in entries)}회, "
        f"비용 ${cost:.4f}" + ("" if ok else " — 일부 실패/중단, 재실행하면 이어서 진행"),
        file=sys.stderr,
    )
    return (0 if ok else 2), entries


def write_log(log_path: str, mode: str, entries: list) -> None:
    p = Path(log_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "run_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "mode": mode,
                "files": [e.__dict__ if isinstance(e, FileEntry) else e for e in entries],
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )


def setup_auth(args) -> None:
    """헤드리스 인증 준비 — 값은 절대 로그에 남기지 않는다.

    우선순위: ① 이미 설정된 CLAUDE_CODE_OAUTH_TOKEN 환경변수
             ② 토큰 파일(기본 저장소 루트 .claude_oauth_token, git 제외) —
                `claude setup-token`으로 발급한 1년 유효 장기 토큰
             ③ 없으면 CLI 로그인 세션(~/.claude) 사용 — 만료 시 [auth] 오류로 안내
    """
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return
    p = Path(args.token_file)
    if p.exists():
        token = p.read_text(encoding="utf-8-sig").strip()
        if token:
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token
            print(f"[auth] 장기 토큰 사용: {p.name}", file=sys.stderr)
            if os.environ.get("ANTHROPIC_API_KEY"):
                print(
                    "[warn] ANTHROPIC_API_KEY가 함께 설정돼 있어 토큰보다 우선 적용됩니다(종량제 과금 주의).",
                    file=sys.stderr,
                )


def expand_globs(patterns: list[str]) -> list[str]:
    paths: list[str] = []
    for p in patterns:
        if any(c in p for c in "*?["):
            paths.extend(sorted(globmod.glob(p)))
        else:
            paths.append(p)
    return paths


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="reports/*.md에 절·보고서 요약을 생성해 삽입한다.")
    parser.add_argument("md", nargs="*", help="대상 .md 파일 경로 (reports/*.md)")
    parser.add_argument("--force", action="store_true", help="기존 요약이 있어도 다시 생성")
    parser.add_argument("--scan", action="store_true", help="dry-run: 유닛 분할 진단만 (LLM 미호출·무수정)")
    parser.add_argument("--smoke", action="store_true", help="인증·모델 스모크 테스트 1회")
    parser.add_argument("--model", default="claude-sonnet-5", help="요약 모델 (기본: claude-sonnet-5)")
    parser.add_argument("--min-chars", type=int, default=200, help="유닛 최소 글자 수 (기본: 200)")
    parser.add_argument("--max-chars", type=int, default=4000, help="subtree 유닛 최대 글자 수 (기본: 4000)")
    parser.add_argument("--input-cap", type=int, default=8000, help="LLM 입력 절단 상한 (기본: 8000)")
    parser.add_argument("--concurrency", type=int, default=3, help="동시 LLM 호출 수 (기본: 3)")
    parser.add_argument("--timeout", type=float, default=180, help="호출당 타임아웃 초 (기본: 180)")
    parser.add_argument("--log", default="logs/annotate_log.json", help="로그 경로 (기본: logs/annotate_log.json)")
    parser.add_argument("--judge-cache", default=judgecache.DEFAULT_PATH,
                        help=f"verify 판정 캐시 경로 — FAIL 재생성 대상·모델을 읽는다 (기본: {judgecache.DEFAULT_PATH};"
                             " 빈 문자열이면 재생성 없음)")
    parser.add_argument(
        "--token-file",
        default=str(REPO_ROOT / ".claude_oauth_token"),
        help="claude setup-token 발급 토큰 파일 (기본: 저장소 루트 .claude_oauth_token)",
    )
    args = parser.parse_args()

    os.environ.setdefault("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
    setup_auth(args)

    if args.smoke:
        sys.exit(asyncio.run(run_smoke(args)))

    paths = expand_globs(args.md)
    if not paths:
        print("입력 .md가 없습니다.", file=sys.stderr)
        sys.exit(1)

    if args.scan:
        code, reports = run_scan(paths, args)
        write_log(args.log, "scan", reports)
        sys.exit(code)

    code, entries = asyncio.run(run_annotate(paths, args))
    write_log(args.log, "annotate", entries)
    sys.exit(code)


if __name__ == "__main__":
    main()
