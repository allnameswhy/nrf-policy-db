"""reports/*.md 공용 파서 — frontmatter · 헤딩 트리 · 유닛 분할 · 요약 블록 삽입/제거.

annotate.py / build_db.py / build_index.py가 공유한다. 파싱 규칙이 사본으로
흩어지면 "재파싱해도 동일 ID" 규약이 드리프트로 깨질 수 있어 한곳에 둔다.

좌표계 주의: 헤딩·frontmatter의 줄 번호는 파싱에 사용한 줄 리스트 기준이다.
요약 블록이 있는 원본과 strip_summary_blocks() 결과(base)는 줄 번호가 다르므로,
insert_summaries()에는 반드시 base를 파싱해 얻은 객체를 넘겨야 한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# 헤딩 줄: extract.py render_markdown()의 f"{'#'*depth} {text} <!-- id: {hid} -->"
HEADING_RE = re.compile(r"^(#{1,4}) (.*?) <!-- id: ([^>]+?) -->$")
# LLM 생성 요약 블록 (annotate.py 삽입물 — 인용 시 제외 대상)
SUMMARY_RE = re.compile(r"^> \*\*(?:보고서 )?요약:\*\* ")
UNIT_SUMMARY_PREFIX = "> **요약:** "
REPORT_SUMMARY_PREFIX = "> **보고서 요약:** "
REPORT_KEY = "__report__"  # find/insert_summaries dict에서 보고서 요약의 키


def is_excluded_chapter(text: str) -> bool:
    """참고문헌·부록·첨부 장 판정 — 요약 생성 제외 대상 (# 레벨에만 적용).

    코퍼스 실측상 "참 고 문 헌", "부 록", "[참고문헌]", "<부록 1>", "부록. …"
    같은 표기 변형이 있어 비문자를 걷어낸 뒤 판정한다.
    """
    t = re.sub(r"[^0-9A-Za-z가-힣]", "", text)
    return "참고문헌" in t or t.startswith("부록") or t.startswith("첨부")


# ---- 파일 IO ----


def read_md_lines(path: str | Path) -> list[str]:
    """말미 개행 1개를 벗긴 줄 리스트. write_md_lines()와 왕복 무손실."""
    text = Path(path).read_text(encoding="utf-8")
    if text.endswith("\n"):
        text = text[:-1]
    return text.split("\n")


def write_md_lines(path: str | Path, lines: list[str]) -> None:
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


# ---- frontmatter ----


@dataclass
class Frontmatter:
    report_id: str
    title: str
    abstract_empty: bool
    end_line: int  # 닫는 '---'의 줄 인덱스


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return v


def parse_frontmatter(lines: list[str]) -> Frontmatter:
    """완전한 YAML 파서가 아니다 — extract.py가 수기 직렬화하는 고정 키만 읽는다."""
    if not lines or lines[0] != "---":
        raise ValueError("frontmatter가 없습니다 (첫 줄이 '---'가 아님)")
    report_id = ""
    title = ""
    abstract_empty = True
    end_line = -1
    for i in range(1, len(lines)):
        line = lines[i]
        if line == "---":
            end_line = i
            break
        if line.startswith("report_id:"):
            report_id = line.split(":", 1)[1].strip()
        elif line.startswith("title:"):
            title = _unquote(line.split(":", 1)[1])
        elif line.startswith("abstract:"):
            val = line.split(":", 1)[1].strip()
            # extract.py: 내용 있으면 'abstract: |' 블록, 공란이면 'abstract: ""'
            abstract_empty = val in ('""', "''", "")
    if end_line < 0:
        raise ValueError("frontmatter 닫는 '---'가 없습니다")
    return Frontmatter(report_id, title, abstract_empty, end_line)


# ---- 헤딩 트리 ----


@dataclass
class Heading:
    depth: int
    text: str
    hid: str
    line: int  # 헤딩 줄 인덱스
    children: list["Heading"] = field(default_factory=list)
    end_line: int = -1  # subtree 끝 (exclusive)
    body_end: int = -1  # 직속 본문 끝 (exclusive) = 다음 헤딩 줄(레벨 무관)


def parse_heading_tree(lines: list[str]) -> list[Heading]:
    fm_end = 0
    if lines and lines[0] == "---":
        for i in range(1, len(lines)):
            if lines[i] == "---":
                fm_end = i + 1
                break
    flat: list[Heading] = []
    for j in range(fm_end, len(lines)):
        m = HEADING_RE.match(lines[j])
        if m:
            flat.append(Heading(depth=len(m.group(1)), text=m.group(2), hid=m.group(3), line=j))
    roots: list[Heading] = []
    stack: list[Heading] = []
    for h in flat:
        while stack and stack[-1].depth >= h.depth:
            stack.pop()
        (stack[-1].children if stack else roots).append(h)
        stack.append(h)
    n = len(lines)
    open_stack: list[Heading] = []
    for h in flat:
        while open_stack and open_stack[-1].depth >= h.depth:
            open_stack.pop().end_line = h.line
        open_stack.append(h)
    for h in open_stack:
        h.end_line = n
    for k, h in enumerate(flat):
        h.body_end = flat[k + 1].line if k + 1 < len(flat) else n
    return roots


def iter_headings(roots: list[Heading]):
    for r in roots:
        yield r
        yield from iter_headings(r.children)


# ---- 유닛 분할 (PROJECT_NOTES §3 단계② 확정 규칙) ----


@dataclass
class Unit:
    hid: str
    kind: str  # "subtree" | "intro"
    depth: int
    heading_line: int
    heading_path: list[str]  # 루트→자신 헤딩 텍스트 (프롬프트 문맥용)
    start: int  # 본문 시작 줄 (헤딩 다음 줄)
    end: int  # exclusive
    char_count: int


def _span_chars(lines: list[str], start: int, end: int) -> int:
    """요약 블록 줄을 제외한 글자 수 — 삽입 후 재실행에도 동일 분할(멱등)을 보장.

    제외한 요약 줄의 짝인 빈 줄은 남지만 strip()이 양끝만 걷어내므로,
    블록이 항상 구간 선두(헤딩 직후)에 오는 규약상 계산값은 삽입 전과 동일하다.
    """
    parts = [ln for ln in lines[start:end] if not SUMMARY_RE.match(ln)]
    return len("\n".join(parts).strip())


def split_units(
    roots: list[Heading],
    lines: list[str],
    *,
    min_chars: int = 200,
    max_chars: int = 4000,
    stats: dict | None = None,
) -> list[Unit]:
    """크기 기반 헤딩 트리 분할.

    - # 레벨 참고문헌·부록·첨부 장은 subtree 통째 제외
    - subtree_chars <= max_chars 또는 leaf → subtree 전체가 유닛 1개
    - 초과 시: 직속 본문 >= min_chars면 intro 유닛 채택 후 자식으로 재귀
    - min_chars 미만 조각은 스킵 (실측 미커버 0.07%)

    subtree_chars는 직속 본문들의 합(헤딩 줄 자체는 불포함) — 시뮬레이션 기준과 동일.
    """
    if stats is not None:
        stats.setdefault("uncovered_chars", 0)
        stats.setdefault("excluded_chapter_chars", 0)
    units: list[Unit] = []
    subtree_cache: dict[int, int] = {}

    def direct_chars(h: Heading) -> int:
        return _span_chars(lines, h.line + 1, h.body_end)

    def subtree_chars(h: Heading) -> int:
        key = id(h)
        if key not in subtree_cache:
            subtree_cache[key] = direct_chars(h) + sum(subtree_chars(c) for c in h.children)
        return subtree_cache[key]

    def walk(h: Heading, path: list[str]) -> None:
        sub = subtree_chars(h)
        my_path = path + [h.text]
        if sub <= max_chars or not h.children:
            if sub >= min_chars:
                units.append(Unit(h.hid, "subtree", h.depth, h.line, my_path, h.line + 1, h.end_line, sub))
            elif stats is not None:
                stats["uncovered_chars"] += sub
            return
        direct = direct_chars(h)
        if direct >= min_chars:
            units.append(Unit(h.hid, "intro", h.depth, h.line, my_path, h.line + 1, h.body_end, direct))
        elif stats is not None:
            stats["uncovered_chars"] += direct
        for c in h.children:
            walk(c, my_path)

    for r in roots:
        if r.depth == 1 and is_excluded_chapter(r.text):
            if stats is not None:
                stats["excluded_chapter_chars"] += subtree_chars(r)
            continue
        walk(r, [])
    return units


def unit_text(unit: Unit, lines: list[str]) -> str:
    """유닛 본문 (LLM 입력용). 요약 블록 제외, 하위 헤딩 줄은 id 주석만 제거해 유지."""
    out: list[str] = []
    for ln in lines[unit.start : unit.end]:
        if SUMMARY_RE.match(ln):
            continue
        m = HEADING_RE.match(ln)
        if m:
            out.append(f"{m.group(1)} {m.group(2)}")
        else:
            out.append(ln)
    return "\n".join(out).strip()


# ---- 요약 블록 제거/탐지/삽입 ----


def strip_summary_blocks(lines: list[str]) -> list[str]:
    """요약 블록 줄 + 직후 빈 줄 제거 — insert_summaries()의 정확한 역연산."""
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if SUMMARY_RE.match(lines[i]):
            i += 1
            if i < n and lines[i] == "":
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return out


def find_existing_summaries(lines: list[str], roots: list[Heading], fm: Frontmatter) -> dict[str, str]:
    """{hid: 요약문, REPORT_KEY: 보고서 요약문}. 인자는 같은 좌표계(원본) 기준."""
    found: dict[str, str] = {}
    for h in iter_headings(roots):
        for j in range(h.line + 1, min(h.line + 3, len(lines))):
            if lines[j].startswith(UNIT_SUMMARY_PREFIX):
                found[h.hid] = lines[j][len(UNIT_SUMMARY_PREFIX) :]
                break
            if lines[j] != "":
                break
    first_heading = min((h.line for h in roots), default=len(lines))
    for j in range(fm.end_line + 1, first_heading):
        if lines[j].startswith(REPORT_SUMMARY_PREFIX):
            found[REPORT_KEY] = lines[j][len(REPORT_SUMMARY_PREFIX) :]
            break
    return found


def insert_summaries(
    base_lines: list[str],
    headings_by_hid: dict[str, Heading],
    fm: Frontmatter,
    summaries: dict[str, str],
) -> list[str]:
    """스트립된 base에 요약 dict 전체를 재삽입한 새 줄 리스트를 만든다.

    삽입 위치: 유닛 요약은 헤딩 줄+2(헤딩·빈 줄 다음), 보고서 요약은
    frontmatter 닫는 '---'+2(빈 줄 다음). 내림차순 삽입으로 오프셋 추적이 필요 없다.
    결과는 §4 규약(헤딩 – 빈 줄 – 블록 – 빈 줄 – 본문)을 만족한다.
    """
    inserts: list[tuple[int, list[str]]] = []
    for hid, text in summaries.items():
        if hid == REPORT_KEY:
            pos = fm.end_line + 2
            block = [REPORT_SUMMARY_PREFIX + text, ""]
        else:
            h = headings_by_hid[hid]
            if h.line + 1 < len(base_lines) and base_lines[h.line + 1] != "":
                raise ValueError(f"헤딩 {hid} 다음 줄이 빈 줄이 아님 — 규약 위반 파일")
            pos = h.line + 2
            block = [UNIT_SUMMARY_PREFIX + text, ""]
        inserts.append((pos, block))
    out = list(base_lines)
    for pos, block in sorted(inserts, key=lambda t: t[0], reverse=True):
        out[pos:pos] = block
    return out
