"""검색·답변 흐름 — 질문 1건을 받아 코퍼스에서 근거를 찾아 출처와 함께 답한다 (사용 레이어, 파이프라인 4단계 밖).

PROJECT_NOTES §5 / IMPLEMENTATION_NOTES §7의 6단계를 단일 ClaudeSDKClient 세션으로
구현한다: LLM은 판단(키워드 변형·라우팅·답변)만 하고, RRF 병합·창 절단·SQL은
인프로세스 SDK MCP 툴 4종(bm25_search·get_toc·select_sections·get_body)의
결정론 파이썬이 수행한다.

- 본문 소스: §7 [4]의 ".md에서 로드" 대신 DB body 컬럼 — build_db가 mdio.unit_text로
  .md에서 뽑은 결과와 바이트 동일(요약 블록 제외 완료)하고, 시작 시 files 원장 해시
  하드 게이트가 .md와의 동일성을 보장하므로 사실상 원본 경유 참조다(불일치 시 중단,
  --allow-stale로만 강행). 낡은 색인 + 새 원본의 짝 불일치도 같은 게이트가 차단한다.
- "모른다" 방어 3겹(+평가셋은 후속): BM25 0건 시 확인 불가 유도 단락(사전 결정론) /
  프롬프트 출처 의무 / 답변에 인용된 section_id를 세션이 실제 전달한 본문 집합과
  대조하는 날조 출처 사후 검사(결정론).
- 범위 밖(후속 과제): REPL·관련도 평가셋·LIKE 풀스캔 폴백·kiwipiepy·태그 체계.

사용: python src/search.py "질문" [--trace] [--top-n 8] [--model claude-sonnet-5]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

from annotate import (
    AuthError,
    RetryableError,
    UsageLimitReached,
    _limit_desc,
    fatal_msg,
    setup_auth,
)
from build_db import file_digest, fold_text
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    RateLimitEvent,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)
from status import read_db_ledger

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---- 잠정 상수 — 검색 흐름 실측 후 확정 (IMPLEMENTATION_NOTES §8) ----
RRF_K = 60  # 순위 융합 완충 상수 (관례값, 민감하지 않음)
BODY_THRESHOLD = 8000  # 이하 통째 전달 / 초과 시 창·head 절단 (초과 26유닛 실측)
WINDOW_RADIUS = 1750  # 키워드 중심 창 앞뒤 반경
MAX_WINDOWS = 3  # 병합 후 창 수 상한 — 고빈도 키워드의 사실상 전문화 방지
TOP_N = 8  # select_sections 기본 전달 절 수
EVIDENCE_LIMIT = 20  # bm25_search가 에이전트에 보여줄 증거 행 수
PER_PATH_LIMIT = 30  # 변형×경로별 SQL LIMIT (RRF 병합 깊이)
MAX_TURNS = 30  # 세션 턴 가드 (통상 흐름은 10턴 이내)
DEFAULT_MODEL = "claude-sonnet-5"
# §6 컬럼 가중 계획값 — UNINDEXED 포함 11컬럼 위치 매핑, 평가셋 확보 후 튜닝
BM25_WEIGHTS = (5.0, 2.0, 2.0, 3.0, 1.0, 1.0, 0.0, 0.0, 0.0, 1.0, 1.0)

TOOL_NAMES = ("bm25_search", "get_toc", "select_sections", "get_body")
# 날조 출처 검출용 — section_id 꼴 토큰 (\w는 한글 포함, 슬러그 rid도 커버)
SECTION_ID_RE = re.compile(r"[\w-]+_c\d+(?:s\d+)*")


# ---- DB 헬퍼 (결정론) ----


def open_db(db_path: Path) -> sqlite3.Connection:
    """읽기 전용 연결 — 검색 흐름이 파생물을 오염시킬 경로를 원천 차단."""
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def freshness_gate(db_path: Path, reports_dir: Path, allow_stale: bool) -> None:
    """files 원장 해시 ↔ 현재 .md sha256 하드 게이트.

    DB body 참조가 ".md 경유 참조"로 성립하는 전제 — 불일치면 검색 순위와 인용
    본문이 원본과 어긋나므로 기본 중단한다(--allow-stale로만 경고 강등).
    """
    ledger = read_db_ledger(db_path)
    if ledger is None:
        print(
            f"reports.db의 files 원장을 읽을 수 없습니다({db_path}) — src/build_db.py 실행 필요.",
            file=sys.stderr,
        )
        sys.exit(2)
    digests = {p.as_posix(): file_digest(p) for p in sorted(reports_dir.glob("*.md"))}
    stale = sorted(fp for fp, d in digests.items() if ledger.get(fp) != d)
    orphan = sorted(set(ledger) - set(digests))
    if not stale and not orphan:
        return
    detail = ", ".join(stale + [f"{fp}(원본 없음)" for fp in orphan])
    msg = f"[stale] DB 미반영 {len(stale) + len(orphan)}건: {detail} — src/build_db.py 실행 후 재시도."
    if allow_stale:
        print(f"{msg} (--allow-stale: 낡은 색인으로 계속 — 결과가 원본과 다를 수 있음)", file=sys.stderr)
        return
    print(msg, file=sys.stderr)
    sys.exit(1)


def build_report_map(con: sqlite3.Connection) -> dict[str, str]:
    """{report_id(=파일 스템): filepath} — 합본 파트(2025-17_01)도 스템으로 자연 해소."""
    return {Path(fp).stem: fp for (fp,) in con.execute("SELECT DISTINCT filepath FROM sections")}


def fts_query_phrase(s: str) -> str:
    """MATCH 인젝션·연산자 해석 차단 — 변형 1개 = 이중따옴표 구문 1개."""
    return '"' + s.replace('"', '""') + '"'


SELECT_COLS = (
    "section_id, report_title, chapter, section, summary, year, filepath, "
    "snippet(sections, 4, '[', ']', '…', 20) AS excerpt"  # snippet은 0-기준, body=4
)
_W = ", ".join(str(w) for w in BM25_WEIGHTS)


def run_bm25(con: sqlite3.Connection, match_expr: str, limit: int) -> list[sqlite3.Row]:
    return con.execute(
        f"SELECT {SELECT_COLS} FROM sections WHERE sections MATCH ? "
        f"ORDER BY bm25(sections, {_W}) LIMIT ?",
        (match_expr, limit),
    ).fetchall()


def get_row(con: sqlite3.Connection, section_id: str) -> sqlite3.Row | None:
    return con.execute(
        "SELECT rowid, * FROM sections WHERE section_id = ?", (section_id,)
    ).fetchone()


def get_toc_rows(con: sqlite3.Connection, filepath: str) -> list[sqlite3.Row]:
    """rowid 순 = 문서 순서 (build_db가 heading line 순으로 삽입)."""
    return con.execute(
        "SELECT section_id, chapter, section, summary, length(body) AS n "
        "FROM sections WHERE filepath = ? ORDER BY rowid",
        (filepath,),
    ).fetchall()


def get_adjacent(con: sqlite3.Connection, section_id: str, direction: str) -> sqlite3.Row | None:
    cur = get_row(con, section_id)
    if cur is None:
        return None
    op, order = ("<", "DESC") if direction == "prev" else (">", "ASC")
    return con.execute(
        f"SELECT rowid, * FROM sections WHERE filepath = ? AND rowid {op} ? "
        f"ORDER BY rowid {order} LIMIT 1",
        (cur["filepath"], cur["rowid"]),
    ).fetchone()


# ---- RRF · 창 절단 (결정론) ----


def rrf_merge(rankings: list[list[str]], k: int = RRF_K) -> list[str]:
    """순위표들을 Σ 1/(k+rank)로 융합 — 점수 정규화 없이 이질 경로 병합.

    빈 순위표는 기여 0으로 자연 무시(BM25 0건 시 에이전트 순위 단독이 그대로 나옴).
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for i, sid in enumerate(ranking):
            scores[sid] = scores.get(sid, 0.0) + 1.0 / (k + i + 1)
    return sorted(scores, key=lambda s: -scores[s])


def find_spans(body: str, keyword: str) -> list[tuple[int, int]]:
    """keyword 전 출현 span — 1차 평문, 0건이면 fold 매칭으로 원문 좌표 복원.

    fold 좌표계는 비공백 문자만의 원문 인덱스 배열로 만든다(이어붙이면 정확히
    fold_text(body) — str.split()과 isspace()는 동일 공백 집합). 복원 span은
    원문 연속 구간(중간 공백 포함)이라 인용 규약(원문 그대로)이 유지된다.
    """
    kw = keyword.strip()
    if not kw:
        return []
    spans: list[tuple[int, int]] = []
    i = body.find(kw)
    while i != -1:
        spans.append((i, i + len(kw)))
        i = body.find(kw, i + 1)
    if spans:
        return spans
    fold_to_orig = [idx for idx, ch in enumerate(body) if not ch.isspace()]
    folded = "".join(body[idx] for idx in fold_to_orig)
    fkw = fold_text(kw)
    if not fkw:
        return []
    f = folded.find(fkw)
    while f != -1:
        spans.append((fold_to_orig[f], fold_to_orig[f + len(fkw) - 1] + 1))
        f = folded.find(fkw, f + 1)
    return spans


def cut_windows(body: str, keywords: list[str]) -> tuple[str, str] | None:
    """키워드 중심 대형 창 절단. 전 키워드 미출현이면 None(→ head 절단 폴백)."""
    spans: list[tuple[int, int]] = []
    for kw in keywords:
        spans.extend(find_spans(body, kw))
    if not spans:
        return None
    total = len(spans)
    spans.sort()
    windows: list[list[int]] = []
    for s, e in spans:
        ws, we = max(0, s - WINDOW_RADIUS), min(len(body), e + WINDOW_RADIUS)
        if windows and ws <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], we)
        else:
            windows.append([ws, we])
    dropped = max(0, len(windows) - MAX_WINDOWS)
    kept = windows[:MAX_WINDOWS]
    # 전달량 예산: 병합 창 합계도 임계 이하 — 고빈도 키워드는 인접 창이 이어 붙어
    # 단일 창이 사실상 전문이 되므로(실측: '연구' 105회 → 41K 창 1개) 총량을 자른다
    clipped = False
    budget = BODY_THRESHOLD
    fitted: list[list[int]] = []
    for ws, we in kept:
        if budget <= 0:
            clipped = True
            break
        if we - ws > budget:
            we, clipped = ws + budget, True
        fitted.append([ws, we])
        budget -= we - ws
    kept = fitted
    pieces = []
    if kept[0][0] > 0:
        pieces.append("…(전략)…")
    for j, (ws, we) in enumerate(kept):
        if j > 0:
            pieces.append("…(중략)…")
        pieces.append(body[ws:we])
    if dropped or clipped or kept[-1][1] < len(body):
        pieces.append("…(후략)…")
    sent = sum(we - ws for ws, we in kept)
    notice = (
        f"키워드 창 절단 — 전체 {len(body):,}자 중 {sent:,}자 전달"
        f" (키워드 출현 {total}회, 창 {len(kept)}개"
        + (f", 뒤쪽 창 {dropped}개 생략" if dropped else "")
        + (f", 임계 {BODY_THRESHOLD:,}자 예산 절단" if clipped else "")
        + ")"
    )
    return "\n".join(pieces), notice


def head_cut(body: str) -> tuple[str, str]:
    """head 우선 절단 — 줄 경계 스냅."""
    cut = body[:BODY_THRESHOLD]
    nl = cut.rfind("\n")
    if nl > BODY_THRESHOLD // 2:
        cut = cut[:nl]
    return cut + "\n…(후략)…", f"head 절단 — 전체 {len(body):,}자 중 앞 {len(cut):,}자 전달"


def heading_of(row: sqlite3.Row) -> str:
    return row["chapter"] + (f" > {row['section']}" if row["section"] else "")


def render_unit(row: sqlite3.Row, body_text: str, notice: str) -> str:
    """전달 단위 공통 포맷 — 출처 표기 재료(report_id·section_id·헤딩 경로) 동봉."""
    rid = Path(row["filepath"]).stem
    lines = [
        f"### [{row['section_id']}] {row['report_title']} ({row['year']}) — report_id: {rid}",
        f"위치: {heading_of(row)}",
    ]
    if row["summary"]:
        lines.append(f"요약(라우팅 힌트 — 인용 금지): {row['summary']}")
    if notice:
        lines.append(f"전달 형태: {notice}. 전문·키워드 주변·인접 절은 get_body로 추가 요청 가능.")
    lines += ["본문:", body_text]
    return "\n".join(lines)


# ---- 세션 상태 · 툴 ----

NO_HIT_GUIDE = (
    "후보를 찾지 못했다. 마스터 카탈로그 라우팅(get_toc)으로도 관련 절이 없으면 "
    "추측하지 말고 '보고서 내 확인 불가'로 답하라."
)


@dataclass
class SearchState:
    con: sqlite3.Connection
    report_map: dict[str, str]
    top_n: int
    trace: bool
    bm25_ranking: list[str] = field(default_factory=list)  # ② hold — ④의 첫 순위표
    bm25_hits: dict[str, sqlite3.Row] = field(default_factory=dict)
    delivered: set[str] = field(default_factory=set)  # 본문 전달 집합 — 사후 출처 검사 기준

    def log(self, msg: str) -> None:
        if self.trace:
            print(f"[tool] {msg}", file=sys.stderr)


def text_result(s: str) -> dict:
    return {"content": [{"type": "text", "text": s}]}


def deliver(state: SearchState, row: sqlite3.Row, keywords: list[str]) -> str:
    """⑤ 본문 전달 규칙: 임계 이하 통째 / 초과+키워드 창 / 미출현·키워드 없음 head."""
    body = row["body"]
    if len(body) <= BODY_THRESHOLD:
        text, notice = body, ""
    else:
        cw = cut_windows(body, keywords) if keywords else None
        if cw is not None:
            text, notice = cw
        else:
            text, notice = head_cut(body)
            if keywords:
                notice += " (지정 키워드가 본문에 미출현 — 평문·fold 모두)"
            else:
                notice += " (focus_keywords를 지정하면 키워드 중심 창으로 재전달)"
    state.delivered.add(row["section_id"])
    state.log(f"deliver {row['section_id']}: {notice or f'통째 {len(body):,}자'}")
    return render_unit(row, text, notice)


def build_tools(state: SearchState) -> list:
    """state를 포획한 클로저 툴 4종 — 모듈 전역 상태 배제."""

    @tool(
        "bm25_search",
        "키워드 변형들로 전 보고서 BM25 전문검색. 각 변형은 3글자 이상(2글자 단독 금지 — "
        "조사·복합어로 확장). 결과 순위표는 select_sections에서 자동 병합된다.",
        {
            "type": "object",
            "properties": {
                "variants": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "키워드 변형 3~5개, 각 3글자 이상",
                }
            },
            "required": ["variants"],
        },
    )
    async def bm25_search(args: dict) -> dict:
        raw = [str(v) for v in args.get("variants", [])]
        variants = [v for v in raw if v.strip()]
        ok = [v for v in variants if len(v) >= 3 or len(fold_text(v)) >= 3]
        rejected = [v for v in variants if v not in ok]
        state.log(f"bm25_search variants={ok!r} 거부={rejected!r}")
        notes = []
        if rejected:
            notes.append(
                "거부(3글자 미만 — trigram 색인은 구조적으로 0건): "
                + ", ".join(repr(v) for v in rejected)
                + " → 조사·복합어 변형으로 재작성하라(예: '예산' → '예산이', 'R&D 예산')."
            )
        if not ok:
            return text_result("\n".join(notes) or "유효한 변형이 없다.")

        rankings: list[list[str]] = []
        matched_by: dict[str, list[str]] = {}
        for v in ok:
            exprs = [(fts_query_phrase(v), v)]
            fv = fold_text(v)
            if len(fv) >= 3:
                exprs.append(("body_fold : " + fts_query_phrase(fv), f"{v}(fold)"))
            for expr, label in exprs:
                rows = run_bm25(state.con, expr, PER_PATH_LIMIT)
                ranking = []
                for row in rows:
                    sid = row["section_id"]
                    ranking.append(sid)
                    state.bm25_hits.setdefault(sid, row)
                    tags = matched_by.setdefault(sid, [])
                    if label not in tags:
                        tags.append(label)
                rankings.append(ranking)
        merged = rrf_merge(rankings)
        state.bm25_ranking = merged  # 재호출 시 마지막 검색이 유효 순위표
        state.log(f"→ 병합 {len(merged)}건, top: {merged[:3]}")

        if not merged:
            return text_result("\n".join(notes + [f"BM25 결과 0건. {NO_HIT_GUIDE}"]))
        lines = notes + [
            f"BM25 증거 상위 {min(EVIDENCE_LIMIT, len(merged))}건 (병합 순위표 전체 {len(merged)}건은 절 선정 시 자동 반영, 재검색 시 대체):"
        ]
        for i, sid in enumerate(merged[:EVIDENCE_LIMIT], 1):
            row = state.bm25_hits[sid]
            excerpt = " ".join(str(row["excerpt"]).split())
            lines.append(
                f"{i}. {sid} | {Path(row['filepath']).stem} | {heading_of(row)} | "
                f"{row['year']} | 발췌: {excerpt} | 적중: {', '.join(matched_by[sid])}"
            )
        return text_result("\n".join(lines))

    @tool(
        "get_toc",
        "보고서 1권의 목차(문서 순서: section_id·헤딩 경로·본문 글자 수·요약)를 조회한다. "
        "라우팅으로 고른 후보 보고서의 절을 검토할 때 사용.",
        {
            "type": "object",
            "properties": {
                "report_id": {"type": "string", "description": "예: 2025-02, 2025-17_01"}
            },
            "required": ["report_id"],
        },
    )
    async def get_toc(args: dict) -> dict:
        rid = str(args.get("report_id", "")).strip()
        state.log(f"get_toc {rid}")
        fp = state.report_map.get(rid)
        if fp is None:
            return text_result(
                f"report_id '{rid}' 없음. 유효 목록: " + ", ".join(sorted(state.report_map))
            )
        lines = [
            f"{rid} 목차 (본문 {BODY_THRESHOLD:,}자 초과 절은 select_sections에서 focus_keywords를 함께 지정하라):"
        ]
        for r in get_toc_rows(state.con, fp):
            summary = r["summary"] or "(요약 없음 — 참고문헌·부록류)"
            lines.append(f"{r['section_id']} | {heading_of(r)} | {r['n']:,}자 | {summary}")
        return text_result("\n".join(lines))

    @tool(
        "select_sections",
        "고른 절들을 관련성 높은 순으로 넘기면 BM25 순위표와 RRF 병합해 상위 절의 본문을 "
        "전달한다. 큰 절(8,000자 초과)이 있으면 focus_keywords로 절단 중심어를 지정하라.",
        {
            "type": "object",
            "properties": {
                "section_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "관련성 높은 순 — 이 순서가 두 번째 순위표가 된다",
                },
                "focus_keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "선택 — 대형 유닛 창 절단 중심 키워드",
                },
                "top_n": {"type": "integer", "description": "선택 — 전달 절 수 상한"},
            },
            "required": ["section_ids"],
        },
    )
    async def select_sections(args: dict) -> dict:
        ids = [str(s).strip() for s in args.get("section_ids", []) if str(s).strip()]
        keywords = [str(k).strip() for k in (args.get("focus_keywords") or []) if str(k).strip()]
        top_n = int(args.get("top_n") or state.top_n)
        state.log(f"select_sections ids={ids!r} keywords={keywords!r} top_n={top_n}")

        valid, invalid = [], []
        for sid in ids:
            (valid if get_row(state.con, sid) is not None else invalid).append(sid)
        notes = []
        if invalid:
            notes.append("무효 section_id(제외): " + ", ".join(invalid))
        merged = rrf_merge([state.bm25_ranking, valid])
        chosen = merged[:top_n]
        if not chosen:
            return text_result("\n".join(notes + [f"전달 가능한 본문 없음. {NO_HIT_GUIDE}"]))

        def rank_in(lst: list[str], sid: str) -> str:
            return str(lst.index(sid) + 1) if sid in lst else "-"

        overview = [
            f"RRF 병합 상위 {len(chosen)}건 (BM25 순위 / 라우팅 순위):"
        ] + [
            f"{i}. {sid} (BM25 {rank_in(state.bm25_ranking, sid)}, 라우팅 {rank_in(valid, sid)})"
            for i, sid in enumerate(chosen, 1)
        ]
        parts = []
        for sid in chosen:
            row = get_row(state.con, sid)
            parts.append(deliver(state, row, keywords))
        state.log(f"→ 전달 {len(chosen)}건: {chosen}")
        return text_result("\n\n".join(notes + ["\n".join(overview)] + parts))

    @tool(
        "get_body",
        "본문 추가 확보(2단 로드): mode=full(절 전문), around(keyword 중심 창), "
        "adjacent(direction의 인접 절). 전달받은 본문이 부족할 때 사용.",
        {
            "type": "object",
            "properties": {
                "section_id": {"type": "string"},
                "mode": {"type": "string", "enum": ["full", "around", "adjacent"]},
                "keyword": {"type": "string", "description": "mode=around 필수"},
                "direction": {
                    "type": "string",
                    "enum": ["prev", "next"],
                    "description": "mode=adjacent 필수",
                },
            },
            "required": ["section_id", "mode"],
        },
    )
    async def get_body(args: dict) -> dict:
        sid = str(args.get("section_id", "")).strip()
        mode = str(args.get("mode", "")).strip()
        state.log(f"get_body {sid} mode={mode} kw={args.get('keyword')!r} dir={args.get('direction')!r}")
        row = get_row(state.con, sid)
        if row is None:
            return text_result(f"section_id '{sid}' 없음.")
        if mode == "full":
            state.delivered.add(sid)
            return text_result(render_unit(row, row["body"], f"전문(명시 요청, {len(row['body']):,}자)"))
        if mode == "around":
            kw = str(args.get("keyword", "")).strip()
            if not kw:
                return text_result("mode=around에는 keyword가 필요하다.")
            cw = cut_windows(row["body"], [kw])
            if cw is None:
                return text_result(f"'{kw}' 미출현(평문·fold 모두) — 다른 키워드 또는 mode=full을 시도하라.")
            state.delivered.add(sid)
            return text_result(render_unit(row, cw[0], cw[1]))
        if mode == "adjacent":
            direction = str(args.get("direction", "")).strip()
            if direction not in ("prev", "next"):
                return text_result("mode=adjacent에는 direction(prev|next)이 필요하다.")
            adj = get_adjacent(state.con, sid, direction)
            if adj is None:
                return text_result(f"{sid}의 {direction} 인접 절 없음(보고서 경계).")
            return text_result(deliver(state, adj, []))
        return text_result("mode는 full|around|adjacent 중 하나여야 한다.")

    return [bm25_search, get_toc, select_sections, get_body]


# ---- 프롬프트 · 세션 드라이버 ----

SYSTEM_PROMPT = f"""너는 기관 정책연구보고서 코퍼스 전용 검색·답변 에이전트다.

절차:
1. 질의에서 검색 키워드 변형 3~5개를 추출한다. 각 변형은 3글자 이상만(색인이 trigram이라 2글자 이하는 구조적으로 0건). 2글자 개념은 단독 금지 — 조사·복합어로 3글자화한다(예: '예산' → '예산이', 'R&D 예산'). 어휘 불일치에 대비해 동의어·유관어 변형을 섞는다.
2. bm25_search로 전 보고서를 검색한다.
3. 첫 메시지의 마스터 카탈로그와 검색 증거를 함께 보고 후보 보고서를 고른 뒤, get_toc로 목차·요약을 확인해 관련 절을 고른다. 검색 증거가 빈약해도 카탈로그 라우팅으로 후보를 찾을 수 있다.
4. select_sections에 관련성 높은 순으로 section_id를 넘긴다(이 순서가 검색 순위표와 RRF 병합된다). 본문 {BODY_THRESHOLD:,}자 초과 절이 있으면 focus_keywords로 절단 중심어를 지정한다.
5. 전달 본문이 부족하면 get_body(full/around/adjacent)로 추가 확보한다.
6. 답변을 작성한다.

답변 규칙:
- 근거는 툴이 전달한 '본문' 텍스트뿐이다. 목차·전달 단위의 '요약'은 라우팅 힌트일 뿐 인용·근거 사용 금지.
- 모든 주장 뒤에 출처를 단다: (report_id, section_id, 장>절 위치). section_id는 전달 단위 머리의 전체 식별자(예: 2025-13_c2s2s1s3)를 축약 없이 그대로 쓴다.
- 전달 본문에 없는 내용은 쓰지 않는다. 질의 전부 또는 일부에 답할 근거가 없으면 그 부분을 '보고서 내 확인 불가'로 명시한다. 추측·일반 상식 보충 금지.
- 한국어로, 질문에 직접 답하는 문체로 쓴다."""


def build_initial_prompt(question: str, index_path: Path) -> str:
    catalog = index_path.read_text(encoding="utf-8")
    return (
        "=== 마스터 카탈로그 (보고서 선정 참고 자료 — 인용 금지) ===\n"
        f"{catalog}\n"
        "=== 카탈로그 끝 ===\n\n"
        f"질문: {question}"
    )


def make_search_options(server, args) -> ClaudeAgentOptions:
    """annotate 관례(setting_sources=[]·cwd·thinking 비활성) 승계 + 툴 세션용 변경."""
    kwargs = dict(
        system_prompt=SYSTEM_PROMPT,
        tools=[],  # 내장 도구 전면 차단 — DB 툴이 유일 창구
        mcp_servers={"searchdb": server},
        allowed_tools=[f"mcp__searchdb__{n}" for n in TOOL_NAMES],
        max_turns=args.max_turns,
        model=args.model,
        setting_sources=[],  # CLAUDE.md 등 파일시스템 설정 유입 차단
        cwd=str(REPO_ROOT),
    )
    if not args.thinking:
        kwargs["thinking"] = {"type": "disabled"}  # 기본 비활성 — 사용량 절약(--thinking 해제)
    return ClaudeAgentOptions(**kwargs)


def audit_citations(answer: str, delivered: set[str]) -> list[str]:
    """답변 속 section_id 꼴 토큰 중 세션이 전달한 적 없는 것 = 날조 출처 후보."""
    return sorted(set(SECTION_ID_RE.findall(answer)) - delivered)


async def run_search(question: str, args) -> int:
    con = open_db(Path(args.db))
    state = SearchState(
        con=con,
        report_map=build_report_map(con),
        top_n=args.top_n,
        trace=args.trace,
    )
    server = create_sdk_mcp_server("searchdb", tools=build_tools(state))
    options = make_search_options(server, args)

    answer_parts: list[str] = []
    result_msg: ResultMessage | None = None
    turn = 0

    async def consume(client: ClaudeSDKClient) -> None:
        nonlocal result_msg, turn
        async for msg in client.receive_response():
            if isinstance(msg, RateLimitEvent):
                info = msg.rate_limit_info
                if getattr(info, "status", None) == "rejected":
                    raise UsageLimitReached(_limit_desc(info))
            elif isinstance(msg, AssistantMessage):
                turn += 1
                tool_uses = [b for b in msg.content if isinstance(b, ToolUseBlock)]
                if tool_uses:
                    # 툴 호출 전 텍스트는 중간 코멘트 — 최종 답변은 마지막 툴 이후 텍스트만
                    answer_parts.clear()
                    if state.trace:
                        for b in tool_uses:
                            arg_s = json.dumps(b.input, ensure_ascii=False)[:200]
                            print(f"[turn {turn}] {b.name} {arg_s}", file=sys.stderr)
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        answer_parts.append(block.text)
            elif isinstance(msg, ResultMessage):
                result_msg = msg

    stream_error: Exception | None = None
    client = ClaudeSDKClient(options=options)
    await client.connect()
    try:
        await client.query(build_initial_prompt(question, args.index_path))
        try:
            await asyncio.wait_for(consume(client), args.timeout)
        except asyncio.TimeoutError as e:
            raise RetryableError(f"타임아웃 {args.timeout:.0f}s") from e
        except (UsageLimitReached, AuthError):
            raise
        except Exception as e:  # SDK는 CLI 오류를 일반 Exception으로도 올린다
            stream_error = e
    finally:
        await client.disconnect()

    # annotate.call_llm과 동일한 오류 분류 — result_msg가 더 구체적이면 우선
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

    answer = ""
    if result_msg is not None and isinstance(result_msg.result, str):
        answer = result_msg.result
    if not answer.strip():
        answer = "".join(answer_parts)
    if not answer.strip():
        subtype = getattr(result_msg, "subtype", None) if result_msg else None
        print(
            f"[error] 답변이 생성되지 않았습니다(subtype={subtype}, {turn}턴) — "
            "--max-turns 상향 또는 재실행을 검토하세요.",
            file=sys.stderr,
        )
        return 1

    print(answer)

    fabricated = audit_citations(answer, state.delivered)
    if fabricated:
        print(
            "[검증] 전달되지 않은 출처 인용(날조 의심): " + ", ".join(fabricated),
            file=sys.stderr,
        )
    if args.trace and result_msg is not None:
        cost = getattr(result_msg, "total_cost_usd", None)
        print(f"[usage] {turn}턴, cost={cost}", file=sys.stderr)
    return 0


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(
        description="검색·답변 흐름 — 질문 1건을 받아 코퍼스에서 근거를 찾아 출처와 함께 답한다."
    )
    parser.add_argument("question", nargs="+", help="질문 (여러 단어면 따옴표 없이도 됨)")
    parser.add_argument("--db", default="reports.db", help="FTS5 DB 경로 (기본: reports.db)")
    parser.add_argument("--reports-dir", default="reports", help="원본 .md 디렉터리 — build_db 실행 시와 동일해야 함 (기본: reports)")
    parser.add_argument("--index", default="master_index.md", help="마스터 카탈로그 경로 (기본: master_index.md)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"에이전트 모델 (기본: {DEFAULT_MODEL})")
    parser.add_argument("--top-n", type=int, default=TOP_N, help=f"본문 전달 절 수 (기본: {TOP_N})")
    parser.add_argument("--max-turns", type=int, default=MAX_TURNS, help=f"세션 턴 상한 (기본: {MAX_TURNS})")
    parser.add_argument("--timeout", type=float, default=600, help="세션 전체 타임아웃 초 (기본: 600)")
    parser.add_argument("--trace", action="store_true", help="툴 호출·전달 요약을 stderr로 출력")
    parser.add_argument("--thinking", action="store_true", help="모델 thinking 활성화 (기본: 비활성)")
    parser.add_argument("--allow-stale", action="store_true", help="DB가 .md보다 낡아도 강행 (기본: 중단)")
    parser.add_argument(
        "--token-file",
        default=str(REPO_ROOT / ".claude_oauth_token"),
        help="claude setup-token 발급 토큰 파일 (기본: 저장소 루트 .claude_oauth_token)",
    )
    args = parser.parse_args()
    question = " ".join(args.question).strip()
    if not question:
        print("질문이 비어 있습니다.", file=sys.stderr)
        sys.exit(2)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"reports.db 없음({db_path}) — src/build_db.py 실행 필요.", file=sys.stderr)
        sys.exit(2)
    freshness_gate(db_path, Path(args.reports_dir), args.allow_stale)
    args.index_path = Path(args.index)
    if not args.index_path.exists():
        print(f"마스터 카탈로그 없음({args.index}) — src/build_index.py 실행 필요.", file=sys.stderr)
        sys.exit(2)

    os.environ.setdefault("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
    setup_auth(args)

    try:
        sys.exit(asyncio.run(run_search(question, args)))
    except (UsageLimitReached, AuthError) as e:
        print(fatal_msg(e), file=sys.stderr)
        sys.exit(1)
    except RetryableError as e:
        print(f"[error] 세션 오류: {e} — 재실행하면 새 세션으로 다시 시도합니다.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
