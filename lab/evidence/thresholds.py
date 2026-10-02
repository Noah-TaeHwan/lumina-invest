# lab/evidence/thresholds.py
"""조정 세트에서 SYS 임계값을 고른다(spec 3절 규칙 5): τ_c 먼저, 그다음 τ_s. 동률이면 큰 값."""
from __future__ import annotations

from app.services.evidence.judge import Judgement, sys_decision

GRID = [round(0.05 * i, 2) for i in range(1, 20)]


def _f1(tp: int, fp: int, fn: int) -> float:
    return 0.0 if tp == 0 else 2 * tp / (2 * tp + fp + fn)


def _decide(r: dict, tau_s: float, tau_c: float) -> str:
    return sys_decision(Judgement(r["s"], r["c"], True, 1), r["valid"], tau_s, tau_c)[0]


def choose_tau_c(rows: list[dict]) -> float:
    """반박 대 나머지 F1이 최대인 τ_c. 규칙 1은 τ_s와 무관하므로 τ_s=1.01로 둔다."""
    best = (-1.0, 0.0)
    for t in GRID:
        pred = [_decide(r, 1.01, t) == "contradicted" for r in rows]
        gold = [r["label"] == "contradicted" for r in rows]
        tp = sum(p and g for p, g in zip(pred, gold))
        fp = sum(p and not g for p, g in zip(pred, gold))
        fn = sum(g and not p for p, g in zip(pred, gold))
        best = max(best, (_f1(tp, fp, fn), t))
    return best[1]


def choose_tau_s(rows: list[dict], tau_c: float) -> float:
    """지지됨 정밀도 ≥ 0.90인 가장 작은 τ_s. 없으면 지지됨 F1 최대(동률이면 큰 값)."""
    stats = []
    for t in GRID:
        pred = [_decide(r, t, tau_c) == "supported" for r in rows]
        gold = [r["label"] == "supported" for r in rows]
        tp = sum(p and g for p, g in zip(pred, gold))
        fp = sum(p and not g for p, g in zip(pred, gold))
        fn = sum(g and not p for p, g in zip(pred, gold))
        stats.append((t, tp, fp, fn))
    for t, tp, fp, fn in stats:
        if tp + fp > 0 and tp / (tp + fp) >= 0.90:
            return t
    return max((_f1(tp, fp, fn), t) for t, tp, fp, fn in stats)[1]
