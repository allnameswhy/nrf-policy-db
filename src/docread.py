"""문서 판독 — PDF당 LLM 1회 (R12 Phase 2, 2026-09-30).

PDF 한 개를 모델이 한 번 읽어 ① 합본 분할(문서별 쪽 범위) ② 앞부속/본문/뒷부속 경계 ③ 목차 항목 목록을
정하고 `toc/{PDF stem}.json`에 저장한다. 그 뒤 단계는 결정론이다 — register가 문서 범위로 등록부 파트 행을
만들고(판독 파일이 없는 PDF는 register가 이 모듈을 불러 채운다), extract(src/toclane.py)가 목차 항목을 본문 줄에
대응시켜 헤딩으로 삼는다.

모델이 받는 것: 쪽 프로필 표(쪽마다 꼬리말 번호·글자 수·표식·첫 두 줄 — 경량 스캔 산출) + 목차 쪽·표지·
경계 후보 쪽의 이미지(PyMuPDF 렌더). 모델이 내는 것: 구조 포인터뿐(제목·깊이·인쇄 쪽·쪽 경계) —
본문 텍스트는 어디에도 쓰이지 않는다(헤딩 텍스트도 extract가 본문 줄 원문으로 쓴다).

재현성: 판독 파일은 한 번 저장되면 재실행이 덮어쓰지 않는다(`--force`로만 재판독). 헤딩 ID는 이 파일에서
결정론으로 나오므로 파일을 git에 둔다(report_ids.tsv와 같은 성격).

사용: python src/docread.py "pdfs/<파일>.pdf" ... [--force] [--model M]   # 보통은 register가 부른다 — 직접 실행은 재판독용
종료 코드: 0 = 전부 판독(또는 기존 파일), 1 = 판독 실패 있음, 2 = 인증·한도 중단.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import datetime
import glob as globmod
import json
import os
import sys
import time
import unicodedata
from pathlib import Path

import pymupdf

import extract
import registry
import tocparse

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "claude-sonnet-5-5"
RENDER_ZOOM = 1.6            # ≈950×1350px — 모델 쪽 축소 임계(긴 변 1,568px) 아래
MAX_IMAGES = 20              # 호출당 이미지 상한(목차 쪽 우선, 남는 예산을 표지·경계 후보 쪽에)
MAX_IMAGE_BYTES = 20 * 1024 * 1024
TOC_WINDOW = 40              # 구간 시작부터 목차를 찾는 쪽 범위
NO_TOC_FRONT_PAGES = 8       # 목차 쪽을 못 찾은 구간은 앞 몇 쪽을 그대로 보여준다
LINE_CLIP = 40

SYSTEM_PROMPT = (
    "너는 한국 정책연구보고서 PDF의 구조를 판독한다. 입력은 (1) 쪽 프로필 표 — PDF 쪽마다 꼬리말에 인쇄된 "
    "쪽 번호, 글자 수, 표식, 맨 위 두 줄 — 와 (2) 목차 쪽·표지·경계 후보 쪽의 이미지다. 다음을 정해 JSON으로만 답한다.\n"
    "\n"
    "1. documents — 이 PDF에 들어 있는 문서들. 보통 하나다. 본편 뒤에 별권·부록 책자·요약본처럼 자기 표지와 "
    "쪽 번호(1부터 다시)를 가진 독립 문서가 이어 붙은 합본이면 여러 개다. 프로필 표의 '재시작' 표식은 꼬리말 번호가 "
    "1로 되돌아간 쪽 — 경계 후보일 뿐이다. 앞부속(표지·제출문·요약문·목차)이 따로 번호를 매기고 본문이 1쪽부터 "
    "다시 시작하는 것은 같은 문서다(나누지 않는다). 참고문헌은 언제나 본편에 붙인다. 부록·붙임·별첨은 쪽 번호가 본편에서 "
    "이어지면 같은 문서지만, 꼬리말 번호가 1로 다시 시작하면 본편 목차에 실려 있더라도 별도 문서(kind=appendix)로 나눈다 — "
    "그 문서의 시작 쪽은 부록의 표지·간지(재시작 쪽 앞의 번호 없는 쪽 포함)이고, 이렇게 나눈 부록의 항목은 본편 entries에 "
    "넣지 않는다(부록 문서가 자기 목차를 가지면 그 문서의 entries로). "
    "새 문서의 시작 쪽은 그 문서의 표지(재시작 쪽보다 앞일 수 있다). pages는 [첫 쪽, 끝 쪽] PDF 쪽 번호이고 "
    "문서들이 1쪽부터 마지막 쪽까지 빈틈·겹침 없이 이어져야 한다.\n"
    "2. 문서마다 body_start — 본문이 시작하는 PDF 쪽(앞부속이 끝난 다음, 첫 장의 첫 쪽. 장 표제만 있는 구분 쪽이 "
    "있으면 그 쪽). back_start — 참고문헌·부록 등 뒷부속이 시작하는 PDF 쪽(없으면 null). toc_pages — 본문 목차가 "
    "실린 PDF 쪽들(표·그림 목차 쪽 제외).\n"
    "3. 문서마다 entries — 그 문서의 본문 목차 항목을 목차에 실린 순서대로 전부. 목차 이미지에 인쇄된 글자를 그대로 "
    "옮긴다(고쳐 쓰거나 요약하지 않는다, 목차에 없는 항목을 만들지 않는다). label = 번호 표기(`제1장`, `Ⅰ.`, `1.`, "
    "`1.1`, `가.`, `(1)` 등 인쇄된 그대로, 없으면 빈 문자열), title = 번호를 뺀 제목(여러 줄로 줄바꿈된 제목은 한 줄로 "
    "이어 붙인다), page = 항목 옆에 인쇄된 쪽 번호(아라비아 숫자, 없거나 로마 숫자면 null), depth = 위계(가장 위 "
    "단위 = 1, 그 아래 = 2 … 최대 4; 번호 체계와 들여쓰기로 판단하고 단계를 건너뛰지 않는다), kind = front(요약문·"
    "제출문처럼 본문 앞의 항목) / body(본문) / back(참고문헌·부록 및 그 하위 항목). 참고문헌·부록은 depth 1이다. "
    "표 목차·그림 목차의 항목과 목차 표제(`목차`, `CONTENTS`)는 넣지 않는다. 국문 목차와 영문 목차가 둘 다 있으면 "
    "국문만. 요약 목차와 세부 목차가 둘 다 있으면 세부 목차만. 목차가 없는 문서는 entries를 빈 배열로.\n"
    "4. kind(문서) = main(본편) / volume(별권·분권) / appendix(부록 책자) / other. title = 표지 제목. "
    "boundary_reason = 이 문서의 시작 경계를 그렇게 정한 근거 한 문장(첫 문서는 빈 문자열 가능).\n"
    "\n"
    "쪽 번호는 두 가지를 구분한다: pages·body_start·back_start·toc_pages는 PDF 쪽(프로필 표의 첫 열, 이미지 라벨), "
    "entries의 page는 목차에 인쇄된 번호다."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "documents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pages": {"type": "array", "items": {"type": "integer"}},
                    "kind": {"type": "string", "enum": ["main", "volume", "appendix", "other"]},
                    "title": {"type": "string"},
                    "body_start": {"type": "integer"},
                    "back_start": {"type": ["integer", "null"]},
                    "toc_pages": {"type": "array", "items": {"type": "integer"}},
                    "boundary_reason": {"type": "string"},
                    "entries": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "depth": {"type": "integer"},
                                "label": {"type": "string"},
                                "title": {"type": "string"},
                                "page": {"type": ["integer", "null"]},
                                "kind": {"type": "string", "enum": ["front", "body", "back"]},
                            },
                            "required": ["depth", "label", "title", "page", "kind"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["pages", "kind", "title", "body_start", "back_start", "toc_pages",
                             "boundary_reason", "entries"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["documents"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# 입력 조립 (결정론)
# ---------------------------------------------------------------------------

def restart_pages(scans) -> list[int]:
    """꼬리말 번호가 1보다 큰 값까지 간 뒤 1로 되돌아간 쪽(0-based) — 합본 경계 후보
    (장 문법 게이트 없이)."""
    out, run_max = [], 0
    for i, s in enumerate(scans):
        if s.footer_arabic is None:
            continue
        if s.footer_arabic == 1 and run_max > 1:
            out.append(i)
            run_max = 1
        else:
            run_max = max(run_max, s.footer_arabic)
    return out


def _clip(text: str) -> str:
    t = " ".join(text.split())
    return t if len(t) <= LINE_CLIP else t[:LINE_CLIP] + "…"


def profile_table(scans, restarts: set[int]) -> str:
    rows = ["쪽|꼬리말|글자수|표식|맨 위 두 줄"]
    for i, s in enumerate(scans):
        flags = []
        if s.blank:
            flags.append("빈쪽")
        elif s.image_only:
            flags.append("이미지")
        kind = extract.page_toc_kind(s)
        if kind in ("ko", "en"):
            flags.append("목차표제")
        if extract.page_toc_like(s):
            flags.append("목차형")
        if extract.page_has_front_title(s, True):
            flags.append("앞부속표제")
        if i in restarts:
            flags.append("재시작")
        heads = [_clip(ln.text) for ln in s.lines if ln.text.strip()][:2]
        foot = s.footer if s.footer else ("" if s.footer_arabic is None else str(s.footer_arabic))
        rows.append(f"{i + 1}|{foot}|{s.text_chars}|{','.join(flags)}|{' ‖ '.join(heads)}")
    return "\n".join(rows)


def image_pages(scans, restarts: list[int]) -> tuple[list[int], list[str]]:
    """이미지로 보낼 쪽(0-based, 오름차순)과 경고. 목차 쪽이 우선, 남는 예산을 표지·경계 후보 쪽에."""
    n = len(scans)
    warns: list[str] = []
    starts = [0] + restarts
    toc: list[int] = []
    for bi, st in enumerate(starts):
        # 새 문서의 표지·목차는 재시작 쪽보다 앞에 있을 수 있다 — 구간을 앞으로 넉넉히 잡는다
        lo = max(0, st - TOC_WINDOW) if bi else 0
        hi = n - 1
        pages, _info = tocparse.toc_page_set(scans, (lo if bi == 0 else st, hi))
        if bi and not pages:
            prev_end = starts[bi - 1]
            back = [p for p in range(max(prev_end + 1, st - 12), st)
                    if extract.page_toc_kind(scans[p]) in ("ko", "en") or extract.page_toc_like(scans[p])]
            pages = back
        if not pages and bi == 0:
            pages = [p for p in range(0, min(NO_TOC_FRONT_PAGES, n)) if not scans[p].blank]
            warns.append("목차 쪽 미검출 — 앞쪽을 그대로 전달")
        toc.extend(p for p in pages if p not in toc)
    toc.sort()
    if len(toc) > MAX_IMAGES - 1:
        warns.append(f"목차 쪽 {len(toc)}장 — 상한 초과, 앞 {MAX_IMAGES - 1}장만 전달")
        toc = toc[:MAX_IMAGES - 1]
    chosen = set(toc) | {0}
    extra = []
    for r in restarts:
        # 재시작 쪽과 그 앞의 번호 없는 쪽(새 문서의 표지 후보)
        k = r - 1
        while k > 0 and scans[k].footer_arabic is None and r - k <= 6:
            k -= 1
        extra.extend([k + 1, r])
    for p in extra:
        if len(chosen) >= MAX_IMAGES:
            warns.append("경계 후보 쪽 이미지 일부 생략(상한) — 프로필 표로만 전달")
            break
        chosen.add(p)
    return sorted(chosen), warns


def render_png(doc, pno: int, save_dir: Path | None) -> bytes:
    pix = doc[pno].get_pixmap(matrix=pymupdf.Matrix(RENDER_ZOOM, RENDER_ZOOM), alpha=False)
    data = pix.tobytes("png")
    if save_dir is not None:
        save_dir.mkdir(parents=True, exist_ok=True)
        (save_dir / f"p{pno + 1:03d}.png").write_bytes(data)
    return data


def build_content(pdf_path: Path, png_dir: Path | None) -> tuple[list[dict], dict]:
    doc = pymupdf.open(str(pdf_path))
    try:
        scans = extract.scan_document(doc, tables=False)
        restarts = restart_pages(scans)
        pages, warns = image_pages(scans, restarts)
        table = profile_table(scans, set(restarts))
        content: list[dict] = [{
            "type": "text",
            "text": (f"PDF 파일: {pdf_path.name}\n전체 {len(scans)}쪽.\n"
                     f"꼬리말 번호가 1로 되돌아간 쪽(경계 후보): "
                     f"{', '.join(str(r + 1) for r in restarts) if restarts else '없음'}\n\n"
                     f"[쪽 프로필 표]\n{table}\n\n[쪽 이미지 {len(pages)}장 — 각 이미지 앞에 PDF 쪽 번호]"),
        }]
        total = 0
        sent = []
        for p in pages:
            png = render_png(doc, p, png_dir)
            total += len(png) * 4 // 3
            if total > MAX_IMAGE_BYTES:
                warns.append(f"이미지 용량 상한 — p.{p + 1}부터 생략")
                break
            content.append({"type": "text", "text": f"PDF p.{p + 1}"})
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode("ascii")}})
            sent.append(p + 1)
        content.append({"type": "text", "text": "위 자료로 이 PDF의 documents를 판독해 JSON으로 답하라."})
        meta = {"n_pages": len(scans), "restarts": [r + 1 for r in restarts], "images": sent, "warnings": warns,
                "toc_flag_pages": [i + 1 for i, s in enumerate(scans)
                                   if extract.page_toc_kind(s) in ("ko", "en") or s.leader_lines >= 3]}
        return content, meta
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 결정론 게이트
# ---------------------------------------------------------------------------

def validate(data, meta: dict) -> list[str]:
    """모델 출력의 형식·정합 위반 목록(빈 목록 = 통과)."""
    errs: list[str] = []
    n = meta["n_pages"]
    docs = data.get("documents") if isinstance(data, dict) else None
    if not isinstance(docs, list) or not docs:
        return ["documents가 비어 있음"]
    expect = 1
    for di, d in enumerate(docs, 1):
        pg = d.get("pages")
        if not (isinstance(pg, list) and len(pg) == 2 and all(isinstance(x, int) for x in pg)):
            errs.append(f"문서 {di}: pages 형식 위반")
            return errs
        if pg[0] != expect:
            errs.append(f"문서 {di}: 시작 쪽 {pg[0]} — 앞 문서 끝 다음 쪽({expect})이어야 함(빈틈·겹침 금지)")
        if pg[1] < pg[0]:
            errs.append(f"문서 {di}: 끝 쪽 < 시작 쪽")
        expect = pg[1] + 1
        bs, bk = d.get("body_start"), d.get("back_start")
        if not (isinstance(bs, int) and pg[0] <= bs <= pg[1]):
            errs.append(f"문서 {di}: body_start {bs}가 문서 범위 {pg[0]}-{pg[1]} 밖")
        if bk is not None and not (isinstance(bk, int) and isinstance(bs, int) and bs <= bk <= pg[1]):
            errs.append(f"문서 {di}: back_start {bk}가 body_start~문서 끝 범위 밖")
        ents = d.get("entries") or []
        prev = 0
        for ei, e in enumerate(ents, 1):
            dep = e.get("depth")
            if not (isinstance(dep, int) and 1 <= dep <= 4):
                errs.append(f"문서 {di} 항목 {ei}: depth {dep} (1~4)")
                break
            if dep > prev + 1:
                errs.append(f"문서 {di} 항목 {ei} '{(e.get('title') or '')[:20]}': depth {dep}가 직전 {prev}에서 "
                            "두 단계 이상 건너뜀")
                break
            prev = dep
            if not f"{e.get('label') or ''}{e.get('title') or ''}".strip():
                errs.append(f"문서 {di} 항목 {ei}: label·title 모두 공란")
                break
        flagged = [p for p in meta.get("toc_flag_pages", []) if pg[0] <= p <= pg[1]]
        if flagged and len(ents) < 3 and (pg[1] - pg[0] + 1) >= 20:
            errs.append(f"문서 {di}: 목차형 쪽({flagged[:5]})이 있는데 entries가 {len(ents)}개")
    if expect != n + 1 and not errs:
        errs.append(f"마지막 문서 끝 쪽 {expect - 1} ≠ PDF 끝 쪽 {n}")
    return errs


# ---------------------------------------------------------------------------
# LLM 호출 (annotate의 인증·예외 레이어 재사용)
# ---------------------------------------------------------------------------

def _parse_json_text(text: str):
    s = text or ""
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("JSON 객체 없음")
    return json.loads(s[a:b + 1])


async def call_model(content: list[dict], *, model: str, timeout: float) -> tuple[dict, dict]:
    """query() 1회(이미지 블록 포함 user 메시지). 반환: (JSON 객체, usage 메타)."""
    import annotate as an
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, RateLimitEvent, ResultMessage, TextBlock,
                                  query)

    options = ClaudeAgentOptions(
        # thinking은 지정하지 않는다 — claude-sonnet-5-5는 {"type": "disabled"}를 400으로 거부(2026-09-30 실측)
        system_prompt=SYSTEM_PROMPT, tools=[], max_turns=2, model=model,
        setting_sources=[], cwd=str(REPO_ROOT),
        output_format={"type": "json_schema", "schema": SCHEMA},
    )

    async def prompt():
        yield {"type": "user", "session_id": "", "message": {"role": "user", "content": content},
               "parent_tool_use_id": None}

    texts: list[str] = []
    result_msg = None

    async def consume() -> None:
        nonlocal result_msg
        async for msg in query(prompt=prompt(), options=options):
            if isinstance(msg, RateLimitEvent):
                info = msg.rate_limit_info
                if getattr(info, "status", None) == "rejected":
                    raise an.UsageLimitReached(an._limit_desc(info))
            elif isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        texts.append(block.text)
            elif isinstance(msg, ResultMessage):
                result_msg = msg

    stream_error = None
    try:
        await asyncio.wait_for(consume(), timeout)
    except asyncio.TimeoutError as e:
        raise an.RetryableError(f"타임아웃 {timeout:.0f}s") from e
    except an.UsageLimitReached:
        raise
    except Exception as e:  # noqa: BLE001 — SDK가 CLI 오류를 일반 Exception으로도 올린다(annotate.call_llm과 동일)
        stream_error = e
    if result_msg is not None and result_msg.is_error:
        detail = str(result_msg.result or result_msg.subtype)
        if result_msg.api_error_status == 429:
            raise an.UsageLimitReached("api_error_status=429")
        low = detail.lower()
        if "authenticate" in low or "not logged in" in low or "oauth" in low:
            raise an.AuthError(detail[:200])
        raise an.RetryableError(f"result error: {detail[:300]}")
    if stream_error is not None and result_msg is None:
        raise an.RetryableError(str(stream_error)[:300]) from stream_error
    usage = {"cost": getattr(result_msg, "total_cost_usd", None), "usage": getattr(result_msg, "usage", None),
             "turns": getattr(result_msg, "num_turns", None)}
    data = getattr(result_msg, "structured_output", None)
    if not isinstance(data, dict):
        text = result_msg.result if (result_msg is not None and isinstance(result_msg.result, str)) else ""
        try:
            data = _parse_json_text(text or "".join(texts))
        except ValueError as e:
            raise an.RetryableError(f"JSON 파싱 실패: {e}") from e
    return data, usage


async def read_pdf(pdf_path: Path, *, model: str, png_dir, timeout: float) -> tuple[dict | None, dict]:
    """PDF 1개 판독 → (판독 데이터 | None, 로그 항목). 게이트 위반이면 위반 문장을 붙여 1회 재요청."""
    import annotate as an
    t0 = time.time()
    stem = unicodedata.normalize("NFC", pdf_path.stem)
    # 폴더 이름은 60자로 자르고 끝 공백을 뗀다(Windows: 끝 공백 폴더 불가, 경로 길이 상한)
    content, meta = build_content(pdf_path, (Path(png_dir) / stem[:60].strip()) if png_dir else None)
    log = {"file": pdf_path.name, "n_pages": meta["n_pages"], "restarts": meta["restarts"],
           "images": meta["images"], "warnings": list(meta["warnings"]), "attempts": 0, "cost": 0.0,
           "violations": []}
    data, errs = None, ["호출 실패"]
    msg = content
    for attempt in range(2):
        last_exc = None
        for delay in [0.0] + an.RETRY_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            try:
                data, usage = await call_model(msg, model=model, timeout=timeout)
                last_exc = None
                break
            except an.RetryableError as e:
                last_exc = e
        log["attempts"] += 1
        if last_exc is not None:
            log["error"] = str(last_exc)
            data = None
            break
        log["cost"] += usage.get("cost") or 0.0
        log["turns"] = usage.get("turns")
        errs = validate(data, meta)
        if not errs:
            break
        log["violations"].append(errs)
        msg = content + [{"type": "text", "text": (
            "직전 답은 다음 규칙을 어겼다. 같은 자료로 다시 판독해 전체 JSON을 새로 답하라.\n- " + "\n- ".join(errs)
            + "\n\n직전 답:\n" + json.dumps(data, ensure_ascii=False)[:6000])}]
    log["seconds"] = round(time.time() - t0, 1)
    if data is None or errs:
        log["status"] = "docread_failed"
        return None, log
    out = {"file": pdf_path.name, "read_at": datetime.date.today().isoformat(), "model": model,
           "n_pages": meta["n_pages"], "documents": data["documents"]}
    log["status"] = "ok"
    log["documents"] = [{"pages": d["pages"], "body_start": d["body_start"], "back_start": d["back_start"],
                         "entries": len(d["entries"])} for d in data["documents"]]
    return out, log


def save(out: dict, pdf_path: Path, toc_dir=None) -> Path:
    p = registry.toc_path(pdf_path, toc_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    return p


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------

DEFAULT_PNG_DIR = "logs/docread"
DEFAULT_LOG = "logs/docread_log.json"


async def read_many(paths: list[Path], *, model: str, png_dir, timeout: float, concurrency: int,
                    toc_dir=None, force: bool = False) -> tuple[list[dict], Exception | None]:
    """판독 파일이 없는 PDF(force면 전부)를 판독해 저장한다. 반환 (PDF별 로그 항목, 중단 사유 | None).
    한도·인증 예외가 나면 새 판독을 시작하지 않고 멈춘다 — 그때까지 끝난 판독은 저장돼 있다."""
    import annotate as an
    sem = asyncio.Semaphore(concurrency)
    logs: list[dict] = []
    stop: list[Exception] = []

    async def one(pdf: Path):
        if stop:
            return
        if registry.toc_path(pdf, toc_dir).is_file() and not force:
            logs.append({"file": pdf.name, "status": "exists"})
            return
        async with sem:
            if stop:
                return
            try:
                out, log = await read_pdf(pdf, model=model, png_dir=png_dir, timeout=timeout)
            except (an.UsageLimitReached, an.AuthError) as e:
                stop.append(e)
                return
            except Exception as e:  # noqa: BLE001
                log, out = {"file": pdf.name, "status": "error", "error": repr(e)[:300]}, None
            if out is not None:
                save(out, pdf, toc_dir)
            logs.append(log)
            print(f"[{log['status']}] {pdf.name[:50]} — {log.get('seconds')}s, 이미지 {len(log.get('images', []))}장, "
                  f"문서 {len(log.get('documents', []))}, 항목 "
                  f"{[d['entries'] for d in log.get('documents', [])]}, ${log.get('cost', 0):.3f}"
                  + (f" · 재요청 {len(log['violations'])}" if log.get("violations") else "")
                  + (f" · {log.get('error')}" if log.get("error") else ""), file=sys.stderr)

    await asyncio.gather(*(one(p) for p in paths))
    return logs, (stop[0] if stop else None)


def write_log(logs: list[dict], log_path, model: str) -> None:
    """PDF별 마지막 판독 기록을 병합 저장(이미 있던 파일을 건너뛴 기록은 옛 항목을 덮지 않는다)."""
    lp = Path(log_path)
    lp.parent.mkdir(parents=True, exist_ok=True)
    prev = {}
    if lp.is_file():
        try:
            prev = {e["file"]: e for e in json.loads(lp.read_text(encoding="utf-8")).get("files", [])}
        except (ValueError, KeyError):
            prev = {}
    for e in logs:
        if e.get("status") != "exists" or e["file"] not in prev:
            prev[e["file"]] = e
    lp.write_text(json.dumps({"run_at": datetime.datetime.now().isoformat(timespec="seconds"), "model": model,
                              "files": list(prev.values())}, ensure_ascii=False, indent=1), encoding="utf-8")


def ensure_many(paths: list[Path], *, model: str = DEFAULT_MODEL, png_dir=DEFAULT_PNG_DIR, timeout: float = 600,
                concurrency: int = 3, toc_dir=None, log_path=DEFAULT_LOG) -> tuple[dict[str, dict], Exception | None]:
    """register용 — 판독 파일이 없는 PDF만 판독해 채운다(인증은 호출자가 annotate.setup_auth로 미리).
    반환 ({파일명: 로그 항목}, 중단 사유 | None). 로그 항목의 status = ok | exists | docread_failed | error."""
    os.environ.setdefault("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
    logs, stop = asyncio.run(read_many(paths, model=model, png_dir=png_dir, timeout=timeout,
                                       concurrency=concurrency, toc_dir=toc_dir))
    write_log(logs, log_path, model)
    return {e["file"]: e for e in logs}, stop


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="PDF당 1회 LLM 판독 — 합본 분할·본문 경계·목차 항목 → toc/*.json")
    ap.add_argument("pdf", nargs="+", help="대상 PDF (pdfs/*.pdf — 자체 글롭 확장)")
    ap.add_argument("--force", action="store_true", help="판독 파일이 있어도 다시 판독(헤딩 ID가 달라질 수 있음)")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"판독 모델 (기본: {DEFAULT_MODEL})")
    ap.add_argument("--out-dir", default=None, help="판독 파일 디렉터리 (기본: 저장소의 toc/ — 시험용으로만 바꾼다)")
    ap.add_argument("--png-dir", default=DEFAULT_PNG_DIR, help="렌더 PNG 보관 디렉터리 (빈 문자열이면 저장 안 함)")
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=600, help="호출당 타임아웃 초 (기본: 600)")
    ap.add_argument("--token-file", default=str(REPO_ROOT / ".claude_oauth_token"))
    args = ap.parse_args()

    paths: list[Path] = []
    for p in args.pdf:
        if not Path(p).is_file() and any(c in p for c in "*?["):
            paths.extend(Path(x) for x in sorted(globmod.glob(p)))
        else:
            paths.append(Path(p))
    if not paths:
        print("입력 PDF가 없습니다.", file=sys.stderr)
        sys.exit(2)
    import annotate as an
    os.environ.setdefault("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
    an.setup_auth(args)
    logs, stop = asyncio.run(read_many(paths, model=args.model, png_dir=args.png_dir, timeout=args.timeout,
                                       concurrency=args.concurrency, toc_dir=args.out_dir, force=args.force))
    write_log(logs, args.log, args.model)
    if stop is not None:
        print(an.fatal_msg(stop), file=sys.stderr)
        sys.exit(2)
    sys.exit(1 if any(e.get("status") in ("docread_failed", "error") for e in logs) else 0)


if __name__ == "__main__":
    main()
