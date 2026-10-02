# lab/evidence/metrics.py
"""Stage 0 지표: AUC, Cohen κ, 2단 군집 부트스트랩(군집→질문), Clopper–Pearson 단측 상한."""
from __future__ import annotations

import numpy as np
from scipy.stats import beta
from sklearn.metrics import cohen_kappa_score, roc_auc_score

SEED = 20261002


def auc(y: list[int], s: list[float]) -> float | None:
    """라벨이 한 종류뿐이면 None."""
    return None if len(set(y)) < 2 else float(roc_auc_score(y, s))


def kappa(a: list, b: list) -> float:
    """두 라벨러의 Cohen κ."""
    return float(cohen_kappa_score(a, b))


def cluster_bootstrap(rows: list[dict], stat, n: int = 2000, seed: int = SEED):
    """군집을 복원추출하고, 뽑힌 군집 안에서 질문을 복원추출한다. 통계가 None인 반복은 버린다."""
    point = stat(rows)
    by_c: dict = {}
    for r in rows:
        by_c.setdefault(r["cluster"], {}).setdefault(r["qid"], []).append(r)
    keys = sorted(by_c)
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(n):
        sample: list[dict] = []
        for ci in rng.integers(0, len(keys), len(keys)):
            qs = by_c[keys[ci]]
            qkeys = sorted(qs)
            for qi in rng.integers(0, len(qkeys), len(qkeys)):
                sample.extend(qs[qkeys[qi]])
        v = stat(sample)
        if v is not None:
            vals.append(v)
    if not vals:
        return point, None, None
    return point, float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))


def cp_upper(failures: int, n: int, alpha: float = 0.05) -> float:
    """실패율의 단측 (1-alpha) Clopper–Pearson 상한."""
    if failures >= n:
        return 1.0
    return float(beta.ppf(1 - alpha, failures + 1, n - failures))


def percentile(values: list[float], q: float) -> float:
    """백분위수(numpy 기본 선형 보간)."""
    return float(np.percentile(values, q))
