"""관리번호 등록부 생성 — 파이프라인 1단계 (LLM 0).

PDF 표지에 인쇄된 관리번호(`정책연구 YYYY-NN`)를 읽어 `report_ids.tsv`에 기입한다.
이후 모든 단계(extract·verify·promote·status·serve)는 이 표만 읽는다(`src/registry.py`) —
표에 없거나 rid가 공란인 PDF는 어느 단계도 지나지 못하므로, 표지에서 못 읽은 파일은
이 도구가 출력하는 「사람이 고칠 것」 목록을 보고 사람이 표에 적는다.

rid 부여 규칙(2026-09-04 확정 — 파일명 번호는 rid에 쓰지 않고 힌트로만 출력):
 1) 기본 번호: 표의 기존 행에 rid가 있으면 그대로(재실행 불변). 없으면 표지 3단 탐색
    ① 1쪽 → ② 1~6쪽 → ③ 1~6쪽+끝 2쪽 — 각 단에서 같은 줄에 정책연구/연구보고/
    관리번호/NRF 문맥이 있는 `YYYY-NN`이 정확히 하나면 채택(source=cover), 여러 개면
    그 단에서 멈추고 공란(후보를 note에 나열), 하나도 없으면 다음 단. 전부 실패 → 공란.
 2) 권수 접미: 표지·속표지(1~2쪽)에 `제N권`이 있으면 충돌 여부와 무관하게 `-vN`
    (권이 시차를 두고 들어와도 먼저 온 권이 접미 없이 굳지 않도록). 실측 오탐 0.
 3) 중복 해소: 2)까지의 rid가 다른 행과 같으면 표지 제목(1쪽 최대 글꼴 텍스트)을 대조 —
    다르면 다른 문서 → 정렬순 뒤 파일에 `-b`(`-c`…), 같으면 중복 다운로드 의심 → 공란.
 4) 합본 파트 `_NN`은 표가 아니라 extract가 붙인다.
기존 행의 rid·source·note는 절대 덮어쓰지 않는다(cover_title만 공란이면 채움). 표에는
있는데 pdfs/에 없는 파일은 행을 지우지 않고 "행 삭제 필요"로 출력한다.

사용: python src/register.py ["pdfs/<파일>.pdf" ...]   # 인자 없으면 pdfs/*.pdf 전부
      --check   쓰기 없이 표↔pdfs 정합 검사만(공란·파일 없음·미수록·형식 위반·중복)
      --dry-run 추가될 행만 출력
종료 코드: 0 = 사람이 고칠 것 없음, 1 = 기입·정리 필요, 2 = 사용 오류.
실측(2026-09-04, 278권): 자동 확정 258 · 기입 필요 20 · `-vN` 4(2018-49) · `-b` 2.
"""

from __future__ import annotations

import argparse
import glob as globmod
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

import registry
from registry import COLUMNS, REGISTRY_PATH, RID_RE, SOURCES, STEM_RE, Row, norm_name, read_rows

NUM_RE = re.compile(r"((?:19|20)\d{2})\s*[-–]\s*(\d{1,3})(?!\d)")
CTX_RE = re.compile(r"정책\s*연구|연구\s*보고|관리\s*번호|NRF")
VOL_RE = re.compile(r"제\s*(\d{1,2})\s*권(?![가-힣])")
# 파일명 번호 — rid에 쓰지 않고 힌트·불일치 note에만 (종전 extract의 파일명 레인 정규식)
FILENAME_NUM_RE = re.compile(r"정책연구-(\d{4}-\d{2})|^\s*\((\d{4})-(\d{1,2})\)")
FRONT_PAGES = 6
VOL_PAGES = 2
BACK_PAGES = 2
DUP_SUFFIXES = "bcdefghijklmnopqrstuvwxyz"
REVIEW_MARKS = ("다른 후보", "와 다름(표지 채택)")  # 검토 목록 대상 note 표지 — 자동 채택이 다른 신호를 제친 경우


@dataclass
class CoverScan:
    n_pages: int = 0
    by_page: dict[int, list[str]] = field(default_factory=dict)  # 1-based 쪽 → 문맥 있는 후보
    vols: list[int] = field(default_factory=list)
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


def scan_cover(pdf_path) -> CoverScan:
    doc = pymupdf.open(str(pdf_path))
    try:
        sc = CoverScan(n_pages=len(doc))
        for pno in scan_pages(sc.n_pages):
            text = unicodedata.normalize("NFC", doc[pno - 1].get_text())
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
        if sc.n_pages:
            sc.title = cover_title(doc[0])
    finally:
        doc.close()
    return sc


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
            return None, f"후보 여러 개({label}): {', '.join(cands)}", cands
    if sc.text_chars == 0:
        return None, "표지 이미지(텍스트 없음)", []
    return None, "표지에 관리번호 없음", []


def filename_number(name: str) -> str | None:
    m = FILENAME_NUM_RE.search(Path(name).stem)
    if not m:
        return None
    return m.group(1) if m.group(1) else f"{m.group(2)}-{int(m.group(3)):02d}"


def fold_title(s: str) -> str:
    return unicodedata.normalize("NFC", "".join(s.split())).lower()


def decide(name: str, sc: CoverScan, taken: dict[str, Row], md_hint: list[str] | None = None) -> Row:
    """규칙 1~3단계 — 순수 함수(taken = 이미 rid가 정해진 행들, 파일명 정렬순으로 누적)."""
    base, reason, others = pick_base(sc)
    notes: list[str] = []
    fn = filename_number(name)
    if base is None:
        notes.append(reason)
        if fn:
            notes.append(f"힌트: 파일명 번호 {fn}")
        if md_hint:
            notes.append("힌트: 기존 .md " + ", ".join(md_hint))
        return Row(name, "", "", sc.title, " · ".join(notes))
    rid = base
    if len(sc.vols) == 1:
        rid += f"-v{sc.vols[0]}"
    elif len(sc.vols) > 1:
        notes.append(f"권 신호 여러 개({', '.join(map(str, sc.vols))}) — 접미 미부여, 확인 필요")
    if fn and fn != base:
        notes.append(f"파일명 번호 {fn}와 다름(표지 채택)")
    if others:
        notes.append("다른 후보: " + ", ".join(others))
    family = [r for r in taken.values()
              if r.report_id == rid or re.fullmatch(re.escape(rid) + r"-[b-z]", r.report_id)]
    if family:
        same = [r for r in family if sc.title and fold_title(r.cover_title) == fold_title(sc.title)]
        if same:
            notes.insert(0, f"중복 의심: 같은 번호·같은 제목(← {same[0].file})")
            return Row(name, "", "", sc.title, " · ".join(notes))
        used = {r.report_id for r in family}
        for s in DUP_SUFFIXES:
            cand = f"{rid}-{s}"
            if cand not in used:
                notes.insert(0, f"번호 중복(← {family[0].file})")
                rid = cand
                break
    return Row(name, rid, "cover", sc.title, " · ".join(notes))


def md_sources(reports_dir: Path) -> dict[str, list[str]]:
    """{PDF 파일명: [기존 .md 스템]} — frontmatter source_pdf 기준(공란 행 힌트용)."""
    out: dict[str, list[str]] = {}
    for mp in sorted(Path(reports_dir).glob("*.md")):
        try:
            with open(mp, encoding="utf-8") as f:
                for _ in range(40):
                    line = f.readline()
                    if not line:
                        break
                    if line.startswith("source_pdf:"):
                        src = line.split(":", 1)[1].strip().strip('"')
                        out.setdefault(norm_name(src), []).append(mp.stem)
                        break
        except OSError:
            continue
    return out


def sanitize(s: str) -> str:
    return " ".join(str(s or "").replace("\t", " ").split())


def write_rows(rows: list[Row], path: Path) -> None:
    lines = ["\t".join(COLUMNS)]
    for r in sorted(rows, key=lambda r: r.file):
        source = r.source or ("manual" if r.report_id else "")
        lines.append("\t".join(sanitize(v) for v in (r.file, r.report_id, source, r.cover_title, r.note)))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def check_table(table: dict[str, Row], pdf_dir: Path) -> dict[str, list[str]]:
    """사람이 고칠 것 — {종류: [줄]}. 종류 순서 = 출력 순서.

    pdfs/hold/ 보류 파일은 등록 대상이 아니다(2026-09-07 원칙) — 표에 그 행이 남아 있으면
    지운 파일과 같이 "행 삭제 필요"로 표시하되 사유를 구분한다. 등록은 반출 후 register가 다시 한다.
    """
    present = {norm_name(p) for p in Path(pdf_dir).glob("*.pdf")}
    held = registry.held_names(pdf_dir)
    out: dict[str, list[str]] = {"기입 필요": [], "행 삭제 필요": [], "형식 위반": [],
                                 "rid 중복": [], "미수록(register 실행 필요)": [], "검토": []}
    by_rid: dict[str, list[str]] = {}
    for f, r in sorted(table.items()):
        if f not in present:
            why = "pdfs/hold/ 보류 중 — 보류 파일은 등록하지 않음(반출 후 재등록)" if f in held else "pdfs/에 없음"
            out["행 삭제 필요"].append(f"{f} — {why}")
            continue
        if r.source not in SOURCES + ("",):  # 탭이 밀려 다른 열의 값이 source에 들어온 줄
            out.setdefault("열 밀림 의심(탭 개수)", []).append(
                f"{f} — source 열에 '{r.source[:24]}' — 손편집 대신 --set \"파일명 일부=YYYY-NN\" 사용 권장")
        if not r.report_id:
            out["기입 필요"].append(f"{f} — {r.note or '사유 미기록'}")
            continue
        if not RID_RE.match(r.report_id):
            out["형식 위반"].append(f"{f} — {r.report_id!r}: ASCII 영숫자·하이픈만(밑줄·한글 불가)")
            continue
        by_rid.setdefault(r.report_id, []).append(f)
        if not STEM_RE.match(r.report_id):
            out["검토"].append(f"{f} → {r.report_id} — 표준형(YYYY-NN[-vN][-b])이 아님(연도 대조 제외)")
        elif any(k in r.note for k in REVIEW_MARKS):
            out["검토"].append(f"{f} → {r.report_id} — {r.note}")
    for rid, fs in sorted(by_rid.items()):
        if len(fs) > 1:
            out["rid 중복"].append(f"{rid} ← {', '.join(fs)}")
    for f in sorted(present - set(table)):
        out["미수록(register 실행 필요)"].append(f)
    return out


def validate_rid(table: dict[str, Row], file: str, rid: str) -> tuple[str | None, str | None]:
    """(오류 문장 | None, 경고 문장 | None) — 형식·중복 검사만(표 불변). rid 공란 = 공란 복귀 허용."""
    if file not in table:
        return f"표에 없는 파일: {file}", None
    if not rid:
        return None, None
    if not RID_RE.match(rid):
        return f"'{rid}' 형식 위반 — ASCII 영숫자·하이픈만(밑줄·한글·공백 불가)", None
    dup = [f for f, r in table.items() if r.report_id == rid and f != file]
    if dup:
        return f"'{rid}'는 이미 '{dup[0][:50]}'의 번호 — 다른 문서면 '{rid}-b'처럼 접미", None
    if not STEM_RE.match(rid):
        return None, f"'{rid}'는 표준형(YYYY-NN[-vN][-b])이 아님 — 연도 대조 제외"
    return None, None


UNKNOWN_RE = re.compile(r"^(\d{4})-00$")


def next_unknown(table: dict[str, Row], base: str, own: str) -> str:
    """번호 미상 규약(사용자 결정 2026-09-04): 관리번호를 못 찾으면 `YYYY-00`, 연도도 모르면
    `0000-00` — 같은 연도의 미상 건끼리는 다음 빈 `-vN`으로 구분(`2021-00-v1`, `-v2`…).
    own = 기입 대상 파일(자기 값은 빈 번호로 취급 — 같은 값 재입력 시 번호 유지)."""
    used = {r.report_id for f, r in table.items() if f != own}
    n = 1
    while f"{base}-v{n}" in used:
        n += 1
    return f"{base}-v{n}"


def set_rid(table: dict[str, Row], file: str, rid: str) -> tuple[str | None, str | None, str]:
    """파일명 정확 일치 행에 rid 기입(source=manual; 공란이면 공란 복귀). 반환 (오류, 경고, 기록된 rid).

    /admin 카드 입력칸(serve.api_admin_register_set)과 --set이 공유하는 유일한 쓰기 경로.
    `YYYY-00`(번호 미상)은 다음 빈 `-vN`을 붙여 기록하고 경고 문장으로 알린다.
    """
    file = norm_name(file)
    rid = (rid or "").strip()
    typed = rid
    if UNKNOWN_RE.match(rid):
        rid = next_unknown(table, rid, file)
    err, warn = validate_rid(table, file, rid)
    if err:
        return err, warn, typed
    if rid != typed:
        warn = f"번호 미상 규약: {typed} → {rid}(다음 빈 -vN)" + (f" · {warn}" if warn else "")
    row = table[file]
    row.report_id, row.source = rid, ("manual" if rid else "")
    return None, warn, rid


def apply_sets(table: dict[str, Row], specs: list[str]) -> int:
    """--set '파일명 일부=RID' 반영. 반환 0 = 전건 반영, 2 = 사용 오류(아무것도 안 바꿈)."""
    plan: list[tuple[str, str]] = []
    for spec in specs:
        if "=" not in spec:
            print(f"--set 형식 오류(파일명일부=RID): {spec}", file=sys.stderr)
            return 2
        key, rid = spec.rsplit("=", 1)
        key = unicodedata.normalize("NFC", key.strip())
        hits = [f for f in table if key in f]
        if len(hits) != 1:
            print(f"--set '{key}': 일치 파일 {len(hits)}건 — 유일해야 합니다"
                  + (": " + "; ".join(h[:50] for h in hits[:5]) if hits else ""), file=sys.stderr)
            return 2
        probe = {k: Row(v.file, v.report_id, v.source, v.cover_title, v.note) for k, v in table.items()}
        err, warn, _ = set_rid(probe, hits[0], rid.strip())  # 사본에 미리 적용해 검사만
        if err:
            print(f"--set '{key}': {err}", file=sys.stderr)
            return 2
        if warn:
            print(f"[경고] {warn}", file=sys.stderr)
        plan.append((hits[0], rid.strip()))
    for f, rid in plan:
        _, _, final = set_rid(table, f, rid)
        print(f"[set] {final or '(공란)'}: {f}", file=sys.stderr)
    return 0


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
    parser.add_argument("--registry", default=str(REGISTRY_PATH))
    parser.add_argument("--check", action="store_true", help="쓰기 없이 표↔pdfs 정합 검사만")
    parser.add_argument("--dry-run", action="store_true", help="추가될 행만 출력(쓰기 없음)")
    parser.add_argument("--set", action="append", default=[], metavar="파일명일부=RID",
                        help="표를 손으로 고치지 않고 기입: 파일명 일부(유일해야 함)=관리번호. 반복 가능. "
                             "빈 값(=)은 공란으로 되돌림")
    args = parser.parse_args()

    pdf_dir = Path(args.pdf_dir)
    if not pdf_dir.is_dir():
        print(f"디렉터리가 없습니다: {pdf_dir}", file=sys.stderr)
        sys.exit(2)
    reg_path = Path(args.registry)
    table = {r.file: r for r in read_rows(reg_path)}

    if args.set:
        code = apply_sets(table, args.set)
        if code:
            sys.exit(code)
        write_rows(list(table.values()), reg_path)
        print(f"== 등록부 {reg_path.name}: --set {len(args.set)}건 반영 ==")
        sys.exit(1 if print_issues(check_table(table, pdf_dir)) else 0)

    if args.check:
        print(f"== 등록부 {reg_path.name}: 행 {len(table)} ==")
        sys.exit(1 if print_issues(check_table(table, pdf_dir)) else 0)

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

    hints = md_sources(Path(args.reports_dir))
    added: list[Row] = []
    filled_title = 0
    for p in sorted(paths, key=lambda p: norm_name(p)):
        name = norm_name(p)
        if name in table:
            if not table[name].cover_title:
                table[name].cover_title = scan_cover(p).title
                filled_title += 1
            continue
        row = decide(name, scan_cover(p), table, hints.get(name))
        table[name] = row
        added.append(row)
        tag = row.report_id or "(공란)"
        print(f"[{'cover' if row.report_id else '기입 필요'}] {tag}: {name}"
              + (f" — {row.note}" if row.note else ""), file=sys.stderr)

    if not args.dry_run and (added or filled_title):
        write_rows(list(table.values()), reg_path)
        registry.load_registry(reg_path)  # 캐시 갱신(같은 프로세스 내 후속 조회용)

    rows = list(table.values())
    n_rid = sum(1 for r in rows if r.report_id)
    n_cover = sum(1 for r in rows if r.report_id and r.source in ("cover", ""))
    n_manual = sum(1 for r in rows if r.report_id and r.source == "manual")
    n_vol = sum(1 for r in rows if re.search(r"-v\d{1,2}(?:-[b-z])?$", r.report_id))
    n_dup = sum(1 for r in rows if re.search(r"-[b-z]$", r.report_id))
    print(f"== 등록부 {reg_path.name}{' (dry-run — 미기록)' if args.dry_run else ''} ==")
    print(f"행 {len(rows)} (이번 추가 {len(added)}) · rid 확정 {n_rid} (cover {n_cover} · manual {n_manual}"
          f" · -vN {n_vol} · -b {n_dup}) · 공란 {len(rows) - n_rid}")
    sys.exit(1 if print_issues(check_table(table, pdf_dir)) else 0)


if __name__ == "__main__":
    main()
