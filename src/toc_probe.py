"""목차 앵커 실측 프로브 — Phase 1 (2026-09-22). 읽기 전용, LLM 0.

PDF마다: 스캔 → 합본 분할(현행 detect_parts vs 장 문법 없는 detect_parts_v2 대조) → 파트마다 목차 페이지 → 행 조립 →
항목 파싱 → 쪽 대응표 → 본문 줄 앵커링 → (적재분) reports/{rid}.md 헤딩 트리와 대조. 결과는 logs/toc_probe.json 하나에
쓰고 콘솔에 권당 한 줄 요약표 + 코퍼스 요약 + 최악 사례를 찍는다. reports/·report_ids.tsv·DB는 건드리지 않는다.

  python src/toc_probe.py --loaded            # pdfs/에 있고 reports/{rid}.md가 있는 권(표 인식 포함 스캔)
  python src/toc_probe.py --hold              # pdfs/hold/*.pdf (경량 스캔)
  python src/toc_probe.py --pdf 경로 [...]     # 지정 파일만
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

import pymupdf

import mdio
import registry
import tocparse
from extract import (
    detect_parts,
    find_body_start,
    mark_colophon_pages,
    mark_footnotes,
    scan_document,
)

REPO = Path(__file__).resolve().parent.parent
SRC_RE = re.compile(r'^source_pdf:\s*"?(.*?)"?\s*$')
VIABLE_RATE = 0.9


# ---------------------------------------------------------------------------
# 대상 수집
# ---------------------------------------------------------------------------

def loaded_targets() -> dict[str, list[tuple[str, Path]]]:
    """{PDF 경로: [(rid, md 경로)]} — reports/*.md의 source_pdf를 pdfs/ → pdfs/hold/ 순으로 해석."""
    groups: dict[str, list[tuple[str, Path]]] = {}
    for md in sorted((REPO / "reports").glob("*.md")):
        src = None
        with open(md, encoding="utf-8") as f:
            for _ in range(60):
                line = f.readline()
                if not line:
                    break
                m = SRC_RE.match(line.rstrip("\n"))
                if m:
                    src = m.group(1)
                    break
        if not src:
            print(f"[경고] {md.name}: source_pdf 없음 — 제외", file=sys.stderr)
            continue
        p = REPO / src
        if not p.is_file():
            alt = REPO / "pdfs" / "hold" / Path(src).name
            if alt.is_file():
                p = alt
            else:
                print(f"[경고] {md.name}: PDF 없음 {src} — 제외", file=sys.stderr)
                continue
        groups.setdefault(str(p), []).append((md.stem, md))
    return groups


# ---------------------------------------------------------------------------
# 파트 분석
# ---------------------------------------------------------------------------

def analyze_part(scans, part, label: str, md_path: Path | None) -> dict:
    rec: dict = {"id": label, "part": [part[0] + 1, part[1] + 1], "pages": part[1] - part[0] + 1}
    pages, tinfo = tocparse.toc_page_set(scans, part)
    rec["toc"] = dict(tinfo, found=bool(pages))
    bs_cur, _c = find_body_start(scans, part)
    rec["body_start"] = {"current": (bs_cur + 1) if bs_cur is not None else None, "toc": None, "agree": None}
    if not pages:
        rec["entries"] = {"n": 0}
        rec["viable"] = False
        return rec
    rows, rinfo = tocparse.toc_rows(scans, pages)
    entries, einfo = tocparse.parse_entries(rows)
    consumed = rinfo.pop("consumed")
    domain_start = pages[-1]   # 마지막 목차 쪽 포함(목차 옆 본문 열) — 소비된 줄은 제외
    mark_footnotes(scans, (min(domain_start + 1, part[1]), part[1]))
    mark_colophon_pages(scans, part, [])
    lines = tocparse.body_lines(scans, part, domain_start, consumed)
    pmap = tocparse.page_offsets(scans, part)
    anchors, astats = tocparse.anchor_entries(scans, part, entries, pmap, domain_start, lines)
    by_order = {e.order: e for e in entries}
    targets = [e for e in entries if not e.front]
    rec["entries"] = {
        "n": len(entries), "targets": len(targets),
        "with_number": sum(1 for e in targets if e.printed_page is not None),
        "front": sum(1 for e in entries if e.front),
        "by_depth": dict(Counter(e.depth for e in targets)),
        "kinds": dict(Counter(e.kind for e in targets)),
        "family_order": einfo["family_order"], "chapter_family": einfo.get("chapter_family"), "joined": einfo["joined"], "excluded": einfo["excluded"],
        "english_skipped": einfo.get("english_skipped", 0), "front_cut": einfo.get("front_cut", 0), "duplicate_toc": einfo.get("duplicate_toc", 0),
        "duplicates": einfo["duplicates"], **rinfo,
    }
    rec["pmap"] = dataclasses.asdict(pmap)
    rec["anchor"] = astats
    n_t = len(targets)
    rate = (astats["anchored"] / n_t) if n_t else 0.0
    rec["anchor_rate"] = round(rate, 3)
    rec["viable"] = n_t >= 3 and rate >= VIABLE_RATE and astats["depth1_anchored"] >= 2
    d1 = [a.page for a in anchors if a.tier != "none" and by_order[a.order].depth == 1
          and by_order[a.order].kind == "normal"]
    bs_toc = min(d1) if d1 else None
    rec["body_start"]["toc"] = (bs_toc + 1) if bs_toc is not None else None
    rec["body_start"]["agree"] = (bs_toc == bs_cur) if (bs_toc is not None and bs_cur is not None) else None
    rec["anchors"] = [
        {"order": a.order, "depth": by_order[a.order].depth, "title": by_order[a.order].title[:50],
         "printed": by_order[a.order].printed_page, "tier": a.tier,
         "page": (a.page + 1) if a.page is not None else None, "text": a.text[:50],
         "delta": a.delta, "n_cands": a.n_cands}
        for a in anchors]
    if md_path is not None:
        try:
            st = mdio.load_report(md_path)
        except Exception as e:  # noqa: BLE001
            rec["md"] = {"error": str(e)}
            return rec
        headings = list(mdio.iter_headings(st.roots))
        hits = tocparse.md_headings_on_pages(lines, headings)
        anchored_g = {a.g: a.order for a in anchors if a.g is not None}
        md_g_all = {g for h in hits for g in h.get("gs", [])}
        by_depth: dict[int, dict] = {}
        hid_map: dict[str, int] = {}
        for h in hits:
            d = h["depth"]
            b = by_depth.setdefault(d, {"n": 0, "one": 0, "zero": 0, "multi": 0, "covered": 0, "not_in_toc": 0})
            if h["hits"] == -1:
                continue
            b["n"] += 1
            if h["hits"] == 1:
                b["one"] += 1
            elif h["hits"] == 0:
                b["zero"] += 1
            else:
                b["multi"] += 1
            hit_orders = [anchored_g[g] for g in h["gs"] if g in anchored_g]
            if hit_orders:   # 한 번이든 여러 번이든 그중 하나가 대응된 목차 항목의 줄이면 커버
                b["covered"] += 1
                hid_map[h["hid"]] = hit_orders[0]
            elif h["hits"] > 0:
                b["not_in_toc"] += 1
        toc_not_in_md = [{"order": o, "title": by_order[o].title[:40], "page": lines[g].page + 1}
                         for g, o in sorted(anchored_g.items()) if g not in md_g_all]
        rec["md"] = {
            "headings": sum(1 for h in hits if h["hits"] != -1), "flat": any(h["hits"] == -1 for h in hits),
            "by_depth": by_depth, "toc_not_in_md": len(toc_not_in_md), "toc_not_in_md_list": toc_not_in_md[:10],
            "zero_hit_list": [f"{h['hid']} {h['text'][:40]}" for h in hits if h["hits"] == 0][:10],
            "hid_map": hid_map,
        }
    return rec


# ---------------------------------------------------------------------------
# PDF 단위 실행
# ---------------------------------------------------------------------------

def run_pdf(pdf_path: str, members: list[tuple[str, Path | None]] | None, tables: bool) -> list[dict]:
    """members = [(rid, md_path)] (적재분, 등록부 파트) 또는 None(보류분: 현행 분할로 파트 열거)."""
    t0 = time.time()
    doc = pymupdf.open(pdf_path)
    try:
        scans = scan_document(doc, tables=tables)
    finally:
        doc.close()
    n = len(scans)
    parts_cur = detect_parts(scans)
    parts_v2 = tocparse.detect_parts_v2(scans)
    parts_equal = parts_cur == parts_v2
    name = Path(pdf_path).name
    out: list[dict] = []
    if members is not None:
        reg = registry.parts_for(pdf_path)
        ranges = {rid: (rng or (0, n - 1)) for rid, rng in reg}
        for rid, md_path in members:
            part = ranges.get(rid)
            if part is None:
                print(f"[경고] {name}: 등록부 파트에 {rid} 없음 — 파일 전체로", file=sys.stderr)
                part = (0, n - 1)
            rec = analyze_part(scans, part, rid, md_path)
            rec.update({"file": name, "set": "loaded", "n_pages": n})
            out.append(rec)
    else:
        stem = Path(pdf_path).stem
        for k, part in enumerate(parts_cur):
            label = stem if len(parts_cur) == 1 else f"{stem}#{k + 1}"
            rec = analyze_part(scans, part, label, None)
            rec.update({"file": name, "set": "hold", "n_pages": n})
            out.append(rec)
    fmt = lambda rs: ", ".join(f"{a + 1}-{b + 1}" for a, b in rs)  # noqa: E731
    for rec in out:
        rec["parts"] = {"equal": parts_equal, "current": fmt(parts_cur), "v2": fmt(parts_v2)}
        rec["seconds"] = round(time.time() - t0, 1)
    return out


# ---------------------------------------------------------------------------
# 요약 · 출력
# ---------------------------------------------------------------------------

def _pct(a: int, b: int) -> str:
    return f"{100 * a / b:.0f}%" if b else "-"


def summarize(vols: list[dict]) -> dict:
    n = len(vols)
    with_toc = [v for v in vols if v["toc"]["found"]]
    parsable = [v for v in with_toc if v["entries"].get("targets", 0) >= 3]
    mappable = [v for v in parsable if v.get("pmap", {}).get("mappable")]
    viable = [v for v in parsable if v.get("viable")]
    rates = [v["anchor_rate"] for v in parsable]
    buckets = Counter()
    for r in rates:
        buckets["≥0.9" if r >= 0.9 else "0.7~0.9" if r >= 0.7 else "0.5~0.7" if r >= 0.5 else "<0.5"] += 1
    tot_targets = sum(v["entries"].get("targets", 0) for v in parsable)
    tot_num = sum(v["entries"].get("with_number", 0) for v in parsable)
    deltas = Counter()
    for v in parsable:
        for d in v["anchor"].get("deltas", []):
            deltas[d] += 1
    bs = [v["body_start"]["agree"] for v in vols if v["body_start"]["agree"] is not None]
    cov = {d: [] for d in (1, 2, 3, 4)}
    anchorable = {d: [0, 0] for d in (1, 2, 3, 4)}
    toc_not_in_md = 0
    for v in vols:
        md = v.get("md")
        if not md or "by_depth" not in md:
            continue
        toc_not_in_md += md["toc_not_in_md"]
        for d, b in md["by_depth"].items():
            d = int(d)
            if b["n"]:
                cov[d].append(b["covered"] / b["n"])
                anchorable[d][0] += b["one"]
                anchorable[d][1] += b["n"]
    files = {}
    for v in vols:
        files.setdefault(v["file"], v["parts"]["equal"])
    parts_diff = [f for f, eq in files.items() if not eq]
    return {
        "volumes": n, "files": len(files), "toc_found": len(with_toc), "toc_parsable": len(parsable),
        "mappable": len(mappable), "viable": len(viable), "anchor_rate_buckets": dict(buckets),
        "entries_with_number": tot_num, "entries_total": tot_targets,
        "delta_histogram": dict(sorted(deltas.items(), key=lambda kv: -kv[1])[:10]),
        "body_start_agree": sum(1 for b in bs if b), "body_start_compared": len(bs),
        "coverage_median_by_depth": {d: (round(statistics.median(c), 3) if c else None) for d, c in cov.items()},
        "md_anchorable_by_depth": {d: (f"{a[0]}/{a[1]}") for d, a in anchorable.items()},
        "toc_not_in_md": toc_not_in_md,
        "residue": n - len(viable), "residue_no_toc": n - len(with_toc),
        "parts_diff_files": parts_diff,
        "leaf_gt_4000": sum(v["anchor"].get("leaf_gt_4000", 0) for v in parsable),
        "sections": sum(v["anchor"].get("sections", 0) for v in parsable),
    }


def print_table(vols: list[dict]) -> None:
    hdr = f"{'id':22} {'pg':>4} {'toc':7} {'ent':>4} {'num%':>4} {'map':>9} {'anch':>5} {'1/2/3/4/G':>14} {'un':>3} {'cov ##/###/####':>16} {'bs':>2} {'pt':>2}"
    print(hdr)
    for v in vols:
        e = v["entries"]
        toc = v["toc"]
        tock = (toc.get("kind") or "-")[:4] + ("+" if toc.get("two_col_pages") else "")
        if not toc["found"]:
            tock = "none"
        ent = e.get("targets", 0)
        num = _pct(e.get("with_number", 0), ent) if ent else "-"
        pm = v.get("pmap")
        mp = (f"{pm['offset']:+d}/{pm['share']:.2f}" + ("2u" if pm["two_up"] else "")) if pm and pm["offset"] is not None else "-"
        a = v.get("anchor", {})
        bt = a.get("by_tier", {})
        tiers = "/".join(str(bt.get(k, 0) + bt.get(f"G{k}", 0)) for k in ("1", "2", "3", "4"))
        g = sum(c for k, c in bt.items() if k.startswith("G"))
        tiers = f"{tiers}/{g}"
        un = bt.get("none", 0)
        md = v.get("md")
        if md and "by_depth" in md:
            cov = "/".join(_pct(md["by_depth"].get(d, md["by_depth"].get(str(d), {})).get("covered", 0),
                                md["by_depth"].get(d, md["by_depth"].get(str(d), {})).get("n", 0))
                           for d in (2, 3, 4))
        else:
            cov = "-"
        bsa = v["body_start"]["agree"]
        bs = "=" if bsa else ("≠" if bsa is False else "-")
        pt = "=" if v["parts"]["equal"] else "≠"
        anch = f"{100 * v.get('anchor_rate', 0):.0f}%" if ent else "-"
        print(f"{v['id'][:22]:22} {v['pages']:>4} {tock:7} {ent:>4} {num:>4} {mp:>9} {anch:>5} {tiers:>14} {un:>3} {cov:>16} {bs:>2} {pt:>2}")


def print_summary(s: dict, vols: list[dict], worst: int) -> None:
    print()
    print("== 코퍼스 요약 ==")
    print(f"파트 {s['volumes']} (파일 {s['files']}) · 목차 발견 {s['toc_found']} · 파싱 가능(항목 3+) {s['toc_parsable']} · "
          f"쪽 예측 가능 {s['mappable']} · 성립(대응 ≥ {VIABLE_RATE:.0%}, 1단 ≥ 2) {s['viable']}")
    print(f"대응률 분포 {s['anchor_rate_buckets']} · 쪽 번호 있는 항목 {s['entries_with_number']}/{s['entries_total']}")
    print(f"전역 탐색 예측 쪽 차이 상위 {s['delta_histogram']}")
    print(f"본문 시작 일치 {s['body_start_agree']}/{s['body_start_compared']} · 합본 분할 불일치 파일 {len(s['parts_diff_files'])}: {s['parts_diff_files'][:10]}")
    print(f"정답 커버리지 중위(깊이별) {s['coverage_median_by_depth']} · .md 헤딩 정확히 한 번 찾힘(깊이별) {s['md_anchorable_by_depth']} · "
          f".md에 없는 대응 항목 {s['toc_not_in_md']}")
    print(f"4,000자 초과 잎 구간 {s['leaf_gt_4000']}/{s['sections']} · 잔여(불성립) {s['residue']} (목차 없음 {s['residue_no_toc']})")
    bad = sorted((v for v in vols if v["toc"]["found"] and v["entries"].get("targets", 0) >= 3),
                 key=lambda v: (-(v["anchor"]["by_tier"].get("none", 0)), v["anchor_rate"]))[:worst]
    if bad:
        print()
        print(f"== 미대응 많은 순 {len(bad)}건 ==")
        for v in bad:
            a = v["anchor"]
            print(f"- {v['id']}: 대응 {a['anchored']}/{a['targets']} (순서 탈락 {a['by_tier'].get('dropped_by_order', 0)}) "
                  f"미대응 예: {' | '.join(a['unanchored_titles'][:5])}")
    zero = [(v["id"], v["md"]["zero_hit_list"]) for v in vols if v.get("md") and v["md"].get("zero_hit_list")]
    if zero:
        print()
        print(f"== .md 헤딩을 본문에서 못 찾은 권 {len(zero)}건 (매처 결함 의심 순서로 확인) ==")
        for rid, lst in zero[:worst]:
            print(f"- {rid}: {' | '.join(lst[:4])}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loaded", action="store_true")
    ap.add_argument("--hold", action="store_true")
    ap.add_argument("--pdf", nargs="*", default=[])
    ap.add_argument("--toc-window", type=int, default=tocparse.TOC_WINDOW_PAGES)
    ap.add_argument("--out", default=str(REPO / "logs" / "toc_probe.json"))
    ap.add_argument("--worst", type=int, default=20)
    ap.add_argument("--limit", type=int, default=0, help="처음 N개 PDF만(스모크)")
    ap.add_argument("--no-tables", action="store_true", help="적재분도 경량 스캔")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    tocparse.TOC_WINDOW_PAGES = args.toc_window

    jobs: list[tuple[str, list | None, bool]] = []
    if args.loaded:
        for p, members in loaded_targets().items():
            jobs.append((p, members, not args.no_tables))
    if args.hold:
        for p in sorted((REPO / "pdfs" / "hold").glob("*.pdf")):
            jobs.append((str(p), None, False))
    for p in args.pdf:
        pp = Path(p)
        if not pp.is_file():
            print(f"[오류] 파일 없음: {p}", file=sys.stderr)
            return 2
        members = None
        lt = loaded_targets()
        if str(pp.resolve()) in {str(Path(k).resolve()) for k in lt}:
            key = next(k for k in lt if Path(k).resolve() == pp.resolve())
            members = lt[key]
        jobs.append((str(pp), members, members is not None and not args.no_tables))
    if args.limit:
        jobs = jobs[:args.limit]
    if not jobs:
        ap.print_help()
        return 2

    vols: list[dict] = []
    t0 = time.time()
    for i, (p, members, tables) in enumerate(jobs, 1):
        print(f"[{i}/{len(jobs)}] {Path(p).name}", file=sys.stderr)
        try:
            vols.extend(run_pdf(p, members, tables))
        except Exception as e:  # noqa: BLE001
            print(f"[오류] {Path(p).name}: {type(e).__name__}: {e}", file=sys.stderr)
            vols.append({"id": Path(p).stem, "file": Path(p).name, "set": "loaded" if members else "hold",
                         "error": f"{type(e).__name__}: {e}", "toc": {"found": False}, "entries": {"n": 0},
                         "body_start": {"agree": None}, "parts": {"equal": True}, "pages": 0, "viable": False})
    elapsed = round(time.time() - t0, 1)
    summary = summarize([v for v in vols if "error" not in v])
    summary["errors"] = [v["id"] for v in vols if "error" in v]
    summary["elapsed_s"] = elapsed
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                               "params": {"toc_window": args.toc_window, "viable_rate": VIABLE_RATE},
                               "volumes": vols, "summary": summary}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print_table([v for v in vols if "error" not in v])
    print_summary(summary, [v for v in vols if "error" not in v], args.worst)
    print(f"\n소요 {elapsed}s · 기록 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
