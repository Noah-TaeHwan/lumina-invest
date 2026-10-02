# tests/evidence/test_stage1_metrics.py
from lab.evidence import metrics


def test_mde_shrinks_with_more_clusters():
    rows = [{"cluster": c, "qid": f"{c}-{q}", "d": (c % 3) * 0.1 + q * 0.01} for c in range(19) for q in range(6)]
    stat = lambda rs: sum(r["d"] for r in rs) / len(rs)
    small, big = metrics.mde(rows, 4, stat, n=400), metrics.mde(rows, 40, stat, n=400)
    assert small > big > 0


def test_verdict_branches():
    assert metrics.verdict(0.01, 0.03) == {"statistical": "superior", "practically_equivalent": True}
    assert metrics.verdict(-0.2, -0.1) == {"statistical": "inferior", "practically_equivalent": False}
    assert metrics.verdict(-0.02, 0.08) == {"statistical": "inconclusive", "practically_equivalent": False}
    assert metrics.verdict(None, None)["statistical"] == "inconclusive"


def test_macro_f1():
    assert metrics.macro_f1(["a", "b"], ["a", "b"], ("a", "b")) == 1.0
