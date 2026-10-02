# app/services/evidence/judge.py
"""주장 하나와 문단 k개로 JEV Choice 요청을 만들고 문단별 지지·반박 확률을 받는다.

Stage 0은 JEV 단독 점수(문단별 지지 확률 최댓값)만 쓴다. 숫자 대조와 SYS 규칙은 Stage 1에서 붙인다.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

INSTRUCTIONS = ("The state has one [Claim] about a Korean listed company and numbered [Passage] blocks "
                "from its annual report. How does [Passage {j}] relate to the [Claim]? "
                "Judge only from that passage, not from outside knowledge.")
CRITERIA = {
    "supports": "The passage states every fact in the claim, possibly in other words, "
                "for the same company, period and metric.",
    "contradicts": "The passage states something about the same company, period and metric "
                   "that conflicts with the claim.",
    "says_nothing": "The passage neither states nor conflicts with the claim's facts, "
                    "or it covers only part of them.",
}
QUESTION_SHA = hashlib.sha256(json.dumps([INSTRUCTIONS, CRITERIA], ensure_ascii=False,
                                         sort_keys=True).encode()).hexdigest()


@dataclass
class Judgement:
    """문단별 지지(s)·반박(c) 확률. ok=False면 모두 0이다."""

    s: list[float]
    c: list[float]
    ok: bool
    requests: int


def build_state(company: str, claim: str, passages: list[str]) -> str:
    """[Company]·[Claim]·[Passage n] 줄로 된 state."""
    lines = [f"[Company] {company}", f"[Claim] {claim}"]
    lines += [f"[Passage {j}] {t}" for j, t in enumerate(passages, 1)]
    return "\n".join(lines)


def build_questions(n: int, only: list[int] | None = None) -> dict:
    """문단마다 Choice 질문 하나(p1..pn). only가 있으면 그 문단만."""
    return {f"p{j}": {"type": "choice", "instructions": INSTRUCTIONS.format(j=j), "criteria": CRITERIA}
            for j in (only or range(1, n + 1))}


def judge_claim(client, company: str, claim: str, passages: list[str], *, use_cache: bool = True,
                tag: str = "", single: bool = False) -> Judgement:
    """묶음 모드는 요청 1회, single 모드는 같은 state로 문단마다 요청 1회. 하나라도 실패하면 전부 0."""
    n = len(passages)
    state = build_state(company, claim, passages)
    groups = [[j] for j in range(1, n + 1)] if single else [list(range(1, n + 1))]
    s, c = [0.0] * n, [0.0] * n
    for g in groups:
        r = client.ask(state, build_questions(n, g), use_cache=use_cache, tag=tag)
        if not r.ok:
            return Judgement([0.0] * n, [0.0] * n, False, len(groups))
        for j in g:
            p = r.answers[f"p{j}"]
            s[j - 1], c[j - 1] = p["supports"], p["contradicts"]
    return Judgement(s, c, True, len(groups))


def jev_score(j: Judgement) -> float:
    """JEV 단독 점수: 문단별 지지 확률 최댓값. 실패면 0."""
    return max(j.s) if j.ok and j.s else 0.0


def passage_labels(j: Judgement) -> list[str]:
    """문단별 확률 최댓값 선택지(반복·묶기 일치 검사용)."""
    return [max((("supports", s), ("contradicts", c), ("says_nothing", 1 - s - c)), key=lambda x: x[1])[0]
            for s, c in zip(j.s, j.c)]


def sys_decision(j: Judgement, valid: list[bool], tau_s: float, tau_c: float) -> tuple[str, int | None, float]:
    """spec 3절 SYS 규칙. valid는 문단별 숫자 존재 확인 통과 여부다.

    반박: max c ≥ τ_c이고 max c > S_V(숫자 확인을 통과한 문단의 지지 최댓값)일 때. 점수 0.
    지지됨: S_V ≥ τ_s일 때, 그 문단이 출처. 점수 S_V. 나머지는 근거 없음(점수 S_V).
    """
    if not j.ok:
        return "unjudged", None, 0.0
    cand = [(s, i) for i, (s, v) in enumerate(zip(j.s, valid)) if v]
    s_v, idx = max(cand) if cand else (0.0, None)
    mc = max(j.c)
    if mc >= tau_c and mc > s_v:
        return "contradicted", j.c.index(mc), 0.0
    if idx is not None and s_v >= tau_s:
        return "supported", idx, s_v
    return "no_evidence", None, s_v
