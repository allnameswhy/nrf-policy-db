"""extract 산출물 검증 스텝 — PDF↔.md 전수 대조 후 통과 시 검증 스탬프 기록.

PROJECT_NOTES §4의 "검증 스텝": frontmatter verified_extract/verified_annotate
(YYYY-MM-DD 사다리)의 기록 주체. 판정은 전부 결정론(LLM 무관)이고, .md 본문은
절대 수정하지 않는다 — 쓰는 것은 frontmatter 스탬프 줄뿐(--no-stamp로 억제).

검사:
  A 구조 불변식    .md 단독 — frontmatter 필수 키·report_id·year·pdf_pages,
                   헤딩 ID 형식·유일·서수 경로 연속, 정상 장 >= 2,
                   잔존 특수기호(원시 불릿·NBSP·U+2012), GFM 표 파이프 정합
  B 손실 전수 대조 PDF를 extract와 동일 함수로 재스캔해 본문 구간 모든 줄의
                   fold_g가 .md(요약 블록 제거본)에 포함되는지 검사.
                   표 내부 줄은 전역 포함 → 페이지 국소 table_covers 순으로 판정
                   (find_tables 행렬 단계 소실 검출 — 실측 4권 21건 부류).
                   재스캔 표 markdown/caption의 .md 포함도 검사(.md측 훼손 검출).
  이미지 마커      이미지 전용 구간마다 손실 명시 마커 존재
  C 목차 대조      앞부속 목차(리더런 페이지)와 장 대조 — 제목 포함(경고),
                   장 번호 커버(문법 미감지·헤딩 오탐 검출, FAIL)
  D 스탬프         전 검사 통과 시 verified_extract 기록. 요약 커버리지 완비
                   (모든 유닛 요약 + 잔여 0 + 배치 규약 일치)면 verified_annotate도
                   — 규약·커버리지 검증이지 요약 품질 판정이 아니다.
                   FAIL 파일의 기존 스탬프는 제거(거짓 "검증됨" 방지).

한계(§8 문서화): 이미지 속 글자는 원천 미추출(마커로만 명시), 동일 문장이 문서
타처에 있으면 그 줄의 소실이 가려질 수 있음, pymupdf 버전 변경 시 표 인식 차이로
재현이 어긋날 수 있음(requirements 핀 전제 — 광범위 미스는 드리프트로 요약 보고).

종료 코드: 0 = 전건 PASS(경고 허용), 1 = FAIL 존재, 2 = 사용 오류.
extract/annotate가 실행 실패에 쓰는 비0과 달리 1은 "정상적으로 내린 불합격 판정"이다.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import re
import sys
from pathlib import Path

import pymupdf

import mdio
from extract import (
    CAPTION_RE,
    PROFILES,
    TEMPLATE_FOLD,
    derive_report_id,
    detect_structure,
    find_body_start,
    fold,
    fold_g,
    mark_colophon_pages,
    mark_footnotes,
    page_table_fold,
    profile_value,
    scan_document,
    split_bundle,
    table_covers,
)

MD_STEM_RE = re.compile(r"^(\d{4}-\d{2})(?:_\d{2})?$")
FM_KEY_RE = re.compile(r"^([a-z_]+):(.*)$")
RAW_BULLET_RE = re.compile(r"^\s*[□○ㅇ❍◉▸▪‣∙•Ÿ](\s|$)")
UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
CANON_RE = re.compile(r"[^0-9A-Za-z가-힣]")

REQUIRED_KEYS = ("report_id", "title", "title_en", "year", "lead_researcher",
                 "institution", "keywords_ko", "keywords_en", "abstract", "source_pdf")
# 목차 줄은 두줄형 표기가 한 줄로 합쳐져 있어 완화 패턴으로 장 번호를 수집한다
RELAXED_PROFILE = {"jang_split": "jang", "bare_digit_split": "arabic"}
PROFILE_PAT = {name: pat for name, pat, _ in PROFILES}
DRIFT_MISS_RATIO = 0.05  # 미스율이 이보다 크면 개별 소실이 아니라 버전 드리프트로 요약


def canon(s: str) -> str:
    """숫자·영문·한글만 남기는 정규화 — 목차의 점·리더런·괄호·개행을 무력화."""
    return CANON_RE.sub("", s or "")


@dataclasses.dataclass
class Report:
    report_id: str
    fails: list = dataclasses.field(default_factory=list)
    warns: list = dataclasses.field(default_factory=list)
    notes: list = dataclasses.field(default_factory=list)
    stats: dict = dataclasses.field(default_factory=dict)
    stamped: str = ""  # "" | extract | annotate | 제거


def fm_raw(lines: list[str], fm_end: int) -> dict[str, str]:
    """frontmatter 스칼라 키의 원문 값. abstract 블록 연속 줄은 들여쓰기라 제외된다."""
    out = {}
    for ln in lines[1:fm_end]:
        m = FM_KEY_RE.match(ln)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


# ---------------------------------------------------------------------------
# A. 구조 불변식 (.md 단독)
# ---------------------------------------------------------------------------

def check_frontmatter(rep: Report, raw: dict, st, stem: str, part, n_parts: int) -> bool:
    """True면 치명(파트 드리프트) — PDF 대조를 생략한다."""
    missing = [k for k in REQUIRED_KEYS if k not in raw]
    if missing:
        rep.fails.append(f"frontmatter 필수 키 누락: {', '.join(missing)}")
    if st.fm.report_id != stem:
        rep.fails.append(f"report_id({st.fm.report_id}) ≠ 파일명({stem})")
    if raw.get("year") != stem[:4]:
        rep.fails.append(f"year({raw.get('year')}) ≠ 파일명 연도({stem[:4]})")
    if not mdio._unquote(raw.get("title", "")):
        rep.warns.append("title 공란(결측 허용 규약 — 확인 권장)")
    if n_parts > 1:
        expect = f'"{part[0] + 1}-{part[1] + 1}"'
        if raw.get("pdf_pages") != expect:
            rep.fails.append(f"pdf_pages({raw.get('pdf_pages')}) ≠ 재계산 {expect} — 파트 드리프트, PDF 대조 생략")
            return True
    elif "pdf_pages" in raw:
        rep.fails.append("단독본 .md에 pdf_pages 존재")
    return False


def check_headings(rep: Report, st, stem: str) -> list:
    """헤딩 ID 형식·유일·서수 경로 검사. 정상(비 참고문헌/부록) 장 목록 반환."""
    hid_re = re.compile(rf"^{re.escape(stem)}_c\d+(?:s\d+)*$")
    seen = set()

    def walk(h, parent, idx):
        expect = f"{stem}_c{idx}" if parent is None else f"{parent.hid}s{idx}"
        if not hid_re.match(h.hid):
            rep.fails.append(f"헤딩 ID 형식 위반: {h.hid}")
        if h.hid in seen:
            rep.fails.append(f"헤딩 ID 중복: {h.hid}")
        seen.add(h.hid)
        if h.hid != expect:
            rep.fails.append(f"헤딩 ID 서수 불일치: {h.hid} (기대 {expect}, '{h.text[:20]}')")
        for j, c in enumerate(h.children, 1):
            walk(c, h, j)

    for i, r in enumerate(st.roots, 1):
        if r.depth != 1:
            rep.fails.append(f"최상위 헤딩 depth≠1: {r.hid}")
        walk(r, None, i)
    normal = [r for r in st.roots if r.depth == 1 and not mdio.is_excluded_chapter(r.text)]
    if len(normal) < 2:
        rep.fails.append(f"정상 장 수 {len(normal)} < 2 — extract 산출물이 아닐 가능성")
    return normal


def check_body_text(rep: Report, base: list[str]) -> None:
    """잔존 특수기호(원시 불릿·NBSP·U+2012)와 GFM 표 파이프 정합."""
    fm_end = mdio.parse_frontmatter(base).end_line
    n = len(base)
    j = fm_end + 1
    while j < n:
        ln = base[j]
        if " " in ln:
            rep.fails.append(f"NBSP 잔존 (줄 {j + 1})")
        if "‒" in ln:
            rep.fails.append(f"U+2012 잔존 (줄 {j + 1})")
        if ln.startswith("|"):
            k = j
            while k < n and base[k].startswith("|"):
                if " " in base[k]:
                    rep.fails.append(f"NBSP 잔존 (줄 {k + 1})")
                k += 1
            if k - j < 2:
                rep.fails.append(f"고아 표 줄 (줄 {j + 1})")
            elif len({len(UNESCAPED_PIPE.findall(base[x])) for x in range(j, k)}) > 1:
                rep.fails.append(f"GFM 표 파이프 수 불일치 (줄 {j + 1}~{k})")
            j = k
            continue
        if not ln.startswith("<!--") and RAW_BULLET_RE.match(ln):
            rep.fails.append(f"원시 불릿 글리프 잔존 (줄 {j + 1}): {ln.strip()[:30]}")
        j += 1


# ---------------------------------------------------------------------------
# B. 손실 전수 대조 (PDF 재스캔 대조)
# ---------------------------------------------------------------------------

def check_coverage(rep: Report, scans, part, body_start, md_fold: str, cap: int) -> None:
    """본문 구간 모든 줄의 fold_g ⊆ .md 검사. 제외 규칙은 build_body의 삭제 규칙과 1:1."""
    needles, misses = 0, []
    for scan in scans[body_start:part[1] + 1]:
        if scan.blank or scan.dropped or scan.image_only:
            continue
        tf = None  # 페이지 국소 표 haystack (지연 계산)
        for ln in scan.lines:
            text = ln.text.strip()
            if not text or ln.is_leader:
                continue
            folded = fold(text)
            if any(folded.startswith(t) or t in folded for t in TEMPLATE_FOLD):
                continue
            ng = fold_g(text)
            if not ng:
                continue
            needles += 1
            if ng in md_fold:
                continue
            if ln.in_table:
                if tf is None:
                    tf = page_table_fold(scan.tables)
                if table_covers(text, tf):
                    continue
            misses.append((scan.index + 1, "표 내부" if ln.in_table else "본문", text))
    rep.stats["needles"] = needles
    rep.stats["misses"] = len(misses)
    if not misses:
        return
    rep.stats["miss_lines"] = [f"p.{pg} [{kind}] {text}" for pg, kind, text in misses[:200]]
    if needles and len(misses) / needles > DRIFT_MISS_RATIO:
        rep.fails.append(f"소실 의심 {len(misses)}/{needles}줄 — 광범위 불일치는 재추출/버전 드리프트 신호")
        return
    for pg, kind, text in misses[:cap]:
        rep.fails.append(f"소실({kind}) p.{pg}: {text[:60]}")
    if len(misses) > cap:
        rep.fails.append(f"… 소실 {len(misses) - cap}건 더 (로그 참조)")


def check_tables_in_md(rep: Report, scans, part, body_start, md_fold: str) -> None:
    """재스캔 표 블록이 .md에 그대로 있는가 — .md측 표 훼손·드리프트 검출."""
    for scan in scans[body_start:part[1] + 1]:
        if scan.blank or scan.dropped or scan.image_only:
            continue
        for tab in scan.tables:
            if tab.markdown is None:
                continue
            if fold_g(tab.markdown) not in md_fold:
                rep.fails.append(f"표 블록 훼손/누락 p.{scan.index + 1} — 재스캔 표가 .md에 없음")
            elif tab.caption and fold_g(tab.caption) not in md_fold:
                rep.fails.append(f"표 캡션 누락 p.{scan.index + 1}: {tab.caption[:40]}")


def check_image_markers(rep: Report, scans, part, body_start, md_line_set: set) -> None:
    """이미지 전용 연속 구간마다 손실 명시 마커 존재 — build_body의 방출 규칙과 동일."""
    img = {s.index for s in scans[body_start:part[1] + 1] if s.image_only}
    ranges = []
    for scan in scans[body_start:part[1] + 1]:
        pg = scan.index
        if not scan.image_only or scan.dropped or (pg - 1) in img:
            continue
        end = pg
        while end + 1 in img:
            end += 1
        rng = f"p.{pg + 1}" if end == pg else f"p.{pg + 1}-{end + 1}"
        ranges.append(rng)
        marker = f"<!-- {rng}: 이미지 전용 페이지, 텍스트 추출 불가 -->"
        if marker not in md_line_set:
            rep.fails.append(f"이미지 마커 누락: {marker}")
    if ranges:
        rep.stats["image_ranges"] = ranges


# ---------------------------------------------------------------------------
# C. 목차 대조 (신규 문법 미감지·헤딩 오탐 안전망)
# ---------------------------------------------------------------------------

BARE_MD_RE = re.compile(r"^(\d{1,2})\s+\S")  # bare_digit .md 헤딩 텍스트("1 제목")용


def _relaxed_pats(profile: str) -> list[tuple[str, re.Pattern]]:
    """목차 줄용 완화 패턴 목록. 로마자는 유니코드/ASCII 표기가 본문과 어긋나는
    실측(2025-29: 목차 Ⅰ. vs 본문 II.)이 있어 두 형태 모두 수집한다."""
    r = RELAXED_PROFILE.get(profile, profile)
    if r in ("roman_unicode", "roman_ascii"):
        return [("roman_unicode", PROFILE_PAT["roman_unicode"]),
                ("roman_ascii", PROFILE_PAT["roman_ascii"])]
    return [(r, PROFILE_PAT[r])]


def _title_variants(text: str) -> list[str]:
    """장 제목의 canon 변형들 — 원문 그대로 + 선두 번호 토큰 제거형.

    목차와 본문의 번호 표기가 다르면(Ⅱ↔II) canon에 남는 번호 문자가 어긋나므로
    토큰 제거형으로도 대조한다. 4자 미만 변형은 우연 일치가 흔해 버린다.
    """
    vs = {canon(text)}
    for pat in PROFILE_PAT.values():
        m = pat.match(text)
        if m:
            vs.add(canon(text[m.end() - 1:]))
    m = BARE_MD_RE.match(text)
    if m:
        vs.add(canon(text[m.end() - 1:]))
    return [v for v in vs if len(v) >= 4]


def check_toc(rep: Report, scans, part, body_start, profile: str | None, roots) -> None:
    toc_scans = [scans[i] for i in range(part[0], body_start) if scans[i].leader_lines >= 3]
    if not toc_scans:
        rep.notes.append("목차 페이지 미발견 — 목차 대조 생략")
        return
    depth1 = [h for h in roots if h.depth == 1]
    # (a) 제목 방향: 목차 전문 canon에 장 제목(변형 포함)이 부분열로 포함되는가 (경고).
    #     참고문헌·부록은 목차 미표기가 관행이라 제외(2025-13·25 실측).
    toc_canon = canon(" ".join(ln.text for s in toc_scans for ln in s.lines))
    unmatched = []
    for h in depth1:
        if mdio.is_excluded_chapter(h.text):
            continue
        vs = _title_variants(h.text)
        if vs and not any(v in toc_canon for v in vs):
            unmatched.append(h.text)
    if unmatched:
        rep.warns.append(f"목차에서 못 찾은 장 제목 {len(unmatched)}/{len(depth1)}(표기 차이?): "
                         + "; ".join(t[:24] for t in unmatched[:3]))
    if profile is None:
        rep.notes.append("프로파일 미확정 — 목차 번호 대조 생략")
        return
    # (b) 번호 방향: .md 헤딩 표면 번호 집합 ↔ 목차 번호 집합 대칭 비교
    pats = _relaxed_pats(profile)
    toc_values: set[int] = set()
    for s in toc_scans:
        for ln in s.lines:
            t = ln.text.strip()
            if CAPTION_RE.match(t):
                continue  # 표·그림 목차 항목
            for name, pat in pats:
                m = pat.match(t)
                if m:
                    try:
                        toc_values.add(profile_value(name, m))
                    except (ValueError, IndexError):
                        pass
                    break
    md_pats = pats + ([("bare", BARE_MD_RE)] if profile == "bare_digit_split" else [])
    md_num: dict[int, object] = {}  # 표면 번호 → 헤딩 (번호 달린 참고문헌 장도 포함)
    for h in depth1:
        for name, pat in md_pats:
            m = pat.match(h.text)
            if m:
                try:
                    v = int(m.group(1)) if name == "bare" else profile_value(name, m)
                except (ValueError, IndexError):
                    continue
                md_num.setdefault(v, h)
                break
    rep.stats["toc_values"] = sorted(toc_values)
    rep.stats["md_chapter_values"] = sorted(md_num)
    if not toc_values:
        rep.notes.append("목차에서 장 번호 미수집 — 번호 대조 생략")
        return
    if not md_num:
        rep.notes.append("헤딩에서 장 번호 미추출 — 번호 대조 생략")
        return
    missing = sorted(v for v, h in md_num.items()
                     if v not in toc_values and not mdio.is_excluded_chapter(h.text))
    over = sorted(v for v in toc_values if v not in md_num)
    if missing:
        rep.fails.append(f"목차에서 확인 안 되는 장 번호 {missing} — 헤딩 오탐 의심")
    if over:
        msg = f"목차에만 있는 장 번호 {over} — extract가 못 본 장(문법 미감지) 의심"
        if _relaxed_pats(profile)[0][0] == "arabic":
            # 아라비아 목차는 하위 항목 번호("1. …")와 장 번호가 구분 불가 — 육안 확인 유도
            rep.warns.append(msg + " (하위 항목 번호 혼입 가능 — --scan 육안 확인)")
        else:
            rep.fails.append(msg)


# ---------------------------------------------------------------------------
# D. 스탬프
# ---------------------------------------------------------------------------

def annotate_complete(st) -> bool:
    """status.py·annotate --scan과 동일 산식: 잔여 호출 0 + 요약 배치 규약 일치."""
    done = sum(1 for u in st.units if u.hid in st.existing)
    remaining = (len(st.units) - done) + (
        1 if st.fm.abstract_empty and mdio.REPORT_KEY not in st.existing and st.units else 0)
    return remaining == 0 and st.roundtrip_ok()


def apply_stamp(rep: Report, md_path: Path, original: list[str], passed: bool, annotate_ok: bool) -> None:
    today = datetime.date.today().isoformat()
    if passed:
        new = mdio.set_verified_stamps(original, today, today if annotate_ok else "")
    else:
        new = mdio.set_verified_stamps(original, "")  # 거짓 "검증됨" 제거
    if new == original:
        if passed:
            rep.stamped = "annotate" if annotate_ok else "extract"
        return
    # 이중 가드: ① 스탬프 줄 외 무변경 ② 재파싱 값 일치 — 위반 시 쓰기 중단
    if mdio.strip_verified_stamps(new) != mdio.strip_verified_stamps(original):
        rep.fails.append("스탬프 가드 위반(스탬프 외 변경 감지) — 쓰기 중단")
        return
    fm = mdio.parse_frontmatter(new)
    want = (today if passed else "", today if passed and annotate_ok else "")
    if (fm.verified_extract, fm.verified_annotate) != want:
        rep.fails.append("스탬프 가드 위반(재파싱 불일치) — 쓰기 중단")
        return
    mdio.write_md_lines(md_path, new)
    rep.stamped = ("annotate" if annotate_ok else "extract") if passed else "제거"


# ---------------------------------------------------------------------------
# 파트 단위 검사 오케스트레이션
# ---------------------------------------------------------------------------

def check_part(rep: Report, md_path: Path, scans, part, n_parts: int, stem: str, args) -> None:
    try:
        st = mdio.load_report(md_path, min_chars=args.min_chars, max_chars=args.max_chars)
    except Exception as e:
        rep.fails.append(f"로더 파싱 실패: {e}")
        return
    raw = fm_raw(st.base, st.fm.end_line)
    fatal = check_frontmatter(rep, raw, st, stem, part, n_parts)
    check_headings(rep, st, stem)
    check_body_text(rep, st.base)

    if not fatal:
        body_start, cands = find_body_start(scans, part)
        if body_start is None:
            rep.fails.append("본문 시작 페이지 재현 실패 — 드리프트 의심")
        else:
            mark_footnotes(scans, (body_start, part[1]))
            mark_colophon_pages(scans, part, [])
            structure = detect_structure(scans, part, body_start, cands, stem)
            if structure is None:
                rep.fails.append("L1 구조 재현 실패 — 드리프트 의심")
            md_fold = fold_g("\n".join(st.base))
            check_coverage(rep, scans, part, body_start, md_fold, args.misses_cap)
            check_tables_in_md(rep, scans, part, body_start, md_fold)
            check_image_markers(rep, scans, part, body_start, set(st.base))
            check_toc(rep, scans, part, body_start,
                      structure["profile"] if structure else None, st.roots)

    if args.no_stamp:
        rep.notes.append("--no-stamp: 스탬프 미기록")
        return
    apply_stamp(rep, md_path, st.original, passed=not rep.fails, annotate_ok=annotate_complete(st))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="extract 산출물 검증 — PDF↔.md 전수 대조 + 검증 스탬프 기록.")
    parser.add_argument("ids", nargs="*", help="검사할 report_id (기본: 전체; 2025-17은 파트 전체로 확장)")
    parser.add_argument("--reports-dir", default="reports", help="검사 대상 .md 디렉터리 (기본: reports)")
    parser.add_argument("--pdf-dir", default="pdfs", help="source_pdf 경로 실패 시 대체 탐색 디렉터리 (기본: pdfs)")
    parser.add_argument("--no-stamp", action="store_true", help="검사만 하고 어떤 파일도 쓰지 않음")
    parser.add_argument("--log", default="logs/verify_log.json", help="결과 로그 경로")
    parser.add_argument("--misses-cap", type=int, default=20, help="소실 개별 표시 상한 (기본 20)")
    parser.add_argument("--min-chars", type=int, default=200, help="유닛 최소 크기 (annotate와 동일해야 함)")
    parser.add_argument("--max-chars", type=int, default=4000, help="유닛 최대 크기 (annotate와 동일해야 함)")
    args = parser.parse_args()

    reports_dir = Path(args.reports_dir)
    if not reports_dir.is_dir():
        print(f"디렉터리가 없습니다: {reports_dir}", file=sys.stderr)
        sys.exit(2)

    all_md = {p.stem: p for p in sorted(reports_dir.glob("*.md")) if MD_STEM_RE.match(p.stem)}
    if args.ids:
        sel = {}
        for rid in args.ids:
            hits = {s: p for s, p in all_md.items() if s == rid or s.startswith(rid + "_")}
            if not hits:
                print(f"해당 .md 없음: {rid}", file=sys.stderr)
                sys.exit(2)
            sel.update(hits)
        all_md = dict(sorted(sel.items()))
    if not all_md:
        print("검사할 .md가 없습니다", file=sys.stderr)
        sys.exit(2)

    reports: list[Report] = []
    groups: dict[str, list] = {}  # pdf 경로 → [(stem, md_path, Report)]
    for stem, mp in all_md.items():
        rep = Report(stem)
        reports.append(rep)
        try:
            lines = mdio.read_md_lines(mp)
            fm_end = mdio.parse_frontmatter(lines).end_line
        except Exception as e:
            rep.fails.append(f"파싱 실패: {e}")
            continue
        src = mdio._unquote(fm_raw(lines, fm_end).get("source_pdf", ""))
        if not src:
            rep.fails.append("frontmatter source_pdf 공란 — PDF 대조 불가")
            continue
        pdfp = Path(src)
        if not pdfp.is_file():
            alt = Path(args.pdf_dir) / pdfp.name
            if alt.is_file():
                pdfp = alt
            else:
                rep.fails.append(f"원본 PDF 없음: {src}")
                continue
        groups.setdefault(str(pdfp), []).append((stem, mp, rep))

    for pdf_path, members in sorted(groups.items()):
        print(f"[스캔] {Path(pdf_path).name}", file=sys.stderr)
        base = derive_report_id(pdf_path)
        doc = pymupdf.open(pdf_path)
        try:
            scans = scan_document(doc)
        finally:
            doc.close()
        parts = split_bundle(scans)
        expected = {base: parts[0]} if len(parts) == 1 else \
                   {f"{base}_{i + 1:02d}": pt for i, pt in enumerate(parts)}
        for stem, mp, rep in sorted(members):
            if base is None or stem not in expected:
                rep.fails.append(
                    f"합본 분할 불일치 — PDF는 {len(parts)}파트({sorted(expected)}), "
                    f".md는 {stem} (재추출 필요)")
                continue
            check_part(rep, mp, scans, expected[stem], len(parts), stem, args)

    # ---- 리포트 ----
    print()
    print("== 검증 결과 ==")
    for rep in reports:
        status = "PASS" if not rep.fails else "FAIL"
        tag = f" [스탬프: {rep.stamped}]" if rep.stamped else ""
        cov = ""
        if "needles" in rep.stats:
            cov = f"  (대조 {rep.stats['needles']}줄, 미스 {rep.stats['misses']})"
        print(f"{rep.report_id}: {status}{tag}{cov}")
        for f_ in rep.fails:
            print(f"  x {f_}")
        for w in rep.warns:
            print(f"  ! {w}")
        for nt in rep.notes:
            print(f"  - {nt}")
    n_fail = sum(1 for r in reports if r.fails)
    n_warn = sum(len(r.warns) for r in reports)
    print()
    print(f"== 합계 == PASS {len(reports) - n_fail} / FAIL {n_fail} / 경고 {n_warn}")

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps({
        "run_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "mode": "verify",
        "no_stamp": args.no_stamp,
        "files": [dataclasses.asdict(r) for r in reports],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"로그: {log_path}")

    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
