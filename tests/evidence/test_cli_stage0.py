# tests/evidence/test_cli_stage0.py
from lab.evidence import __main__ as cli


def test_subset_is_deterministic():
    cids = [f"x-q1-n{i}" for i in range(20)]
    assert cli.subset(cids, 5) == cli.subset(list(reversed(cids)), 5) and len(cli.subset(cids, 5)) == 5


def test_sessions_split_on_gap():
    t = ["2026-10-02T01:00:00+00:00", "2026-10-02T01:10:00+00:00", "2026-10-02T03:00:00+00:00"]
    assert [len(s) for s in cli.sessions(t)] == [2, 1]


def test_stage0_gates_pass_and_fail():
    ok = cli.stage0_gates(corpus=(40, 40), kappa=0.7, controlled_agree=0.9, auc=(0.8, 0.7, 0.9),
                          batch_agree=0.95, p95_ms=900.0, fail=(0, 400), n_sessions=2, repeat_agree=0.95)
    assert ok["go"] is True
    bad = cli.stage0_gates(corpus=(40, 40), kappa=0.5, controlled_agree=0.9, auc=(0.8, 0.45, 0.9),
                           batch_agree=0.95, p95_ms=900.0, fail=(0, 400), n_sessions=1, repeat_agree=0.95)
    assert bad["go"] is False and not bad["kappa"]["pass"] and not bad["h_ko"]["pass"] and not bad["latency_fail"]["pass"]


def test_latency_rows_keep_only_stage0_tags():
    calls = [{"attempt": a, "tag": t, "latency_ms": float(i)}
             for i, (a, t) in enumerate([(1, "check"), (2, "check"), (1, "repeat1"), (1, "repeat2"), (1, "repeat3"),
                                         (2, "repeat3"), (1, "single"), (2, "single"), (1, "tune"), (2, "tune"),
                                         (1, "holdout"), (2, "holdout")])]
    gated, single = cli.latency_rows(calls)
    assert [r["tag"] for r in gated] == ["check", "repeat1", "repeat2", "repeat3"]
    assert [r["latency_ms"] for r in single] == [6.0]
