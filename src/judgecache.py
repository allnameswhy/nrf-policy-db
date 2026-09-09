"""판정 캐시 — verify(판정·승급)와 annotate(FAIL 재생성)가 주고받는 핸드오프 저장소 (2026-09-09).

`logs/judge_cache.json` = {stem: {hid: entry}} — 파생물(git 제외, 없으면 재판정할 뿐).
entry 필드:
  key           sha256(판정 프롬프트 전문 = 근거 본문+요약+제목·위치). 모델 무관 — Sonnet 승급
                판정을 이후 Haiku 실행이 재사용한다. 옛 형식(sha256("모델\n프롬프트"),
                2026-09-03~08)은 is_hit이 적중으로 인정하고 verify가 새 키로 바꿔 쓴다.
  summary_sha   sha256(판정한 요약 텍스트) — annotate가 "지금 파일의 이 요약에 대한 FAIL인가"를 대조.
  verdict issues model judged_at   최종 판정(ERROR는 저장 안 함). 사람 확인은 model "human".
  fail_streak   서로 다른 요약에 대한 연속 FAIL 수(같은 요약 재판정은 불변). PASS/WARN → 0.
  fail_history  최근 ≤3회 FAIL의 issues(중재 입력·/admin 수동 카드 표시).
  escalated     "" | "judge" | "summary" — 이번 FAIL 연속 구간에서 올린 방향. PASS/WARN → 소거.
  regen_model   중재가 요약 오류로 본 뒤 verify가 설정 — annotate가 그 모델로 재생성하고 소거.
  arbiter       {fault, note, model, at} 마지막 중재 출력.
  manual        승급 재생성 후에도 FAIL — 수동 검토 표식(/admin 카드). PASS/WARN → 소거.

잔여 호출 산식(plan)의 단일 소스 — annotate --scan · verify.annotate_complete · status.py가 공유.
사람 확인(frontmatter `summary_reviewed`, mdio)은 요약이 있는 hid에만 유효하며 재생성·수동
집합에서 제외된다. stdlib + mdio만 import(annotate가 verify를 import하지 않게).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

import mdio

DEFAULT_PATH = "logs/judge_cache.json"
VERDICTS = ("PASS", "WARN", "FAIL")


def judge_key(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def legacy_key(prompt: str, model: str) -> str:
    """2026-09-08까지의 키 형식(모델 포함) — 기존 엔트리 적중 인정용."""
    return hashlib.sha256(f"{model}\n{prompt}".encode("utf-8")).hexdigest()


def summary_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load(path: str) -> dict:
    """없거나 손상이면 빈 dict."""
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def save(path: str, cache: dict) -> None:
    """원자적 저장(tmp → replace) — 한도 중단 중에도 부분 진행도가 깨지지 않게."""
    if not path:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def is_hit(ent: dict | None, prompt: str, model: str) -> bool:
    """같은 판정 입력의 최종 verdict가 있는가(새 키 또는 옛 키)."""
    if not ent or ent.get("verdict") not in VERDICTS:
        return False
    k = ent.get("key")
    return k == judge_key(prompt) or k == legacy_key(prompt, model)


def _current_fail(ent: dict | None, text: str) -> bool:
    return bool(ent) and ent.get("verdict") == "FAIL" and ent.get("summary_sha") == summary_sha(text)


def _scope(existing: dict, units, reviewed) -> list[str]:
    reviewed = set(reviewed)
    keys = [u.hid for u in units] + [mdio.REPORT_KEY]
    return [h for h in keys if h in existing and h not in reviewed]


def regen_set(bucket: dict, existing: dict, units, reviewed=()) -> set[str]:
    """annotate가 다시 써야 할 hid — 현재 요약 텍스트에 대한 FAIL이 캐시에 있고 수동 검토가 아닌 것."""
    return {h for h in _scope(existing, units, reviewed)
            if _current_fail(bucket.get(h), existing[h]) and not bucket.get(h, {}).get("manual")}


def manual_set(bucket: dict, existing: dict, units, reviewed=()) -> set[str]:
    """수동 검토 대기 hid — 현재 요약 텍스트에 대한 FAIL + manual 표식."""
    return {h for h in _scope(existing, units, reviewed)
            if _current_fail(bucket.get(h), existing[h]) and bucket.get(h, {}).get("manual")}


@dataclass
class Plan:
    units_todo: list[str]  # 요약 없음 ∪ FAIL 재생성 (사람 확인 제외)
    report_todo: bool
    regen: set[str]  # 그중 FAIL 재생성분(보고서 요약 키 포함 가능)
    reviewed_kept: int  # 요약이 있는 사람 확인 hid 수(재생성 제외)

    @property
    def calls(self) -> int:
        return len(self.units_todo) + (1 if self.report_todo else 0)


def plan(units, existing: dict, regen=(), reviewed=()) -> Plan:
    """잔여 호출 산식 — 유닛 요약 없음·재생성 대상 + 보고서 요약(없거나 유닛이 바뀌면 재생성).
    사람 확인은 요약이 있는 hid에만 유효(지워졌으면 결측으로 다시 만든다)."""
    regen = set(regen)
    reviewed = {h for h in reviewed if h in existing}
    units_todo = [u.hid for u in units
                  if u.hid not in existing or (u.hid in regen and u.hid not in reviewed)]
    report_todo = bool(units) and mdio.REPORT_KEY not in reviewed and (
        mdio.REPORT_KEY not in existing or bool(units_todo) or mdio.REPORT_KEY in regen)
    return Plan(units_todo, report_todo, regen - reviewed, len(reviewed))


def plan_for(st: mdio.ReportState, bucket: dict | None) -> Plan:
    """ReportState + 캐시 버킷(없으면 {})으로 계획 — status·annotate --scan·verify가 공유."""
    bucket = bucket or {}
    reviewed = st.fm.summary_reviewed
    return plan(st.units, st.existing, regen_set(bucket, st.existing, st.units, reviewed), reviewed)


def settle(bucket: dict, hid: str, *, key: str, sha: str, verdict: str, issues: list,
           model: str, now: str) -> dict:
    """새 판정을 엔트리에 반영(순수 갱신). FAIL: 같은 요약 재판정 → streak 불변, 재생성 후
    재실패 → +1, 그 외 → 1. PASS/WARN: streak 0·이력 소거·escalated/regen_model/manual 소거."""
    prev = bucket.get(hid) or {}
    ent = dict(prev)
    ent.update(key=key, summary_sha=sha, verdict=verdict, issues=issues, model=model, judged_at=now)
    if verdict == "FAIL":
        if prev.get("verdict") == "FAIL" and prev.get("summary_sha") == sha:
            streak = int(prev.get("fail_streak") or 1)
        elif prev.get("verdict") == "FAIL":
            streak = int(prev.get("fail_streak") or 0) + 1
        else:
            streak = 1
        ent["fail_streak"] = streak
        ent["fail_history"] = (list(prev.get("fail_history") or []) + [issues])[-3:]
    else:
        ent["fail_streak"] = 0
        ent["fail_history"] = []
        for k in ("escalated", "regen_model", "manual"):
            ent.pop(k, None)
    bucket[hid] = ent
    return ent
