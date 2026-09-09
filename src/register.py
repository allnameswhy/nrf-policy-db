"""관리번호 등록부 생성 — 파이프라인 1단계 (LLM 0).

PDF 표지에 인쇄된 관리번호(`정책연구 YYYY-NN`)를 읽어 `report_ids.tsv`에 기입한다.
이후 모든 단계(extract·verify·promote·status·serve)는 이 표만 읽는다(`src/registry.py`) —
표에 없거나 rid가 공란인 행이 하나라도 있는 PDF는 어느 단계도 지나지 못하므로, 표지에서 못
읽은 문서는 이 도구가 출력하는 「사람이 고칠 것」 목록(또는 /admin 가족 카드)을 보고 사람이
기입한다(`--set` / /admin 입력칸 — 표 파일 손편집 금지).

rid 부여 규칙(v4, 2026-09-08 사용자 결정 — 파일명 번호는 rid에 쓰지 않고 힌트로만):
 0) 합본 분할: 경량 전체 스캔(extract.scan_document(tables=False))으로 detect_parts를 돌려
    파트마다 행(pages 열)을 만든다 — 등록 단위 = 문서, 파트도 각자 rid(`_NN` 접미 폐지).
 1) 기본 번호: 파트(또는 파일) 표지 3단 탐색 ① 1쪽 → ② 1~6쪽 → ③ 1~6쪽+끝 2쪽 — 각 단에서
    같은 줄에 정책연구/연구보고/관리번호/NRF 문맥이 있는 `YYYY-NN`이 정확히 하나면 채택
    (source=cover), 여러 개면 그 단에서 멈추고 공란(후보를 note에), 하나도 없으면 다음 단.
 2) 권수 접미: 표지·속표지(1~2쪽)에 `제N권`이 정확히 하나면 `-vN`을 잠정 부여.
 3) 가족 검사: 같은 `YYYY-NN`을 가진 행(기존 행 + 같은 실행의 다른 새 행·같은 파일의 다른
    파트; rid 가족 또는 note의 표지 번호)이 **하나라도 있으면 공란**(사람이 결정 — 어느 쪽이
    본편이고 무엇이 -b·-vN인지는 문서 성격이라 자동으로 정하지 않는다). 가족이 없으면 확정.
    같은 실행의 새 행끼리 충돌하면 전부 공란(SAME_RUN_FIRST_KEEPS). 기존 행은 불변.
 4) 표준형 `YYYY-NN[-b][-vN]` — 글자 = 같은 번호의 다른 독립 보고서, v = 그 보고서의 권·부록.
note는 고정 세그먼트(registry.parse_note가 되읽음): 표지 번호 · 권 신호 · 표지 단서 · 합본
파트 · 충돌 · 파일명 번호 불일치 · 다른 후보 · 힌트. registered = rid를 적은 날.

기입·수정: `--set "파일명일부[#N]=RID"`(반복 가능, 합본은 #파트번호) → set_family — 형식·중복
검사 후 기록. 이미 추출된 문서의 rid를 바꾸면 reports/{old}.md 개명 + 헤딩 ID·판정 캐시
치환까지 수행(요약·스탬프 불변; 이후 build_db·build_index 재동기화). `--drop 파일명일부`는 그
파일의 행 전부 삭제(PDF 삭제·개명·교체 뒤 재등록용). 표에 있는데 pdfs/에 없는 파일은 행을
지우지 않고 "행 삭제 필요"로 출력한다.

사용: python src/register.py ["pdfs/<파일>.pdf" ...]   # 인자 없으면 pdfs/*.pdf 전부
      --check   쓰기 없이 표↔pdfs 정합 검사만(공란·파일 없음·미수록·형식 위반·중복)
      --dry-run 추가될 행만 출력
종료 코드: 0 = 사람이 고칠 것 없음, 1 = 기입·정리 필요, 2 = 사용 오류.
실측(2026-09-04 v3, 278권): 자동 확정 258 · 기입 필요 20 · `-vN` 4(2018-49) · 번호 중복 2쌍.
"""

from __future__ import annotations

import argparse
import copy
import datetime
import glob as globmod
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

import registry
from extract import detect_parts, scan_document
from registry import (COLUMNS, NOTE_SEP, REGISTRY_PATH, RID_RE, SOURCES, STEM_RE, Row, family_key,
                      family_of, format_pages, norm_name, parse_note, parse_pages, read_rows,
                      rid_sort_key)

NUM_RE = re.compile(r"((?:19|20)\d{2})\s*[-–]\s*(\d{1,3})(?!\d)")
CTX_RE = re.compile(r"정책\s*연구|연구\s*보고|관리\s*번호|NRF")
VOL_RE = re.compile(r"제\s*(\d{1,2})\s*권(?![가-힣])")
# 파일명 번호 — rid에 쓰지 않고 힌트·불일치 note에만 (종전 extract의 파일명 레인 정규식)
FILENAME_NUM_RE = re.compile(r"정책연구-(\d{4}-\d{2})|^\s*\((\d{4})-(\d{1,2})\)")
FRONT_PAGES = 6
VOL_PAGES = 2
BACK_PAGES = 2
LETTERS = "bcdefghijklmnopqrstuvwxyz"
REVIEW_MARKS = ("다른 후보", "와 다름(표지 채택)")  # 검토 목록 대상 note 표지 — 자동 채택이 다른 신호를 제친 경우
# 표지 1~2쪽 단서 단어(긴 것 우선 — 포함 관계면 긴 쪽만): 부속 문서 판단 재료(카드·note)
COVER_CUES = ("성과백서", "가이드북", "요약본", "사례집", "자료집", "매뉴얼", "로드맵",
              "별권", "별책", "부록", "첨부", "백서")
TITLE_STOP = {"연구", "보고서", "최종", "최종보고서", "방안", "위한", "대한", "분석", "개발", "정책",
              "정책연구", "사업", "지원", "체계", "구축", "마련", "전략", "기획", "개선", "관한", "통한"}
LEGACY_REGISTERED_DATE = "2026-09-04"  # v4 이전(registered 공란) 행의 등록일 — 전량 그날 기입
SAME_RUN_FIRST_KEEPS = False  # True = 같은 실행 충돌 시 파일명 정렬 첫 행은 유지(사용자 결정: 전부 공란)
UNKNOWN_RE = re.compile(r"^(\d{4})-00$")
KEY_PART_RE = re.compile(r"^(.*?)#(\d{1,2})$")


@dataclass
class CoverScan:
    n_pages: int = 0  # 스캔 범위(파트) 안 쪽 수
    by_page: dict[int, list[str]] = field(default_factory=dict)  # 1-based 상대 쪽 → 문맥 있는 후보
    vols: list[int] = field(default_factory=list)
    cues: list[str] = field(default_factory=list)
    title: str = ""
    text_chars: int = 0


def cover_title(page) -> str:
    """1쪽 최대 글꼴 span 텍스트(4자 이상) — 사람 검토·중복 대조용."""
    best_size, best = 0.0, ""
    try:
        blocks = page.get_text("dict")["blocks"]
    except Exception:
        return ""
    for b in blocks:
        for line in b.get("lines", []):
            for sp in line.get("spans", []):
                t = " ".join(sp.get("text", "").split())
                if len(t) >= 4 and sp.get("size", 0) > best_size:
                    best_size, best = sp["size"], t
    return best


def scan_pages(n: int) -> list[int]:
    front = list(range(1, min(FRONT_PAGES, n) + 1))
    back = [p for p in range(max(1, n - BACK_PAGES + 1), n + 1) if p not in front]
    return front + back


def scan_cover_doc(doc, lo: int, hi: int) -> CoverScan:
    """열린 문서의 [lo, hi](0-based) 범위를 한 문서로 보고 표지 신호를 읽는다."""
    n = hi - lo + 1
    sc = CoverScan(n_pages=max(n, 0))
    for pno in scan_pages(sc.n_pages):
        text = unicodedata.normalize("NFC", doc[lo + pno - 1].get_text())
        sc.text_chars += len(text.strip())
        found: set[str] = set()
        for line in text.splitlines():
            if not CTX_RE.search(line):
                continue
            for m in NUM_RE.finditer(line):
                found.add(f"{m.group(1)}-{int(m.group(2)):02d}")
        if found:
            sc.by_page[pno] = sorted(found)
        if pno <= VOL_PAGES:
            for m in VOL_RE.finditer(text):
                v = int(m.group(1))
                if v not in sc.vols:
                    sc.vols.append(v)
            for cue in COVER_CUES:
                if cue in text and cue not in sc.cues and not any(cue in c for c in sc.cues):
                    sc.cues.append(cue)
    if sc.n_pages:
        sc.title = cover_title(doc[lo])
    return sc


def scan_cover(pdf_path) -> CoverScan:
    doc = pymupdf.open(str(pdf_path))
    try:
        return scan_cover_doc(doc, 0, len(doc) - 1)
    finally:
        doc.close()


PartScans = list[tuple[CoverScan, "tuple[int, int] | None"]]


def scan_pdf(pdf_path) -> PartScans:
    """경량 전체 스캔 → 합본 파트 경계 → 파트별 표지 스캔. 단독본은 [(표지, None)]."""
    doc = pymupdf.open(str(pdf_path))
    try:
        n = len(doc)
        parts = detect_parts(scan_document(doc, tables=False)) if n else [(0, -1)]
        if len(parts) <= 1:
            return [(scan_cover_doc(doc, 0, n - 1), None)]
        return [(scan_cover_doc(doc, lo, hi), (lo, hi)) for lo, hi in parts]
    finally:
        doc.close()


def tiers(n: int) -> list[tuple[str, list[int]]]:
    front = list(range(1, min(FRONT_PAGES, n) + 1))
    return [("1쪽", front[:1]), (f"1~{len(front)}쪽", front), ("뒷표지 포함", scan_pages(n))]


def pick_base(sc: CoverScan) -> tuple[str | None, str, list[str]]:
    """3단 탐색 → (rid 또는 None, 사유/단 이름, 채택 외 후보)."""
    for label, pages in tiers(sc.n_pages):
        cands = sorted({c for p in pages for c in sc.by_page.get(p, [])})
        if len(cands) == 1:
            others = sorted({f"{c}(p{p})" for p, cs in sc.by_page.items()
                             for c in cs if c != cands[0]})
            return cands[0], label, others
        if len(cands) > 1:
            return None, f"후보 여러 개({label}: {', '.join(cands)})", cands
    if sc.text_chars == 0:
        return None, "표지 이미지(텍스트 없음)", []
    return None, "표지 번호 없음", []


def filename_number(name: str) -> str | None:
    m = FILENAME_NUM_RE.search(Path(name).stem)
    if not m:
        return None
    return m.group(1) if m.group(1) else f"{m.group(2)}-{int(m.group(3)):02d}"


def fold_title(s: str) -> str:
    return unicodedata.normalize("NFC", "".join(s.split())).lower()


def cover_segments(sc: CoverScan, base: str | None, reason: str) -> list[str]:
    segs = [f"표지 번호 {base}" if base else reason]
    if len(sc.vols) == 1:
        segs.append(f"권 신호 제{sc.vols[0]}권")
    elif len(sc.vols) > 1:
        segs.append("권 신호 여러 개(" + ", ".join(f"제{v}권" for v in sc.vols) + ") — 접미 미부여, 확인 필요")
    if sc.cues:
        segs.append("표지 단서 " + "·".join(sc.cues))
    return segs


def decide(name: str, sc: CoverScan, md_hint: list[str] | None = None, today: str = "",
           part_seg: str = "") -> Row:
    """규칙 1~2단계 — 순수 함수(가족 검사는 resolve_collisions). 표지 신호를 note 세그먼트로 남긴다."""
    base, reason, others = pick_base(sc)
    fn = filename_number(name)
    segs = cover_segments(sc, base, reason)
    if part_seg:
        segs.append(part_seg)
    if base is None:
        if fn:
            segs.append(f"힌트: 파일명 번호 {fn}")
        if md_hint:
            segs.append("힌트: 기존 .md " + ", ".join(md_hint))
        return Row(name, "", "", sc.title, NOTE_SEP.join(segs))
    rid = base + (f"-v{sc.vols[0]}" if len(sc.vols) == 1 else "")
    if fn and fn != base:
        segs.append(f"파일명 번호 {fn}와 다름(표지 채택)")
    if others:
        segs.append("다른 후보: " + ", ".join(others))
    return Row(name, rid, "cover", sc.title, NOTE_SEP.join(segs), "", today)


def decide_parts(name: str, part_scans: PartScans, md_hint: list[str] | None = None,
                 today: str = "") -> list[Row]:
    """파일 1개 → 행 목록(합본이면 파트마다 — 각 파트를 독립 문서로 결정)."""
    if len(part_scans) == 1 and part_scans[0][1] is None:
        return [decide(name, part_scans[0][0], md_hint, today)]
    n = len(part_scans)
    rows: list[Row] = []
    for i, (sc, rng) in enumerate(part_scans, 1):
        pages = format_pages(rng)
        seg = f"합본 파트 {i}/{n}({pages}쪽" + (", 쪽 번호 재시작)" if i > 1 else ")")
        row = decide(name, sc, md_hint, today, part_seg=seg)
        row.pages = pages
        rows.append(row)
    return rows


def resolve_collisions(new_rows: list[Row], existing: list[Row]) -> None:
    """가족(같은 YYYY-NN 행)이 하나라도 있으면 새 행을 공란으로 — 기존 행은 불변.

    같은 실행의 새 행끼리도 가족이면 전부 공란(SAME_RUN_FIRST_KEEPS=True면 정렬 첫 행 유지).
    충돌 상대는 note에 남긴다(`번호 중복(← 파일 = rid)`, 제목까지 같으면 `중복 의심`).
    """
    hits: list[tuple[Row, Row, str]] = []  # (새 행, 상대, 상대의 잠정 rid)
    for i, r in enumerate(new_rows):
        base = family_of(r.report_id) if r.report_id else ""
        if not base:
            continue
        pool = existing + (new_rows[:i] if SAME_RUN_FIRST_KEEPS else new_rows)
        others = [o for o in pool if o is not r and family_key(o) == base]
        if not others:
            continue
        others.sort(key=lambda o: (o not in existing, not o.report_id, o.report_id, o.file, o.pages))
        hits.append((r, others[0], others[0].report_id))
    for r, other, other_rid in hits:
        same = bool(r.cover_title) and fold_title(other.cover_title) == fold_title(r.cover_title)
        tag = "중복 의심: 같은 번호·같은 제목" if same else "번호 중복"
        where = f"{other.file}" + (f" #{other.pages}" if other.pages else "")
        r.note = f"{tag}(← {where} = {other_rid or '공란'})" + (NOTE_SEP + r.note if r.note else "")
        r.report_id, r.source, r.registered = "", "", ""


def decide_batch(items: list[tuple[str, PartScans, "list[str] | None"]], existing: list[Row],
                 today: str) -> list[Row]:
    """[(파일명, 파트 스캔, 기존 .md 힌트)] → 새 행 목록(가족 검사 완료). 단위 테스트 진입점."""
    new_rows: list[Row] = []
    for name, part_scans, md_hint in items:
        new_rows.extend(decide_parts(name, part_scans, md_hint, today))
    resolve_collisions(new_rows, existing)
    return new_rows


# ---------------------------------------------------------------------------
# 가족 문맥 · 제안 · 힌트 (CLI 「기입 필요」 출력과 /admin 가족 카드가 공유)
# ---------------------------------------------------------------------------

def by_file(rows: list[Row]) -> dict[str, list[Row]]:
    out: dict[str, list[Row]] = {}
    for r in rows:
        out.setdefault(r.file, []).append(r)
    for rs in out.values():
        rs.sort(key=registry._pages_sort)
    return out


def family_rows(rows: list[Row], base: str) -> list[Row]:
    return [r for r in rows if base and family_key(r) == base]


def doc_rids(rows: list[Row], base: str) -> list[str]:
    """가족 안의 '문서' rid(권 접미 없는 것: base, base-b …) 정렬."""
    out = set()
    for r in rows:
        m = STEM_RE.match(r.report_id or "")
        if m and m.group(1) == base and not m.group(3):
            out.add(r.report_id)
    return sorted(out)


def next_letter(rows: list[Row], base: str) -> str:
    used = {STEM_RE.match(r.report_id).group(2) for r in rows
            if r.report_id and STEM_RE.match(r.report_id) and STEM_RE.match(r.report_id).group(1) == base}
    for ch in LETTERS:
        if ch not in used:
            return f"{base}-{ch}"
    return f"{base}-z"


def next_vol(rows: list[Row], doc_rid: str) -> int:
    pat = re.compile(rf"^{re.escape(doc_rid)}-v(\d{{1,2}})$")
    used = [int(m.group(1)) for r in rows for m in [pat.match(r.report_id or "")] if m]
    n = 1
    while n in used:
        n += 1
    return n


def md_path(reports_dir: Path, rid: str) -> Path:
    return Path(reports_dir) / f"{rid}.md"


def md_source_of(path: Path) -> str:
    """reports/*.md frontmatter의 source_pdf(파일명, NFC) — 없으면 ''."""
    try:
        with open(path, encoding="utf-8") as f:
            for _ in range(40):
                line = f.readline()
                if not line:
                    break
                if line.startswith("source_pdf:"):
                    return norm_name(line.split(":", 1)[1].strip().strip('"'))
    except OSError:
        pass
    return ""


def md_sources(reports_dir: Path) -> dict[str, list[str]]:
    """{PDF 파일명: [기존 .md 스템]} — frontmatter source_pdf 기준(공란 행 힌트용)."""
    out: dict[str, list[str]] = {}
    for mp in sorted(Path(reports_dir).glob("*.md")):
        src = md_source_of(mp)
        if src:
            out.setdefault(src, []).append(mp.stem)
    return out


def suggest_rids(rows: list[Row], row: Row) -> list[dict]:
    """표지·가족·같은 파일에서 나온 제안만 — [{rid, label, why}]. 표지 밖 추론(파일명 번호·
    제목 유사)은 hints로 글만 보여 준다(사용자 결정 2026-09-08)."""
    out: list[dict] = []
    taken = {r.report_id for r in rows if r.report_id and r.key != row.key}

    def add(rid: str, label: str, why: str) -> None:
        if rid and rid not in taken and all(o["rid"] != rid for o in out):
            out.append({"rid": rid, "label": label, "why": why})

    base = family_key(row)
    info = parse_note(row.note)
    if base:
        fam = [r for r in family_rows(rows, base) if r.key != row.key]
        docs = doc_rids(fam, base)
        if base not in taken:
            add(base, "본편", "이 번호의 본편 rid가 비어 있음")
        if len(info.vols) == 1:
            v = info.vols[0]
            add(f"{base}-v{v}", f"표지 제{v}권", "표지·속표지에 제N권 인쇄")
            for d in docs:
                if d != base:
                    add(f"{d}-v{v}", f"{d}의 제{v}권", "표지·속표지에 제N권 인쇄")
        add(next_letter(fam, base), "다른 보고서", "같은 번호를 쓰는 별개의 독립 보고서")
        for d in docs:
            owner = next((r.file for r in fam if r.report_id == d), "")
            add(f"{d}-v{next_vol(fam, d)}", f"{d}의 별권·부록",
                f"{owner[:40]}에 딸린 문서(권·부록·첨부)")
    if row.pages:  # 합본 파트 — 같은 파일의 다른 파트에 딸린 문서일 수 있다
        for sib in rows:
            if sib.file == row.file and sib.key != row.key and sib.report_id:
                d = sib.report_id if not STEM_RE.match(sib.report_id) or not STEM_RE.match(sib.report_id).group(3) \
                    else sib.report_id
                add(f"{d}-v{next_vol(rows, d)}", f"{d}의 별권·부록",
                    f"같은 PDF 파트 {sib.pages}쪽({d})에 딸린 문서면")
    return out


def title_tokens(s: str) -> set[str]:
    """제목·파일명의 3자 이상 한글 어절(불용어 제외) — 2자 어절(산업·적용·기반)은 우연 일치가 많아 제외."""
    return {t for t in re.findall(r"[가-힣]{3,}", s or "") if t not in TITLE_STOP}


def similar_registered(rows: list[Row], row: Row, k: int = 3) -> list[tuple[Row, list[str]]]:
    """제목·파일명 단어가 겹치는 등록 행(rid 있음) 상위 k — 힌트 전용(결정론 정렬)."""
    mine_text = f"{row.cover_title} {Path(row.file).stem}"
    mine = title_tokens(mine_text)
    if not mine:
        return []
    scored = []
    for r in rows:
        if not r.report_id or r.file == row.file:
            continue
        theirs_text = f"{r.cover_title} {Path(r.file).stem}"
        theirs = title_tokens(theirs_text)
        # 띄어쓰기 차이("물리학분야" vs "물리학 분야")를 흡수하려고 토큰 포함 관계로 본다
        shared = sorted({t for t in theirs if t in mine_text} | {t for t in mine if t in theirs_text},
                        key=lambda t: (-len(t), t))
        if shared:
            scored.append((-sum(len(t) for t in shared), r.report_id, r.file, r, shared))
    scored.sort(key=lambda t: t[:3])
    return [(r, shared) for _, _, _, r, shared in scored[:k]]


def hints(rows: list[Row], row: Row, reports_dir: Path) -> list[str]:
    """글로만 보여 주는 힌트(버튼 없음): 파일명 번호 · 기존 .md · 제목 단어가 겹치는 등록 행."""
    out: list[str] = []
    fn = filename_number(row.file)
    if fn:
        out.append(f"파일명 번호 {fn}")
    stems = md_sources(reports_dir).get(row.file)
    if stems:
        out.append("기존 .md " + ", ".join(stems))
    if not family_key(row):
        for r, shared in similar_registered(rows, row):
            out.append(f"제목 단어가 겹치는 등록 행: {r.file[:40]} = {r.report_id} ({', '.join(shared[:3])})")
    return out


def row_view(r: Row, reports_dir: Path) -> dict:
    info = parse_note(r.note)
    return {"file": r.file, "pages": r.pages, "rid": r.report_id, "source": r.source,
            "cover_no": info.cover_no or family_of(r.report_id), "vols": info.vols, "cues": info.cues,
            "cover_title": r.cover_title, "registered": r.registered, "note": r.note,
            "extracted": bool(r.report_id) and md_path(reports_dir, r.report_id).exists()}


def family_context(rows: list[Row], base: str, group: list[Row], reports_dir: Path) -> dict:
    """가족 카드 재료 — 가족 전 행(등록·추출된 행 포함, 모두 편집 가능) + 공란 행과 같은 파일의
    다른 파트 행(합본 문맥: '파트 1 = 2025-17, 추출됨')."""
    files = {r.file for r in group}
    members = [r for r in rows if (base and family_key(r) == base) or r.file in files]
    members = list({r.key: r for r in members}.values())
    members.sort(key=lambda r: (r.file not in files, rid_sort_key(r)))
    return {
        "base": base,
        "rows": [row_view(r, reports_dir) for r in members],
        "inputs": [{"file": r.file, "pages": r.pages, "suggestions": suggest_rids(rows, r),
                    "hints": hints(rows, r, reports_dir)} for r in members if not r.report_id],
    }


def card_groups(rows: list[Row], pdf_dir: Path, reports_dir: Path) -> list[dict]:
    """기입 필요 행을 가족(표지 번호)별로 묶은 카드 재료 목록 — 번호 없는 행은 파일 단위."""
    present = {norm_name(p) for p in Path(pdf_dir).glob("*.pdf")}
    groups: dict[str, list[Row]] = {}
    for r in rows:
        if r.report_id or r.file not in present:
            continue
        key = family_key(r) or f"file:{r.file}"
        groups.setdefault(key, []).append(r)
    out = []
    for key in sorted(groups):
        base = "" if key.startswith("file:") else key
        out.append(family_context(rows, base, groups[key], reports_dir))
    return out


# ---------------------------------------------------------------------------
# 표 쓰기 · 정합 검사
# ---------------------------------------------------------------------------

def sanitize(s: str) -> str:
    return " ".join(str(s or "").replace("\t", " ").split())


def write_rows(rows: list[Row], path: Path) -> None:
    lines = ["\t".join(COLUMNS)]
    for r in sorted(rows, key=rid_sort_key):
        source = r.source or ("manual" if r.report_id else "")
        lines.append("\t".join(sanitize(v) for v in
                               (r.file, r.pages, r.report_id, source, r.cover_title, r.registered, r.note)))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def row_label(r: Row) -> str:
    return r.file + (f" #{r.pages}" if r.pages else "")


def fill_lines(rows: list[Row], r: Row, reports_dir: Path) -> str:
    """「기입 필요」 한 항목 — 파일 줄 + 가족 줄 + 제안 줄 + 힌트 줄(사람이 표를 뒤지지 않게)."""
    lines = [f"{row_label(r)} — {r.note or '사유 미기록'}"]
    base = family_key(r)
    fam = [o for o in family_rows(rows, base) if o.key != r.key] if base else []
    if fam:
        parts = []
        for o in sorted(fam, key=rid_sort_key):
            tag = o.report_id or "공란"
            extra = []
            if o.registered:
                extra.append(f"등록 {o.registered}")
            if o.report_id and md_path(reports_dir, o.report_id).exists():
                extra.append("추출됨")
            parts.append(f"{row_label(o)} = {tag}" + (f"({', '.join(extra)})" if extra else ""))
        lines.append(f"가족 {base}: " + " · ".join(parts))
    sug = suggest_rids(rows, r)
    if sug:
        lines.append("제안: " + " · ".join(f"{s['rid']}({s['label']})" for s in sug))
    hs = hints(rows, r, reports_dir)
    if hs:
        lines.append("힌트: " + " · ".join(hs))
    return "\n      ".join(lines)


def check_table(rows: list[Row], pdf_dir: Path, reports_dir: Path = Path("reports")) -> dict[str, list[str]]:
    """사람이 고칠 것 — {종류: [줄]}. 종류 순서 = 출력 순서.

    pdfs/hold/ 보류 파일은 등록 대상이 아니다(2026-09-07 원칙) — 표에 그 행이 남아 있으면
    지운 파일과 같이 "행 삭제 필요"로 표시하되 사유를 구분한다. 등록은 반출 후 register가 다시 한다.
    """
    present = {norm_name(p) for p in Path(pdf_dir).glob("*.pdf")}
    held = registry.held_names(pdf_dir)
    out: dict[str, list[str]] = {"기입 필요": [], "행 삭제 필요": [], "형식 위반": [], "rid 중복": [],
                                 "미수록(register 실행 필요)": [], "열 밀림 의심(탭 개수)": [], "검토": []}
    by_rid: dict[str, list[str]] = {}
    grouped = by_file(rows)
    for f, frows in sorted(grouped.items()):
        if f not in present:
            why = "pdfs/hold/ 보류 중 — 보류 파일은 등록하지 않음(반출 후 재등록)" if f in held else "pdfs/에 없음"
            out["행 삭제 필요"].append(f"{f} — {why}" + (f" (행 {len(frows)})" if len(frows) > 1 else ""))
            continue
        problem = registry._rows_problem(frows)
        if problem and ("pages" in problem):
            out["형식 위반"].append(f"{f} — {problem}")
        for r in frows:
            if r.source not in SOURCES + ("",):  # 탭이 밀려 다른 열의 값이 source에 들어온 줄
                out["열 밀림 의심(탭 개수)"].append(
                    f"{row_label(r)} — source 열에 '{r.source[:24]}' — 손편집 대신 --set \"파일명 일부=YYYY-NN\" 사용")
            if r.registered and not registry.DATE_RE.match(r.registered):
                out["열 밀림 의심(탭 개수)"].append(f"{row_label(r)} — registered 열에 '{r.registered[:24]}'")
            if not r.report_id:
                out["기입 필요"].append(fill_lines(rows, r, reports_dir))
                continue
            if not RID_RE.match(r.report_id):
                out["형식 위반"].append(f"{row_label(r)} — {r.report_id!r}: ASCII 영숫자·하이픈만(밑줄·한글 불가)")
                continue
            by_rid.setdefault(r.report_id, []).append(row_label(r))
            if not STEM_RE.match(r.report_id):
                out["검토"].append(f"{row_label(r)} → {r.report_id} — 표준형(YYYY-NN[-b][-vN])이 아님(연도 대조 제외)")
            elif any(k in r.note for k in REVIEW_MARKS):
                out["검토"].append(f"{row_label(r)} → {r.report_id} — {r.note}")
    for rid, fs in sorted(by_rid.items()):
        if len(fs) > 1:
            out["rid 중복"].append(f"{rid} ← {', '.join(fs)}")
    for f in sorted(present - set(grouped)):
        out["미수록(register 실행 필요)"].append(f)
    return out


def print_issues(issues: dict[str, list[str]]) -> bool:
    """「사람이 고칠 것」 출력. 반환 = 조치 필요 여부(검토 항목은 제외)."""
    blocking = [k for k in issues if k != "검토" and issues[k]]
    print("== 사람이 고칠 것 (report_ids.tsv) ==")
    if not blocking and not issues["검토"]:
        print("없음 — 표와 pdfs/ 정합")
    for kind, items in issues.items():
        if not items:
            continue
        print(f"[{kind} {len(items)}]")
        for it in items:
            print(f"  - {it}")
    return bool(blocking)


# ---------------------------------------------------------------------------
# 기입 · 개명 · 삭제 (CLI --set/--drop 과 /admin 가족 저장이 공유하는 유일한 쓰기 경로)
# ---------------------------------------------------------------------------

def validate_rid(rows: list[Row], row: Row, rid: str) -> tuple[str | None, str | None]:
    """(오류 문장 | None, 경고 문장 | None) — 형식·중복 검사만(표 불변). rid 공란 = 공란 복귀 허용."""
    if not rid:
        return None, None
    if not RID_RE.match(rid):
        return f"'{rid}' 형식 위반 — ASCII 영숫자·하이픈만(밑줄·한글·공백 불가)", None
    dup = [r for r in rows if r.report_id == rid and r.key != row.key]
    if dup:
        base = family_of(rid) or rid
        return (f"'{rid}'는 이미 '{row_label(dup[0])[:60]}'의 번호 — 다른 보고서면 '{base}-b'처럼 글자 접미, "
                f"그 보고서의 별권·부록이면 '{rid}-v1'"), None
    if not STEM_RE.match(rid):
        return None, f"'{rid}'는 표준형(YYYY-NN[-b][-vN])이 아님 — 연도 대조 제외"
    return None, None


def next_unknown(rows: list[Row], base: str, own: tuple[str, str]) -> str:
    """번호 미상 규약(사용자 결정 2026-09-04): 관리번호를 못 찾으면 `YYYY-00`, 연도도 모르면
    `0000-00` — 같은 연도의 미상 건끼리는 다음 빈 `-vN`으로 구분(`2021-00-v1`, `-v2`…).
    own = 기입 대상 행(자기 값은 빈 번호로 취급 — 같은 값 재입력 시 번호 유지)."""
    used = {r.report_id for r in rows if r.key != own}
    n = 1
    while f"{base}-v{n}" in used:
        n += 1
    return f"{base}-v{n}"


def rename_reports(pairs: list[tuple[str, str]], reports_dir: Path, cache_path: Path) -> None:
    """추출된 문서의 rid 변경 = reports/{old}.md → {new}.md 개명 + `report_id:` 줄·`<!-- id: {old}_c`
    접두·`summary_reviewed` hid 접두·연도·판정 캐시 버킷 치환(요약 블록·스탬프·source_pdf 불변, LLM 0). 맞바꿈이 안전하도록
    임시 이름 2단계. W0 슬러그 치환(2026-09-04)과 같은 로직."""
    reports_dir = Path(reports_dir)
    tmp: dict[str, Path] = {}
    for old, _new in pairs:
        t = reports_dir / f".rename_{old}.md.tmp"
        os.replace(reports_dir / f"{old}.md", t)
        tmp[old] = t
    cache: dict = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except ValueError:
            cache = {}
    for old, new in pairs:
        text = tmp[old].read_text(encoding="utf-8")
        if text.count(f"report_id: {old}\n") != 1:
            raise RuntimeError(f"{old}: report_id 줄이 1개가 아님")
        text = text.replace(f"report_id: {old}\n", f"report_id: {new}\n", 1)
        text = text.replace(f"<!-- id: {old}_c", f"<!-- id: {new}_c")
        # 사람 확인 요약 표식(frontmatter summary_reviewed)의 hid 접두도 헤딩 ID와 같은 규칙으로
        text = re.sub(r"^(summary_reviewed:.*)$",
                      lambda m: m.group(1).replace(f"{old}_c", f"{new}_c"), text, count=1, flags=re.M)
        if STEM_RE.match(old) and STEM_RE.match(new) and old[:4] != new[:4]:
            yr = "" if new.startswith("0000") else new[:4]
            text = re.sub(r"^year:.*$", f"year: {yr}".rstrip(), text, count=1, flags=re.M)
        if f"<!-- id: {old}_c" in text:
            raise RuntimeError(f"{old}: 옛 헤딩 ID 잔존")
        dst = reports_dir / f"{new}.md"
        if dst.exists():
            raise RuntimeError(f"개명 대상이 이미 존재: {dst.name}")
        dst.write_text(text, encoding="utf-8", newline="\n")
        os.remove(tmp[old])
        if isinstance(cache, dict) and old in cache:
            bucket = cache.pop(old)
            cache[new] = {(f"{new}_c" + k[len(old) + 2:] if k.startswith(f"{old}_c") else k): v
                          for k, v in bucket.items()}
    if pairs and isinstance(cache, dict) and cache_path.exists():
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def set_family(rows: list[Row], changes: list[tuple[str, str, str]], reports_dir: Path = Path("reports"),
               logs_dir: Path = Path("logs"), today: str | None = None,
               ) -> tuple[str | None, list[str], list[tuple[str, str]]]:
    """여러 행의 rid를 한 번에(원자적) 기입 — 반환 (오류|None, 경고들, 개명 [(old, new)]).

    changes = [(파일명, pages, rid)]. ① 대상 행을 전부 비운 상태에서 최종 rid 집합을 검증
    (형식·중복·YYYY-00 규약·비표준 경고) ② 추출된 문서(reports/{old}.md)의 rid 변경은 개명
    (목적지 .md가 이번 변경으로 비워지지 않는 다른 문서 것이면 오류) ③ 표 기록(source=manual,
    registered=오늘; 공란이면 둘 다 공란). 오류면 아무것도 바꾸지 않는다.
    """
    reports_dir, logs_dir = Path(reports_dir), Path(logs_dir)
    today = today or datetime.date.today().isoformat()
    by_key = {r.key: r for r in rows}
    norm: list[tuple[tuple[str, str], str]] = []
    for file, pages, rid in changes:
        key = (norm_name(file), (pages or "").strip())
        if key not in by_key:
            return f"표에 없는 행: {key[0]}" + (f" #{key[1]}" if key[1] else ""), [], []
        if any(k == key for k, _ in norm):
            return f"같은 행이 두 번 지정됨: {key[0]}", [], []
        norm.append((key, (rid or "").strip()))

    probe = copy.deepcopy(rows)
    pby = {r.key: r for r in probe}
    for key, _ in norm:
        pby[key].report_id = ""
    warnings: list[str] = []
    finals: list[tuple[tuple[str, str], str]] = []
    for key, typed in norm:
        rid = typed
        if UNKNOWN_RE.match(rid):
            rid = next_unknown(probe, rid, key)
        err, warn = validate_rid(probe, pby[key], rid)
        label = row_label(by_key[key])
        if err:
            return f"{label}: {err}", [], []
        if rid != typed:
            warn = f"번호 미상 규약: {typed} → {rid}(다음 빈 -vN)" + (f" · {warn}" if warn else "")
        if warn:
            warnings.append(f"{label}: {warn}")
        pby[key].report_id = rid
        finals.append((key, rid))

    pairs: list[tuple[str, str]] = []
    for key, rid in finals:
        old = by_key[key].report_id
        if old and old != rid and md_path(reports_dir, old).exists():
            if not rid:
                return (f"{row_label(by_key[key])}: 이미 추출된 문서(reports/{old}.md)는 공란으로 되돌릴 수 없음 — "
                        "다른 rid를 적거나 .md를 정리한 뒤 다시"), [], []
            pairs.append((old, rid))
    vacated = {old for old, _ in pairs}
    for key, rid in finals:
        if not rid or rid == by_key[key].report_id or rid in vacated:
            continue
        dst = md_path(reports_dir, rid)
        if dst.exists():
            src = md_source_of(dst)
            if src != key[0]:
                return f"reports/{rid}.md가 이미 다른 PDF({src or '?'})의 산출물 — 그 rid는 쓸 수 없음", [], []

    if pairs:
        rename_reports(pairs, reports_dir, logs_dir / "judge_cache.json")
    for key, rid in finals:
        r = by_key[key]
        if rid == r.report_id:
            continue  # 같은 값 재입력 — source·등록일 유지
        r.report_id = rid
        r.source = "manual" if rid else ""
        r.registered = today if rid else ""
    return None, warnings, pairs


def resolve_key(rows: list[Row], key: str) -> tuple[Row | None, str | None]:
    """'파일명일부[#N]' → 행. 파일명 일부는 유일해야 하고 합본은 #파트번호(1부터)가 필요하다."""
    key = unicodedata.normalize("NFC", key.strip())
    part = None
    m = KEY_PART_RE.match(key)
    if m:
        key, part = m.group(1).strip(), int(m.group(2))
    grouped = by_file(rows)
    hits = sorted(f for f in grouped if key in f)
    if len(hits) != 1:
        return None, (f"'{key}': 일치 파일 {len(hits)}건 — 유일해야 합니다"
                      + (": " + "; ".join(h[:50] for h in hits[:5]) if hits else ""))
    frows = grouped[hits[0]]
    if part is None:
        if len(frows) > 1:
            return None, (f"'{key}': 합본({len(frows)}파트) — '{key}#1'처럼 파트 번호를 지정하세요: "
                          + ", ".join(f"#{i} {r.pages}쪽" for i, r in enumerate(frows, 1)))
        return frows[0], None
    if not 1 <= part <= len(frows):
        return None, f"'{key}#{part}': 파트 {part} 없음(1~{len(frows)})"
    return frows[part - 1], None


def apply_sets(rows: list[Row], specs: list[str], reports_dir: Path, logs_dir: Path) -> int:
    """--set '파일명일부[#N]=RID' 전건을 set_family 한 번으로. 반환 0 = 반영, 2 = 사용 오류(무변경)."""
    changes: list[tuple[str, str, str]] = []
    for spec in specs:
        if "=" not in spec:
            print(f"--set 형식 오류(파일명일부[#N]=RID): {spec}", file=sys.stderr)
            return 2
        key, rid = spec.rsplit("=", 1)
        row, err = resolve_key(rows, key)
        if err:
            print(f"--set {err}", file=sys.stderr)
            return 2
        changes.append((row.file, row.pages, rid.strip()))
    err, warns, renamed = set_family(rows, changes, reports_dir, logs_dir)
    if err:
        print(f"--set: {err}", file=sys.stderr)
        return 2
    for w in warns:
        print(f"[경고] {w}", file=sys.stderr)
    for old, new in renamed:
        print(f"[개명] reports/{old}.md → reports/{new}.md (헤딩 ID·판정 캐시 치환 — build_db·build_index 재실행 필요)",
              file=sys.stderr)
    for file, pages, rid in changes:
        print(f"[set] {rid or '(공란)'}: {file}" + (f" #{pages}" if pages else ""), file=sys.stderr)
    return 0


def drop_file(rows: list[Row], key: str, reports_dir: Path) -> tuple[list[Row], str | None]:
    """'파일명일부'(유일)에 해당하는 파일의 행 전부 삭제 — 반환 (지운 행들, 오류)."""
    key = unicodedata.normalize("NFC", key.strip())
    hits = sorted({r.file for r in rows if key in r.file})
    if len(hits) != 1:
        return [], (f"'{key}': 일치 파일 {len(hits)}건 — 유일해야 합니다"
                    + (": " + "; ".join(h[:50] for h in hits[:5]) if hits else ""))
    removed = [r for r in rows if r.file == hits[0]]
    for r in removed:
        rows.remove(r)
    return removed, None


# ---------------------------------------------------------------------------
# v4 이전 행 보정 (registered 공란인 행만 1회 — 이후 실행은 no-op)
# ---------------------------------------------------------------------------

def backfill_legacy(rows: list[Row], pdf_dir: Path, today: str) -> tuple[int, int]:
    """rid는 있는데 registered가 공란인 행(v3) → 등록일 2026-09-04 기입; 단독 행이 합본이면
    파트 행으로 확장(파트 1 = 기존 rid, 파트 2~ = 자기 표지로 결정 → 번호 없으면 공란).
    반환 (등록일 기입 수, 합본 확장 파일 수)."""
    dated = expanded = 0
    for f, frows in list(by_file(rows).items()):
        legacy = [r for r in frows if r.report_id and not r.registered]
        if not legacy:
            continue
        pdf = Path(pdf_dir) / f
        if len(frows) == 1 and not frows[0].pages and pdf.is_file():
            r = frows[0]
            part_scans = scan_pdf(pdf)
            if len(part_scans) > 1:
                n = len(part_scans)
                r.pages = format_pages(part_scans[0][1])
                r.note = NOTE_SEP.join([s for s in [r.note, f"합본 파트 1/{n}({r.pages}쪽)"] if s])
                r.registered = LEGACY_REGISTERED_DATE
                new_rows: list[Row] = []
                for i, (sc, rng) in enumerate(part_scans[1:], 2):
                    pages = format_pages(rng)
                    row = decide(f, sc, None, today, part_seg=f"합본 파트 {i}/{n}({pages}쪽, 쪽 번호 재시작)")
                    row.pages = pages
                    new_rows.append(row)
                resolve_collisions(new_rows, [o for o in rows if o is not r] + [r])
                rows.extend(new_rows)
                expanded += 1
                dated += 1
                continue
        for r in legacy:
            r.registered = LEGACY_REGISTERED_DATE
            dated += 1
    return dated, expanded


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="표지 관리번호 → report_ids.tsv 등록 (파이프라인 1단계, LLM 0).")
    parser.add_argument("pdf", nargs="*", help="대상 PDF (기본: pdfs/*.pdf 전부 — 자체 글롭 확장)")
    parser.add_argument("--pdf-dir", default="pdfs")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--logs-dir", default="logs")
    parser.add_argument("--registry", default=str(REGISTRY_PATH))
    parser.add_argument("--check", action="store_true", help="쓰기 없이 표↔pdfs 정합 검사만")
    parser.add_argument("--dry-run", action="store_true", help="추가될 행만 출력(쓰기 없음)")
    parser.add_argument("--set", action="append", default=[], metavar="파일명일부[#N]=RID",
                        help="표를 손으로 고치지 않고 기입: 파일명 일부(유일해야 함)[#파트번호]=관리번호. 반복 가능. "
                             "빈 값(=)은 공란으로 되돌림. 추출된 문서의 rid 변경은 .md 개명까지 수행")
    parser.add_argument("--drop", action="append", default=[], metavar="파일명일부",
                        help="그 파일의 행 전부 삭제(PDF 삭제·개명·교체 뒤 재등록용)")
    args = parser.parse_args()

    pdf_dir = Path(args.pdf_dir)
    if not pdf_dir.is_dir():
        print(f"디렉터리가 없습니다: {pdf_dir}", file=sys.stderr)
        sys.exit(2)
    reg_path = Path(args.registry)
    reports_dir, logs_dir = Path(args.reports_dir), Path(args.logs_dir)
    rows = read_rows(reg_path)
    today = datetime.date.today().isoformat()

    if args.drop or args.set:
        for key in args.drop:
            removed, err = drop_file(rows, key, reports_dir)
            if err:
                print(f"--drop {err}", file=sys.stderr)
                sys.exit(2)
            for r in removed:
                left = " (reports/{0}.md는 남아 있음 — 정리 필요)".format(r.report_id) \
                    if r.report_id and md_path(reports_dir, r.report_id).exists() else ""
                print(f"[drop] {row_label(r)} = {r.report_id or '(공란)'}{left}", file=sys.stderr)
        if args.set:
            code = apply_sets(rows, args.set, reports_dir, logs_dir)
            if code:
                sys.exit(code)
        write_rows(rows, reg_path)
        print(f"== 등록부 {reg_path.name}: --set {len(args.set)}건 · --drop {len(args.drop)}건 반영 ==")
        sys.exit(1 if print_issues(check_table(rows, pdf_dir, reports_dir)) else 0)

    if args.check:
        print(f"== 등록부 {reg_path.name}: 행 {len(rows)} ==")
        sys.exit(1 if print_issues(check_table(rows, pdf_dir, reports_dir)) else 0)

    paths: list[str] = []
    for p in args.pdf or [str(pdf_dir / "*.pdf")]:
        # 실존 파일은 글롭 확장하지 않는다(이름에 [가 든 파일 보호 — extract와 동일)
        paths.extend(sorted(globmod.glob(p)) if not Path(p).is_file() and any(c in p for c in "*?[") else [p])
    missing = [p for p in paths if not Path(p).is_file()]
    if missing:
        print("파일이 없습니다: " + ", ".join(missing), file=sys.stderr)
        sys.exit(2)
    held = [p for p in paths if registry.is_held(p, pdf_dir)]
    if held:  # 보류 파일은 등록하지 않는다(2026-09-07) — 반출(pdfs/ 직하로 이동) 후 등록
        print(f"보류 파일은 등록하지 않습니다({len(held)}건, {pdf_dir / registry.HOLD_SUBDIR}/): "
              "pdfs/ 직하로 옮긴 뒤 등록하세요 — " + ", ".join(norm_name(p) for p in held[:5])
              + (" …" if len(held) > 5 else ""), file=sys.stderr)
        sys.exit(2)

    dated = expanded = 0
    if not args.dry_run:
        dated, expanded = backfill_legacy(rows, pdf_dir, today)
    hints_by_file = md_sources(reports_dir)
    grouped = by_file(rows)
    items: list[tuple[str, PartScans, "list[str] | None"]] = []
    filled_title = 0
    for p in sorted(paths, key=lambda p: norm_name(p)):
        name = norm_name(p)
        if name in grouped:
            for r in grouped[name]:
                if r.cover_title:
                    continue
                try:
                    rng = parse_pages(r.pages)
                except ValueError:
                    continue
                doc = pymupdf.open(p)
                try:
                    title = cover_title(doc[rng[0] if rng else 0]) if len(doc) else ""
                finally:
                    doc.close()
                if title:
                    r.cover_title, filled_title = title, filled_title + 1
            continue
        items.append((name, scan_pdf(p), hints_by_file.get(name)))
    added = decide_batch(items, rows, today)
    rows.extend(added)
    for row in added:
        tag = row.report_id or "(공란)"
        part = f" [파트 {row.pages}]" if row.pages else ""
        print(f"[{'cover' if row.report_id else '기입 필요'}] {tag}{part}: {row.file}"
              + (f" — {row.note}" if row.note else ""), file=sys.stderr)

    if not args.dry_run and (added or filled_title or dated or expanded):
        write_rows(rows, reg_path)
        registry.load_registry(reg_path)  # 캐시 갱신(같은 프로세스 내 후속 조회용)

    n_files = len(by_file(rows))
    n_parts = sum(1 for r in rows if r.pages)
    n_rid = sum(1 for r in rows if r.report_id)
    n_cover = sum(1 for r in rows if r.report_id and r.source in ("cover", ""))
    n_manual = sum(1 for r in rows if r.report_id and r.source == "manual")
    stems = [STEM_RE.match(r.report_id) for r in rows if r.report_id]
    n_vol = sum(1 for m in stems if m and m.group(3))
    n_letter = sum(1 for m in stems if m and m.group(2))
    print(f"== 등록부 {reg_path.name}{' (dry-run — 미기록)' if args.dry_run else ''} ==")
    print(f"행 {len(rows)} (파일 {n_files} · 합본 파트 행 {n_parts} · 이번 추가 {len(added)} · 합본 확장 {expanded}"
          f" · 등록일 보정 {dated}) · rid 확정 {n_rid} (cover {n_cover} · manual {n_manual}"
          f" · -vN {n_vol} · 글자 접미 {n_letter}) · 공란 {len(rows) - n_rid}")
    sys.exit(1 if print_issues(check_table(rows, pdf_dir, reports_dir)) else 0)


if __name__ == "__main__":
    main()
