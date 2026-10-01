"""목차 대응 추출 (R12, 2026-09-30 — 2026-10-01부터 유일한 구조 추출 경로).

판독 파일 `toc/{PDF stem}.json`(src/docread.py가 PDF당 1회 LLM 판독으로 만든 문서 범위·본문 시작·목차 항목)을
읽어 목차 항목을 본문 줄에 대응시키고(tocparse.anchor_entries), 대응된 줄을 헤딩으로 삼아 작성기
(extract.build_body·render_markdown)로 .md를 만든다. 본문 텍스트·헤딩 텍스트는 전부 PyMuPDF 줄 원문이다 —
판독 파일은 구조 포인터(제목·깊이·인쇄 쪽·경계)만 준다.

extract.process_pdf와 verify(`structure: toc`·`structure: flat` 분기)가 같은 build_structure·body_start_of를 쓴다.
tocparse가 extract를 import하므로 extract 쪽에서는 지연 import한다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import extract
import mdio
import registry
import tocparse
from tocparse import TocEntry

VIABLE_RATE = 0.9           # 깊이 1·2 항목 대응률 하한
VIABLE_MIN_DEPTH1 = 2       # 대응된 정상 장 수 하한
PREAMBLE_MIN_CHARS = 200    # 첫 헤딩 앞 본문이 이만큼 이상이면 합성 헤딩 `서두`
NEIGHBOR_REACH = 3          # 꼬리말 없는 쪽(장 표제 쪽)의 위치를 이웃 번호에서 추정하는 거리
PREAMBLE_RE = re.compile(r"^서두 \(p\.\d+(?:-\d+)?\)$")
CHUNK_RE = re.compile(r"^구간 \d+ \(p\.\d+(?:-\d+)?\)$")   # 절 내부 청킹의 합성 헤딩 제목(플랫 레인과 같은 꼴)
MAX_DEPTH = 4               # mdio.HEADING_RE가 받는 헤딩 깊이 상한 — 4단 헤딩 아래로는 구간 헤딩을 만들 수 없다


# ---------------------------------------------------------------------------
# 판독 파일
# ---------------------------------------------------------------------------

def load_docread(pdf_path) -> dict | None:
    p = registry.toc_path(pdf_path)
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def pick_document(data: dict, part) -> dict | None:
    """등록부 파트(0-based 쪽 범위)와 쪽 범위가 같은 판독 문서. 판독 파일의 쪽은 1-based."""
    want = [part[0] + 1, part[1] + 1]
    for d in data.get("documents", []):
        if list(d.get("pages", [])) == want:
            return d
    return None


def body_start_of(docd: dict, part) -> int:
    """판독의 본문 시작(1-based PDF 쪽) → 문서 범위 안으로 보정한 0-based 쪽. 목차 대응·플랫·verify 공용."""
    body_start = int(docd.get("body_start") or (part[0] + 1)) - 1
    return max(part[0], min(body_start, part[1]))


def entries_from(docd: dict) -> list[TocEntry]:
    out: list[TocEntry] = []
    for i, it in enumerate(docd.get("entries", [])):
        label = (it.get("label") or "").strip()
        title = (it.get("title") or "").strip()
        raw = f"{label} {title}".strip()
        if not raw:
            continue
        fam, tok, _rest = tocparse.tokenize(raw)
        kind_in = it.get("kind") or "body"
        bk = tocparse.backmatter_kind(raw, title)
        kind = bk if bk != "normal" else ("appendix" if kind_in == "back" else "normal")
        pg = it.get("page")
        out.append(TocEntry(
            order=i, depth=max(1, min(int(it.get("depth") or 1), 4)), family=fam, token=tok or label,
            title=title or raw,
            # 판독이 번호를 분리해 준 항목은 그 분리를 따른다(`Ⅱ.1.` + `사업개요` — 다시 토큰 분해하면 제목이 `1. 사업개요`)
            title_fold=extract.fold(title) if (label and title) else (
                tocparse.strip_token_fold(raw) if fam != "none" else extract.fold(raw)),
            full_fold=extract.fold(raw),
            printed_page=pg if isinstance(pg, int) and pg > 0 else None,
            front=(kind_in == "front"), kind=kind, page=0, indent=0.0, label=label))
    return out


# ---------------------------------------------------------------------------
# 인쇄 쪽 → PDF 쪽 (꼬리말 직접 조회)
# ---------------------------------------------------------------------------

def footer_window_fn(scans, part, body_start: int, two_up: bool = False):
    """목차의 인쇄 쪽 번호를 꼬리말 번호가 같은 PDF 쪽에서 직접 찾는다(오프셋 가정 없음).
    이웃 쪽과 번호가 이어지는 쪽만 믿고(오독 차단), 같은 번호가 본문 영역에 여러 번이면 쓰지 않는다.
    번호가 찍히지 않은 쪽(장 표제 쪽)은 ±NEIGHBOR_REACH 안의 이웃 번호에서 위치를 추정한다.
    펼침 판형(two_up: PDF 한 쪽에 인쇄 두 쪽, 꼬리말은 쪽당 하나라 2씩 증가)은 인쇄 쪽 n을 꼬리말 n 또는 n-1인 쪽에서 찾는다."""
    step = 2 if two_up else 1
    by_num: dict[int, list[int]] = {}
    for p in range(body_start, part[1] + 1):
        fa = scans[p].footer_arabic
        if fa is None:
            continue
        for q in (p - 1, p + 1):
            if part[0] <= q <= part[1] and scans[q].footer_arabic is not None \
                    and scans[q].footer_arabic - fa == (q - p) * step:
                by_num.setdefault(fa, []).append(p)
                break

    def fn(e: TocEntry):
        n = e.printed_page
        if n is None:
            return None
        if two_up:
            for m in (n, n - 1):
                ps = by_num.get(m)
                if ps and len(ps) == 1:
                    return ps[0] - 1, ps[0] + 1, ps[0], True
            return None
        for k in range(0, NEIGHBOR_REACH + 1):
            for d in ((0,) if k == 0 else (-k, k)):
                ps = by_num.get(n + d)
                if ps and len(ps) == 1:
                    t = ps[0] - d
                    if body_start <= t <= part[1]:
                        # 넷째 값 = 정확 일치(목차 쪽 번호와 꼬리말이 같은 쪽을 직접 찾음) — 위치 기반 대응의 전제
                        return t - 1, t + 1, t, k == 0
        return None

    return fn


# ---------------------------------------------------------------------------
# 구조 재현 (extract·verify 공용)
# ---------------------------------------------------------------------------

def build_structure(scans, part, docd: dict, rid: str, warnings: list):
    """판독 문서 → (structure | None, body_start, info). 호출 전제: 없음(각주·판권 표식은 여기서 한다).

    structure = extract.build_body가 받는 {"body_lines", "chapters", "sub_headings"} + "preamble": (첫 쪽, 끝 쪽) | None,
    "ordered": 읽기 순서 헤딩 목록. None이면 info["reason"]에 불성립 사유."""
    info: dict = {}
    body_start = body_start_of(docd, part)
    info["body_start_page"] = body_start + 1
    extract.mark_footnotes(scans, (body_start, part[1]))
    extract.mark_colophon_pages(scans, part, warnings)

    entries = entries_from(docd)
    targets = [e for e in entries if not e.front]
    info["entries"] = len(entries)
    info["targets"] = len(targets)
    if not targets:
        info["reason"] = "no_toc"
        return None, body_start, info

    pmap = tocparse.page_offsets(scans, (body_start, part[1]))
    two_up = pmap.two_up and scans[body_start].width > scans[body_start].height
    if two_up:   # 펼침 판형 — 줄 읽기 순서를 왼쪽 면 → 오른쪽 면으로(앵커링·본문 방출 공통)
        for p in range(part[0], part[1] + 1):
            scans[p].two_up = True
        info["two_up"] = True
    lines = tocparse.body_lines(scans, part, body_start)
    anchors, astats = tocparse.anchor_entries(
        scans, part, entries, pmap, body_start, lines, front_guard=False, front_penalty=False,
        window_fn=footer_window_fn(scans, part, body_start, two_up), global_sim=True)
    by_order = {e.order: e for e in entries}
    got = [a for a in anchors if a.tier != "none"]
    # 성립 판정은 본문 항목만 — 참고문헌·부록·붙임(과 그 하위)은 미대응이어도 경고만(목차에 제목 없이 `부록 1`로만
    # 실리고 본문은 숫자 + 다른 제목인 판형은 못 잡는 것이 정상, 2025-17)
    d12 = [a for a in anchors if by_order[a.order].depth <= 2 and by_order[a.order].kind == "normal"]
    d12_ok = [a for a in d12 if a.tier != "none"]
    for a in got:
        if a.tier == "5":
            e5 = by_order[a.order]
            toc_text = f"{e5.label} {e5.title}".strip()
            warnings.append(f"위치 기반 대응(p.{a.page + 1}): 목차 '{toc_text}' ↔ 본문 '{a.text[:40]}'")
    d1_ok = [a for a in got if by_order[a.order].depth == 1 and by_order[a.order].kind == "normal"]
    rate12 = (len(d12_ok) / len(d12)) if d12 else 0.0
    info.update({
        "anchored": len(got), "by_tier": astats["by_tier"], "rate_depth12": round(rate12, 3),
        "depth1_anchored": len(d1_ok),
        "unanchored": [f"{'#' * by_order[a.order].depth} {by_order[a.order].token} {by_order[a.order].title}".strip()
                       for a in anchors if a.tier == "none"],
        "leaf_gt_4000": astats["leaf_gt_4000"], "leaf_gt_8000": astats["leaf_gt_8000"],
        "deltas": astats["deltas"],
    })
    if rate12 < VIABLE_RATE or len(d1_ok) < VIABLE_MIN_DEPTH1:
        info["reason"] = "anchor"
        return None, body_start, info

    body = extract.collect_body_lines(scans, part, body_start)
    addr_of = {(pg, j): idx for idx, (pg, j, _ln) in enumerate(body)}
    got.sort(key=lambda a: a.g)
    anchor_gs = {a.g for a in got}

    # 헤딩 줄(두 줄 결합이면 앞 줄이 addr, 나머지는 extra_addrs — 구조 레인과 같은 규약)
    raw_heads = []
    for a in got:
        e = by_order[a.order]
        ln = lines[a.g]
        first, extra, text = a.g, [], ln.text
        partner = tocparse.join_partner(e, lines, a.g)
        if partner is not None and not any(x in anchor_gs for x in partner[0] if x != a.g):
            gs, text = partner
            first, extra = gs[0], gs[1:]
        addr = addr_of.get((lines[first].page, lines[first].j))
        if addr is None:
            warnings.append(f"p.{ln.page + 1} 대응 줄이 본문 범위 밖: {ln.text[:30]}")
            continue
        extras = [addr_of[(lines[x].page, lines[x].j)] for x in extra if (lines[x].page, lines[x].j) in addr_of]
        raw_heads.append((addr, extras, e, re.sub(r"\s+", " ", text).strip()))

    # 첫 헤딩 앞 본문 → 합성 헤딩 `서두`(서수 c1을 차지)
    preamble = None
    if raw_heads:
        first_addr = raw_heads[0][0]
        pre_chars = sum(len(ln.text.strip()) for _pg, _j, ln in body[:first_addr]
                        if not (ln.in_table or ln.is_leader or ln.is_footnote))
        if pre_chars >= PREAMBLE_MIN_CHARS:
            preamble = (body_start + 1, max(body_start + 1, body[first_addr][0] + 1))
            if preamble[1] > preamble[0] and first_addr > 0 and body[first_addr - 1][0] + 1 < preamble[1]:
                preamble = (preamble[0], body[first_addr - 1][0] + 1)
            info["preamble_chars"] = pre_chars
            warnings.append(f"첫 헤딩 앞 본문 {pre_chars}자 — 합성 헤딩 '서두' (p.{preamble[0]}-{preamble[1]})")

    # 서수 경로 ID: mdio.load_report의 부모 규칙(직전의 더 얕은 헤딩)으로 해결한 트리에서 부여
    chapters, subs, ordered = [], [], []
    n_root = 1 if preamble else 0
    stack: list[tuple[int, object, list]] = []   # (depth, heading, [child count])
    for addr, extras, e, text in raw_heads:
        depth = e.depth
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if not stack:
            if depth != 1:
                warnings.append(f"장 미대응 — 하위 항목을 최상위로: {text[:30]}")
                depth = 1
            n_root += 1
            hid = f"{rid}_c{n_root}"
        else:
            stack[-1][2][0] += 1
            hid = f"{stack[-1][1].hid}s{stack[-1][2][0]}"
        h = extract.Heading(addr=addr, depth=depth, hid=hid, text=text, kind=e.kind if depth == 1 else "normal",
                            family=e.family, value=None, extra_addrs=extras)
        (chapters if depth == 1 else subs).append(h)
        ordered.append(h)
        stack.append((depth, h, [0]))
    info["headings"] = len(chapters) + len(subs)
    info["chapters"] = len(chapters) + (1 if preamble else 0)
    structure = {"body_lines": body, "chapters": chapters, "sub_headings": subs,
                 "preamble": preamble, "ordered": ordered}   # ordered = 읽기 순서(펼침 판형은 addr 순서와 다르다)
    return structure, body_start, info


def preamble_heading(rid: str, preamble) -> str:
    a, b = preamble
    label = f"p.{a}" if a == b else f"p.{a}-{b}"
    return f"# 서두 ({label}) <!-- id: {rid}_c1 -->"


# ---------------------------------------------------------------------------
# 절 내부 청킹 (목차에 없는 하위 단을 글자 수 기준 구간으로)
# ---------------------------------------------------------------------------

def chunk_sections(body: list[str], page_marks: list, warnings: list) -> tuple[list[str], dict]:
    """목차 헤딩 가운데 하위 헤딩이 없는 것(잎)의 본문이 유닛 상한(extract.FLAT_MAX_CHARS)을 넘으면 플랫 레인과 같은
    규칙(문단 경계 그리디 누적, 최소 크기 미만 꼬리 병합)으로 나눠 한 단 아래 합성 헤딩
    `구간 k (p.a-b) <!-- id: {잎 ID}s{k} -->`을 넣는다. 목차 헤딩의 ID는 바뀌지 않는다(구간은 잎의 자식 서수).

    나누지 않는 것: 참고문헌·부록 장(유닛 제외 대상), 하위 헤딩이 있는 헤딩의 직속 본문(구간이 실제 하위 헤딩의
    서수를 밀어낸다), 4단 헤딩(더 깊은 헤딩 불가), 블록 하나가 상한을 넘는 경우 — 뒤 둘은 경고만 남긴다."""
    heads = []
    for i, ln in enumerate(body):
        m = mdio.HEADING_RE.match(ln)
        if m:
            heads.append((i, len(m.group(1)), m.group(2), m.group(3)))
    page_of = extract.page_lookup(page_marks)
    stats = {"chunked_sections": 0, "chunk_headings": 0, "oversize_left": 0}
    out: list[str] = []
    pos = 0
    excluded = False
    for k, (i, depth, text, hid) in enumerate(heads):
        nxt_i, nxt_depth = (heads[k + 1][0], heads[k + 1][1]) if k + 1 < len(heads) else (len(body), 0)
        if depth == 1:
            excluded = mdio.is_excluded_chapter(text)
        if excluded or nxt_depth > depth:
            continue
        size = len("\n".join(body[i + 1:nxt_i]).strip())
        if size <= extract.FLAT_MAX_CHARS:
            continue
        blocks = extract.out_blocks(body, i + 1, nxt_i)
        chunks = extract.greedy_chunks(blocks) if depth < MAX_DEPTH else []
        if len(chunks) < 2:
            stats["oversize_left"] += 1
            why = f"{MAX_DEPTH}단 헤딩" if depth >= MAX_DEPTH else "블록 하나가 상한 초과"
            warnings.append(f"절 본문 {size}자 > {extract.FLAT_MAX_CHARS} — 구간 분할 불가({why}): {text[:30]}")
            continue
        out.extend(body[pos:i + 1])
        out.append("")
        for n, c in enumerate(chunks, 1):
            sp = extract.chunk_span(blocks, c)
            if sp > extract.FLAT_MAX_CHARS:
                warnings.append(f"구간 {hid}s{n} 크기 {sp}자 > {extract.FLAT_MAX_CHARS} — 단일 블록 초과/꼬리 병합")
            label = extract.chunk_page_label(blocks, c, page_of)
            out.append(f"{'#' * (depth + 1)} 구간 {n} ({label}) <!-- id: {hid}s{n} -->")
            out.append("")
            for bl, _ in blocks[c[0]:c[1]]:
                out.extend(bl)
                out.append("")
        pos = nxt_i
        stats["chunked_sections"] += 1
        stats["chunk_headings"] += len(chunks)
    out.extend(body[pos:])
    while out and out[-1] == "":
        out.pop()
    return out, stats


# ---------------------------------------------------------------------------
# extract 진입점
# ---------------------------------------------------------------------------

def process_part(r, scans, part, rid: str, path: str, docd: dict) -> None:
    """extract.process_pdf의 목차 대응 분기 — r(PartResult)을 채운다. meta는 호출자가 채워 둔다.
    docd = 이 문서의 판독(pick_document)."""
    diag = r.stats.setdefault("diag", {})
    diag["lane"] = "toc"
    structure, body_start, info = build_structure(scans, part, docd, rid, r.warnings)
    diag["toc_lane"] = info
    diag["body_start_page"] = info.get("body_start_page")
    r.body_start = body_start
    if structure is None:
        r.status = "skipped_no_toc" if info.get("reason") == "no_toc" else "skipped_toc_anchor"
        r.warnings.append("판독 파일에 목차 항목 없음" if info.get("reason") == "no_toc" else
                          f"목차 대응 불성립(깊이 1·2 대응률 {info.get('rate_depth12')}, 장 {info.get('depth1_anchored')})")
        return
    for t in info["unanchored"]:
        r.warnings.append(f"목차 항목 미대응: {t[:50]}")
    r.structure = structure
    r.structure_tag = "toc"
    page_marks: list = []
    body = extract.build_body(scans, part, body_start, structure, r, page_marks=page_marks)
    if structure["preamble"]:
        body = [preamble_heading(rid, structure["preamble"]), ""] + body
        page_marks = [(i + 2, pg) for i, pg in page_marks]
    body, cstats = chunk_sections(body, page_marks, r.warnings)
    info.update(cstats)
    r.markdown = extract.render_markdown(r, f"pdfs/{Path(path).name}", body)
    r.stats["headings"] = info["headings"] + (1 if structure["preamble"] else 0)
    r.stats["chapters"] = info["chapters"]
