# tests/evidence/test_metrics.py
from lab.evidence import metrics


def test_auc_and_kappa():
    assert metrics.auc([0, 0, 1, 1], [0.1, 0.4, 0.35, 0.8]) == 0.75
    assert metrics.auc([1, 1], [0.1, 0.2]) is None
    assert metrics.kappa([1, 0, 1, 0], [1, 0, 1, 0]) == 1.0


def test_cluster_bootstrap_is_reproducible_and_brackets_point():
    rows = [{"cluster": c, "qid": f"{c}-{q}", "y": (c + q + i) % 2, "s": ((c + q + i) % 2) * 0.6 + 0.2 * i}
            for c in range(6) for q in range(3) for i in range(3)]
    stat = lambda rs: metrics.auc([r["y"] for r in rs], [r["s"] for r in rs])
    a = metrics.cluster_bootstrap(rows, stat, n=200)
    assert a == metrics.cluster_bootstrap(rows, stat, n=200)
    assert a[1] <= a[0] <= a[2]


def test_cp_upper_matches_rule_of_three_region():
    assert 0.0295 < metrics.cp_upper(0, 100) < 0.0300
    assert metrics.cp_upper(5, 5) == 1.0


def test_percentile():
    assert metrics.percentile([1, 2, 3, 4, 5], 50) == 3
