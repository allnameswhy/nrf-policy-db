"""extract·annotate 산출물 검증 스텝 — 스탬프 상태 분기 검사 + 통과 시 스탬프 기록.

PROJECT_NOTES §3의 "검증 스텝": frontmatter verified_extract/verified_annotate
(YYYY-MM-DD 사다리)의 유일한 기록 주체. .md 본문은 절대 수정하지 않는다 —
쓰는 것은 frontmatter 스탬프 줄뿐(--no-stamp로 억제).

상태 분기 (파일별 스탬프 상태 기계 — 검사 범위를 상태로 고른다):
  스탬프 없음        풀 검사 A~C(PDF 재스캔) → PASS 시 verified_extract 기록.
                     커버리지 완비면 이어서 D 판정 후 verified_annotate까지.
  verified_extract만 PDF 생략(스탬프 신뢰). A(.md 단독) + 커버리지·배치 확인 후
                     D 품질 판정 → 전건 PASS/WARN이면 verified_annotate 기록.
  둘 다              스킵. --reaudit면 D 재판정(FAIL 시 verified_annotate 회수).
  --full             스탬프 무시하고 A~C 전수 재검사. 매 실행 PDF 전수 대조라는
                     종전 안전망은 이 플래그로만 작동한다(드리프트·수동 편집 의심 시).

검사:
  A 구조 불변식    .md 단독 — frontmatter 필수 키·report_id·year·pdf_pages(PDF
                   재계산 대조는 풀 검사에서만), 헤딩 ID 형식·유일·서수 경로 연속,
                   정상 장 >= 2, 잔존 특수기호, GFM 표 파이프 정합
  B 손실 전수 대조 (풀 검사 전용) PDF를 extract와 동일 함수로 재스캔해 본문 구간
                   모든 줄의 fold_g가 .md(요약 블록 제거본)에 포함되는지 검사.
                   표 내부 줄은 전역 포함 → 페이지 국소 table_covers 순으로 판정
                   (find_tables 행렬 단계 소실 검출 — 실측 4권 21건 부류).
                   재스캔 표 markdown/caption 포함, 이미지 전용 구간 마커도 검사.
  C 목차 대조      (풀 검사 전용) 앞부속 목차(리더런 페이지)와 장 대조 — 제목
                   포함(경고), 장 번호 커버(문법 미감지·헤딩 오탐 검출, FAIL)
  D 품질 판정      (LLM — 유일한 비결정론 검사, annotate 호출 레이어 재사용)
                   유닛 요약을 자기 근거 본문과 1:1 대조. 용도(검색·발견) 기준:
                   핵심 주제·키워드 부재=FAIL(누락) / 본문에 없는 사실=FAIL(할루시
                   네이션) / 수치·주체·인과 상이=FAIL(왜곡) / 본문과 무관=FAIL(무관)
                   / 핵심 포함·부차 편중=WARN. 판정 입력은 annotate 생성 입력과
                   동일 절단(--input-cap). FAIL은 자동 수정하지 않고 리포트만 —
                   수정 경로: 요약 줄 삭제 → annotate 재실행 → verify 재실행.

플랫 문서(frontmatter 'structure: flat' — extract 플랫 폴백 산출물): 풀 검사에서
find_body_start/detect_structure 재현 대신 extract와 공유하는 find_body_start_flat로
body 범위를 재현해 B를 동일하게 실행한다. A는 합성 '구간 N (p.a-b)' 헤딩 전용
검사로 바뀌고(비구간 헤딩·하위 헤딩 = FAIL, 최소 장 수 2→1) C는 생략하되, L1 구조
신호(find_body_start 성공)가 있으면 경고한다(구조 문서가 폴백 또는 --lane flat 강제로
플랫 처리된 의심 — 레인 정합 안전망).
비NRF 슬러그 스템은 검사 대상에 포함되며 year 대조는 NRF 스템에서만 수행한다.

스탬프 기록(검사 아님 — 최종 단계): 통과 상태에 맞는 스탬프를 이중 가드(스탬프 외
무변경 바이트 대조 + 재파싱 값 일치)로 기록. verified_annotate = 커버리지·배치
완비 + D 전건 통과. 결정론 FAIL은 스탬프 전체 회수, D FAIL은 verified_annotate만
회수(추출 검증은 유효). 거짓 "검증됨"을 남기지 않는다.

한계(§8 문서화): 이미지 속 글자는 원천 미추출(마커로만 명시), 동일 문장이 문서
타처에 있으면 그 줄의 소실이 가려질 수 있음, pymupdf 버전 변경 시 표 인식 차이로
재현이 어긋날 수 있음(requirements 핀 전제), D는 LLM 판정이라 실행 간 비결정.

종료 코드: 0 = 전건 PASS(경고 허용), 1 = FAIL 존재, 2 = 사용 오류·한도/인증 중단.
extract/annotate가 실행 실패에 쓰는 비0과 달리 1은 "정상적으로 내린 불합격 판정"이다.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime
import json
import os
import re
import sys
from pathlib import Path

import pymupdf

import mdio
from extract import (
    CAPTION_RE,
    PROFILES,
    TEMPLATE_FOLD,
    derive_report_id_any,
    detect_structure,
    find_body_start,
    find_body_start_flat,
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

REPO_ROOT = Path(__file__).resolve().parent.parent

NRF_STEM_RE = re.compile(r"^(\d{4}-\d{2})(?:_\d{2})?$")  # NRF 표준 스템 — year 대조는 이 형식에서만
FLAT_HEADING_RE = re.compile(r"^구간 \d+ \(p\.\d+(?:-\d+)?\)$")  # extract 플랫 폴백의 합성 헤딩 제목
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
    mode: str = ""  # full | promote | reaudit | skip
    fails: list = dataclasses.field(default_factory=list)  # 결정론(A~C) 불합격
    qfails: list = dataclasses.field(default_factory=list)  # D 품질 판정 불합격
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
    """True면 치명(파트 드리프트) — PDF 대조를 생략한다.

    part=None이면 pdf_pages 재계산 대조를 생략한다(verified_extract 신뢰 경로 —
    PDF 없이는 합본 파트 수를 알 수 없으므로 .md 단독 검사만 남긴다).
    """
    missing = [k for k in REQUIRED_KEYS if k not in raw]
    if missing:
        rep.fails.append(f"frontmatter 필수 키 누락: {', '.join(missing)}")
    if st.fm.report_id != stem:
        rep.fails.append(f"report_id({st.fm.report_id}) ≠ 파일명({stem})")
    if NRF_STEM_RE.match(stem):
        if raw.get("year") != stem[:4]:
            rep.fails.append(f"year({raw.get('year')}) ≠ 파일명 연도({stem[:4]})")
    else:
        y = raw.get("year", "")
        if y and not re.fullmatch(r"\d{4}", y):
            rep.fails.append(f"year({y}) — 슬러그 rid는 4자리 연도 또는 공란이어야 함")
    sv = raw.get("structure")
    if sv is not None and sv != "flat":
        rep.fails.append(f"structure 키 값 위반: {sv} (허용: flat)")
    if not mdio._unquote(raw.get("title", "")):
        rep.warns.append("title 공란(결측 허용 규약 — 확인 권장)")
    if part is None:
        return False
    if n_parts > 1:
        expect = f'"{part[0] + 1}-{part[1] + 1}"'
        if raw.get("pdf_pages") != expect:
            rep.fails.append(f"pdf_pages({raw.get('pdf_pages')}) ≠ 재계산 {expect} — 파트 드리프트, PDF 대조 생략")
            return True
    elif "pdf_pages" in raw:
        rep.fails.append("단독본 .md에 pdf_pages 존재")
    return False


def check_headings(rep: Report, st, stem: str, flat: bool = False) -> list:
    """헤딩 ID 형식·유일·서수 경로 검사. 정상(비 참고문헌/부록) 장 목록 반환.

    flat 문서는 합성 '구간 N' 헤딩만 허용(하위 헤딩 불가) — 레인 위장 방지.
    최소 장 수도 2→1로 완화(400자 가드 통과 문서는 구간 1개일 수 있음)."""
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
    if flat:
        for r in st.roots:
            if not FLAT_HEADING_RE.match(r.text):
                rep.fails.append(f"플랫 문서에 비구간 헤딩: {r.text[:30]}")
            if r.children:
                rep.fails.append(f"플랫 문서에 하위 헤딩 존재: {r.hid}")
    normal = [r for r in st.roots if r.depth == 1 and not mdio.is_excluded_chapter(r.text)]
    floor = 1 if flat else 2
    if len(normal) < floor:
        rep.fails.append(f"정상 장 수 {len(normal)} < {floor} — extract 산출물이 아닐 가능성")
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
# 커버리지·스탬프 기록 (검사 아님 — 최종 단계)
# ---------------------------------------------------------------------------

def annotate_complete(st) -> bool:
    """status.py·annotate --scan과 동일 산식: 잔여 호출 0 + 요약 배치 규약 일치."""
    done = sum(1 for u in st.units if u.hid in st.existing)
    remaining = (len(st.units) - done) + (
        1 if st.fm.abstract_empty and mdio.REPORT_KEY not in st.existing and st.units else 0)
    return remaining == 0 and st.roundtrip_ok()


def apply_stamp(rep: Report, md_path: Path, original: list[str], extract_val: str, annotate_val: str) -> None:
    """스탬프를 주어진 최종 값으로 기록. 값의 결정은 호출부(main의 모드별 정책)."""
    new = mdio.set_verified_stamps(original, extract_val, annotate_val)
    label = "annotate" if annotate_val else ("extract" if extract_val else "제거")
    if new == original:
        if extract_val or annotate_val:
            rep.stamped = label  # 이미 원하는 상태 — 표시만
        return
    # 이중 가드: ① 스탬프 줄 외 무변경 ② 재파싱 값 일치 — 위반 시 쓰기 중단
    if mdio.strip_verified_stamps(new) != mdio.strip_verified_stamps(original):
        rep.fails.append("스탬프 가드 위반(스탬프 외 변경 감지) — 쓰기 중단")
        return
    fm = mdio.parse_frontmatter(new)
    if (fm.verified_extract, fm.verified_annotate) != (extract_val, annotate_val):
        rep.fails.append("스탬프 가드 위반(재파싱 불일치) — 쓰기 중단")
        return
    mdio.write_md_lines(md_path, new)
    rep.stamped = label


# ---------------------------------------------------------------------------
# D. 품질 판정 (LLM — annotate.py의 호출 레이어 재사용)
# ---------------------------------------------------------------------------

JUDGE_SYSTEM_PROMPT = (
    "너는 정책연구보고서 검색 시스템의 요약 감사자다. 요약의 용도는 검색·발견 — "
    "사용자가 요약만 보고 해당 절을 찾아갈 수 있어야 한다. 절 본문과 요약이 주어지면 "
    "다음 기준으로 판정한다. FAIL: 누락(본문의 핵심 주제·키워드가 요약에 없음) / "
    "할루시네이션(본문에 없는 사실 주장) / 왜곡(수치·주체·인과가 본문과 다름) / "
    "무관(요약이 본문 주제와 무관). WARN: 핵심은 담겼으나 부차 내용에 편중. "
    "그 외 PASS. 본문 내용의 압축·환언·표현 선택은 문제 삼지 않는다. "
    "숫자 표기의 단위 환산(예: 1,520,000 위안 = 152만 위안)은 산술적으로 같으면 "
    "왜곡이 아니다. 본문 안에서 서술문과 표 등 표기가 서로 모순될 때 요약이 그중 "
    "한쪽을 그대로 따랐다면 FAIL이 아니라 WARN으로 하고 note에 원문 모순임을 밝힌다. "
    "출력은 JSON 한 줄만, 다른 텍스트 금지: "
    '{"verdict":"PASS|WARN|FAIL","issues":[{"type":"누락|할루시네이션|왜곡|무관",'
    '"claim":"요약 속 문제 문구","note":"짧은 근거"}]} PASS면 issues는 빈 배열.'
)

VERDICTS = ("PASS", "WARN", "FAIL")


def _annotate():
    """LLM 레이어 lazy import — 결정론 경로(--no-llm 등)는 SDK 의존 없이 동작."""
    import annotate

    return annotate


def parse_verdict(raw: str) -> dict:
    """판정 응답에서 JSON verdict 추출. 형식 불량은 ValueError → 재시도 대상."""
    s = raw.strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        raise ValueError(f"판정 JSON 없음: {s[:80]!r}")
    try:
        obj = json.loads(s[i : j + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"판정 JSON 파싱 실패: {e}")
    v = obj.get("verdict")
    if v not in VERDICTS:
        raise ValueError(f"verdict 값 불량: {v!r}")
    issues = obj.get("issues")
    if not isinstance(issues, list):
        issues = []
    return {"verdict": v, "issues": issues}


def build_judge_unit_prompt(an, fm, unit, base: list[str], summary: str, cap: int) -> str:
    """판정 입력 = 생성 입력과 동일 절단 — 판정자 시야 밖 주장 = 진짜 할루시네이션."""
    text, _ = an.truncate_input(mdio.unit_text(unit, base), cap)
    path = " > ".join(unit.heading_path)
    note = (
        "\n주의: 아래 본문은 이 항목의 도입부(직속 본문)만이며 요약도 그 범위만 다룬다."
        if unit.kind == "intro"
        else ""
    )
    return (
        f"보고서: {fm.title}\n위치: {path}{note}\n\n[절 본문]\n{text}\n\n"
        f"[검사 대상 요약]\n{summary}\n\n위 요약을 판정 기준에 따라 JSON으로 판정하라."
    )


def build_judge_report_prompt(fm, pairs: list[tuple[str, str]], summary: str) -> str:
    """보고서 요약은 생성 때와 동일하게 유닛 요약 전체를 근거로 판정한다."""
    listing = "\n".join(f"- {p}: {s}" for p, s in pairs)
    return (
        f"보고서 제목: {fm.title}\n\n[절별 요약 전체 — 이 보고서 요약의 근거 입력]\n"
        f"{listing}\n\n[검사 대상 보고서 요약]\n{summary}\n\n"
        "위 보고서 요약을 판정 기준에 따라 JSON으로 판정하라."
    )


async def judge_call(an, prompt: str, args, jstats: dict) -> dict:
    """판정 1건 (재시도 포함). 3회 실패 시 ERROR verdict — 스탬프를 막되 실행은 계속."""
    last: Exception | None = None
    for attempt in range(3):
        try:
            raw, usage = await an.call_llm(
                prompt, model=args.judge_model, timeout=args.llm_timeout,
                system_prompt=JUDGE_SYSTEM_PROMPT,
            )
        except an.RetryableError as e:
            jstats["calls"] += 1
            last = e
        else:
            jstats["calls"] += 1
            jstats["cost"] += usage.get("cost") or 0.0
            try:
                return parse_verdict(raw)
            except ValueError as e:
                last = e
        if attempt < 2:
            await asyncio.sleep(an.RETRY_DELAYS[min(attempt, len(an.RETRY_DELAYS) - 1)])
    return {"verdict": "ERROR", "issues": [{"type": "판정불능", "claim": "", "note": str(last)[:120]}]}


async def judge_file(an, st, args, jstats: dict) -> dict[str, dict]:
    """파일 1개의 요약 전건 판정. 한도/인증 예외는 전파(전체 실행 중단)."""
    targets: list[tuple[str, str]] = [
        (u.hid, build_judge_unit_prompt(an, st.fm, u, st.base, st.existing[u.hid], args.input_cap))
        for u in st.units
    ]
    if st.fm.abstract_empty and mdio.REPORT_KEY in st.existing:
        pairs = [(" > ".join(u.heading_path), st.existing[u.hid]) for u in st.units]
        targets.append(
            (mdio.REPORT_KEY, build_judge_report_prompt(st.fm, pairs, st.existing[mdio.REPORT_KEY]))
        )

    sema = asyncio.Semaphore(args.concurrency)
    fatal: list[Exception] = []
    results: dict[str, dict] = {}

    async def one(hid: str, prompt: str) -> None:
        async with sema:
            if fatal:
                return
            try:
                results[hid] = await judge_call(an, prompt, args, jstats)
            except (an.UsageLimitReached, an.AuthError) as e:
                if not fatal:
                    fatal.append(e)

    await asyncio.gather(*(one(h, p) for h, p in targets))
    if fatal:
        raise fatal[0]
    return results


def summary_line_map(st) -> dict[str, int]:
    """{hid: 원본 파일의 요약 줄 번호(1-기준)} — FAIL 리포트의 수동 삭제 편의."""
    lines = st.original
    roots = mdio.parse_heading_tree(lines)
    fm = mdio.parse_frontmatter(lines)
    out: dict[str, int] = {}
    for h in mdio.iter_headings(roots):
        for j in range(h.line + 1, min(h.line + 3, len(lines))):
            if lines[j].startswith(mdio.UNIT_SUMMARY_PREFIX):
                out[h.hid] = j + 1
                break
            if lines[j] != "":
                break
    first_heading = min((h.line for h in roots), default=len(lines))
    for j in range(fm.end_line + 1, first_heading):
        if lines[j].startswith(mdio.REPORT_SUMMARY_PREFIX):
            out[mdio.REPORT_KEY] = j + 1
            break
    return out


def _verdict_desc(hid: str, v: dict, lmap: dict[str, int]) -> str:
    where = "보고서 요약" if hid == mdio.REPORT_KEY else hid
    line = lmap.get(hid)
    loc = f"(줄 {line})" if line else ""
    head = f"D {v['verdict']} {where} {loc}".rstrip()
    if not v["issues"]:
        return head
    parts = "; ".join(
        f"[{i.get('type', '?')}] {str(i.get('claim', ''))[:60]} — {str(i.get('note', ''))[:80]}"
        for i in v["issues"][:3]
    )
    return f"{head}: {parts}"


# ---------------------------------------------------------------------------
# 파트 단위 결정론 검사 (스탬프 기록은 main의 최종 단계)
# ---------------------------------------------------------------------------

def check_full(rep: Report, md_path: Path, scans, part, n_parts: int, stem: str, args):
    """풀 검사 A~C — PDF 재스캔 대조 포함. 반환: ReportState 또는 None(파싱 실패)."""
    try:
        st = mdio.load_report(md_path, min_chars=args.min_chars, max_chars=args.max_chars)
    except Exception as e:
        rep.fails.append(f"로더 파싱 실패: {e}")
        return None
    raw = fm_raw(st.base, st.fm.end_line)
    flat = raw.get("structure") == "flat"
    fatal = check_frontmatter(rep, raw, st, stem, part, n_parts)
    check_headings(rep, st, stem, flat=flat)
    check_body_text(rep, st.base)

    if not fatal:
        if flat:
            # 플랫 레인: 구조 재현 대신 extract 폴백과 동일한 body 범위 함수를 공유,
            # 핵심 안전망인 B 손실 전수 대조는 동일하게 실행한다(C 목차 대조는 생략).
            body_start = find_body_start_flat(scans, part)
            if body_start is None:
                rep.fails.append("플랫 본문 시작 재현 실패 — 드리프트 의심")
            else:
                mark_footnotes(scans, (body_start, part[1]))
                mark_colophon_pages(scans, part, [])
                md_fold = fold_g("\n".join(st.base))
                check_coverage(rep, scans, part, body_start, md_fold, args.misses_cap)
                check_tables_in_md(rep, scans, part, body_start, md_fold)
                check_image_markers(rep, scans, part, body_start, set(st.base))
                # 레인 정합 안전망: 플랫 body 시작은 표지에서 잡히는 게 보통이라
                # "body_start 앞 목차 페이지" 검사는 사문(실측 2025-02) — L1 신호 유무로 판별한다.
                sb_probe, _ = find_body_start(scans, part)
                if sb_probe is not None:
                    rep.warns.append("플랫 문서인데 L1 구조 신호 존재 — 구조 문서가 폴백/강제(--lane flat)로 플랫 처리됐을 가능성(--scan 확인)")
        else:
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
    return st


def check_md_only(rep: Report, md_path: Path, stem: str, args):
    """A 구조 불변식만(.md 단독) — verified_extract 신뢰 경로. PDF 불필요."""
    try:
        st = mdio.load_report(md_path, min_chars=args.min_chars, max_chars=args.max_chars)
    except Exception as e:
        rep.fails.append(f"로더 파싱 실패: {e}")
        return None
    raw = fm_raw(st.base, st.fm.end_line)
    check_frontmatter(rep, raw, st, stem, None, 1)
    check_headings(rep, st, stem, flat=raw.get("structure") == "flat")
    check_body_text(rep, st.base)
    return st


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="extract·annotate 산출물 검증 — 상태 분기 검사 + 스탬프 기록.")
    parser.add_argument("ids", nargs="*", help="검사할 report_id (기본: 전체; 2025-17은 파트 전체로 확장)")
    parser.add_argument("--reports-dir", default="reports", help="검사 대상 .md 디렉터리 (기본: reports)")
    parser.add_argument("--pdf-dir", default="pdfs", help="source_pdf 경로 실패 시 대체 탐색 디렉터리 (기본: pdfs)")
    parser.add_argument("--full", action="store_true", help="스탬프 무시하고 A~C 전수 재검사 (PDF 필요)")
    parser.add_argument("--reaudit", action="store_true", help="verified_annotate가 있어도 D 품질 판정 재실행")
    parser.add_argument("--no-llm", action="store_true", help="D 품질 판정 억제 — 결정론 검사만, verified_annotate 미기록")
    parser.add_argument("--no-stamp", action="store_true", help="검사만 하고 어떤 파일도 쓰지 않음")
    parser.add_argument("--judge-model", default="claude-haiku-4-5", help="판정 모델 (기본: claude-haiku-4-5)")
    parser.add_argument("--concurrency", type=int, default=3, help="동시 판정 호출 수 (기본: 3)")
    parser.add_argument("--llm-timeout", type=float, default=240, help="판정 호출당 타임아웃 초 (기본: 240)")
    parser.add_argument("--input-cap", type=int, default=8000, help="판정 입력 절단 상한 — annotate와 동일해야 함 (기본: 8000)")
    parser.add_argument("--log", default="logs/verify_log.json", help="결과 로그 경로")
    parser.add_argument("--misses-cap", type=int, default=20, help="소실 개별 표시 상한 (기본 20)")
    parser.add_argument("--min-chars", type=int, default=200, help="유닛 최소 크기 (annotate와 동일해야 함)")
    parser.add_argument("--max-chars", type=int, default=4000, help="유닛 최대 크기 (annotate와 동일해야 함)")
    parser.add_argument(
        "--token-file",
        default=str(REPO_ROOT / ".claude_oauth_token"),
        help="claude setup-token 발급 토큰 파일 (기본: 저장소 루트 .claude_oauth_token)",
    )
    args = parser.parse_args()

    reports_dir = Path(args.reports_dir)
    if not reports_dir.is_dir():
        print(f"디렉터리가 없습니다: {reports_dir}", file=sys.stderr)
        sys.exit(2)

    all_md = {p.stem: p for p in sorted(reports_dir.glob("*.md"))}  # 슬러그 스템 포함 전수
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

    # ---- 상태 분기 (스탬프 상태 기계 — 검사 범위를 상태로 고른다) ----
    reports: list[Report] = []
    states: dict[str, tuple[Report, Path, object]] = {}  # stem → (rep, md_path, ReportState|None)
    fulls: list[tuple[str, Path, Report, list[str], int]] = []
    for stem, mp in all_md.items():
        rep = Report(stem)
        reports.append(rep)
        try:
            lines = mdio.read_md_lines(mp)
            fm = mdio.parse_frontmatter(lines)
        except Exception as e:
            rep.mode = "full"
            rep.fails.append(f"파싱 실패: {e}")
            continue
        if args.full or not fm.verified_extract:
            rep.mode = "full"
            fulls.append((stem, mp, rep, lines, fm.end_line))
        elif not fm.verified_annotate or args.reaudit:
            rep.mode = "reaudit" if fm.verified_annotate else "promote"
            states[stem] = (rep, mp, check_md_only(rep, mp, stem, args))
        else:
            rep.mode = "skip"
            rep.notes.append("감사 완료 — 스킵 (--reaudit 재판정, --full 전수 재검사)")

    # ---- 풀 검사: PDF 그룹 재스캔 (필요한 그룹만 연다) ----
    groups: dict[str, list] = {}  # pdf 경로 → [(stem, md_path, Report)]
    for stem, mp, rep, lines, fm_end in fulls:
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
        base = derive_report_id_any(pdf_path)
        doc = pymupdf.open(pdf_path)
        try:
            scans = scan_document(doc)
        finally:
            doc.close()
        parts = split_bundle(scans)
        expected = {base: parts[0]} if len(parts) == 1 else \
                   {f"{base}_{i + 1:02d}": pt for i, pt in enumerate(parts)}
        for stem, mp, rep in sorted(members):
            if stem not in expected:
                rep.fails.append(
                    f"합본 분할 불일치 — PDF는 {len(parts)}파트({sorted(expected)}), "
                    f".md는 {stem} (재추출 필요)")
                continue
            states[stem] = (rep, mp, check_full(rep, mp, scans, expected[stem], len(parts), stem, args))

    # ---- D 품질 판정 대상 선정 ----
    cands: list[str] = []
    for stem, (rep, mp, st) in states.items():
        if st is None or rep.fails:
            continue
        covered = annotate_complete(st)
        rep.stats["coverage_complete"] = covered
        if not covered:
            if st.fm.verified_annotate:
                rep.warns.append("verified_annotate가 있으나 커버리지 미완(요약 삭제·드리프트?) — 스탬프 회수")
            else:
                rep.notes.append("annotate 미완 — D 판정 생략")
            continue
        if st.fm.verified_annotate and not args.reaudit:
            continue  # 기존 판정 신뢰 — 스탬프 보존 (--full 경로에서 도달)
        if args.no_llm:
            rep.notes.append("--no-llm: D 판정 생략 — verified_annotate 미기록")
            continue
        cands.append(stem)

    judged: dict[str, dict] = {}  # stem → {hid: verdict dict}
    jstats = {"calls": 0, "cost": 0.0}
    aborted = False
    if cands:
        an = _annotate()
        os.environ.setdefault("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
        an.setup_auth(args)
        for stem in cands:
            rep, mp, st = states[stem]
            if aborted:
                rep.notes.append("D 판정 미수행(한도/인증 중단) — 재실행하면 이어서 진행")
                continue
            n = len(st.units) + (1 if st.fm.abstract_empty and mdio.REPORT_KEY in st.existing else 0)
            print(f"[판정] {stem}: {n}건 ({args.judge_model})", file=sys.stderr)
            try:
                judged[stem] = asyncio.run(judge_file(an, st, args, jstats))
            except (an.UsageLimitReached, an.AuthError) as e:
                aborted = True
                print(an.fatal_msg(e), file=sys.stderr)
                rep.notes.append("D 판정 중단 — 재실행하면 이어서 진행")

    # ---- 판정 결과 반영 ----
    for stem, verdicts in judged.items():
        rep, mp, st = states[stem]
        lmap = summary_line_map(st)
        counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "ERROR": 0}
        for hid, v in sorted(verdicts.items()):
            counts[v["verdict"]] += 1
            if v["verdict"] in ("FAIL", "ERROR"):
                rep.qfails.append(_verdict_desc(hid, v, lmap))
            elif v["verdict"] == "WARN":
                rep.warns.append(_verdict_desc(hid, v, lmap))
        rep.stats["judge"] = counts

    # ---- 스탬프 기록 (검사 아님 — 최종 단계) ----
    today = datetime.date.today().isoformat()
    if args.no_stamp:
        for rep in reports:
            if rep.mode != "skip":
                rep.notes.append("--no-stamp: 스탬프 미기록")
    else:
        for stem, (rep, mp, st) in states.items():
            if st is None:
                continue
            if rep.fails:  # 결정론 불합격 — 스탬프 전체 회수 (거짓 "검증됨" 제거)
                extract_val = annotate_val = ""
            else:
                extract_val = today if rep.mode == "full" else st.fm.verified_extract
                if stem in judged:
                    annotate_val = today if not rep.qfails else ""  # D FAIL은 annotate만 회수
                elif st.fm.verified_annotate and rep.stats.get("coverage_complete"):
                    annotate_val = st.fm.verified_annotate  # 판정 미실행 — 기존 판정 보존
                else:
                    annotate_val = ""
            apply_stamp(rep, mp, st.original, extract_val, annotate_val)

    # ---- 리포트 ----
    print()
    print("== 검증 결과 ==")
    for rep in reports:
        status = "PASS" if not (rep.fails or rep.qfails) else "FAIL"
        tag = f" [스탬프: {rep.stamped}]" if rep.stamped else ""
        cov = ""
        if "needles" in rep.stats:
            cov = f"  (대조 {rep.stats['needles']}줄, 미스 {rep.stats['misses']})"
        jd = rep.stats.get("judge")
        if jd:
            cov += f"  [판정 PASS {jd['PASS']} / WARN {jd['WARN']} / FAIL {jd['FAIL'] + jd['ERROR']}]"
        print(f"{rep.report_id}: {status} [{rep.mode}]{tag}{cov}")
        for f_ in rep.fails + rep.qfails:
            print(f"  x {f_}")
        for w in rep.warns:
            print(f"  ! {w}")
        for nt in rep.notes:
            print(f"  - {nt}")
    n_fail = sum(1 for r in reports if r.fails or r.qfails)
    n_warn = sum(len(r.warns) for r in reports)
    print()
    print(f"== 합계 == PASS {len(reports) - n_fail} / FAIL {n_fail} / 경고 {n_warn}")
    if jstats["calls"]:
        print(f"[판정 합계] 호출 {jstats['calls']}회, 비용 ${jstats['cost']:.4f}")
    if aborted:
        print("[중단] 한도/인증으로 D 판정이 중단됨 — 재실행하면 미기록 파일만 이어서 판정", file=sys.stderr)

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps({
        "run_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "mode": "verify",
        "no_stamp": args.no_stamp,
        "flags": {"full": args.full, "reaudit": args.reaudit, "no_llm": args.no_llm,
                  "judge_model": args.judge_model},
        "judge_totals": jstats,
        "judgements": {stem: verdicts for stem, verdicts in judged.items()},
        "files": [dataclasses.asdict(r) for r in reports],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"로그: {log_path}")

    sys.exit(2 if aborted else (1 if n_fail else 0))


if __name__ == "__main__":
    main()
