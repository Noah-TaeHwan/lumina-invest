"""Stage 0 집계 함수 검증."""
import pandas as pd
import pytest

from lab.jev_gate import gate, stage0
from tests.lab.bars import MINUTE_US, T0_US

TH = {"latency_p99_ms": 5000, "failure_rate_max": 0.02, "schema_valid_min": 0.99,
      "repeat_agreement_min": 0.9, "dev_candidates_min": 300, "projected_cost_max_usd": 5.0}


def test_period_mask_boundaries():
    t_close = pd.Series([T0_US, T0_US + MINUTE_US, T0_US + 86_400_000_000, T0_US + 86_400_000_000 + MINUTE_US])
    assert stage0.period_mask(t_close, "2025-10-01", "2025-10-01").tolist() == [False, True, True, False]


def test_rule_stats():
    trades = pd.DataFrame({"bar": [1, 5], "exit_bar": [3, 7], "reason": ["take", "stop"],
                           "gross_ret": [0.02, -0.01], "net_ret": [0.0178, -0.0122]})
    s = stage0.rule_stats(10, trades)
    assert (s["candidates"], s["trades"], s["win_rate"]) == (10, 2, 0.5)
    assert s["net_compound"] == pytest.approx(1.0178 * 0.9878 - 1)
    assert s["reasons"] == {"take": 1, "stop": 1}


def test_rule_stats_empty():
    empty = pd.DataFrame(columns=["bar", "exit_bar", "reason", "gross_ret", "net_ret"])
    assert stage0.rule_stats(0, empty)["trades"] == 0


def test_choose_n_eligibility_and_tie():
    stats = {30: {"candidates": 900, "net_compound": 0.1}, 60: {"candidates": 500, "net_compound": 0.1},
             120: {"candidates": 200, "net_compound": 0.9}}
    assert stage0.choose_n(stats, 300) == 30
    with pytest.raises(ValueError):
        stage0.choose_n(stats, 1000)


def test_sample_indices_deterministic_sorted_capped():
    a = stage0.sample_indices(1000, 300, 7)
    assert a == stage0.sample_indices(1000, 300, 7) and a == sorted(a) and len(set(a)) == 300
    assert stage0.sample_indices(5, 300, 7) == [0, 1, 2, 3, 4]


def _rec(lat, ok=True, status=200, error=None, tokens=600, model="jev-1.13.0"):
    return {"latency_ms": lat, "ok": ok, "status": status, "error": error,
            "input_tokens": tokens if ok else 0, "model": model if ok else None}


def test_call_summary():
    recs = [_rec(100), _rec(200), _rec(300), _rec(10_000, ok=False, status=None, error="timeout")]
    s = stage0.call_summary(recs)
    assert s["calls"] == 4 and s["failure_rate"] == 0.25 and s["schema_valid_rate"] == 1.0
    assert s["p50_ms"] == 250 and s["mean_input_tokens"] == 600
    assert s["models"] == ["jev-1.13.0"] and s["errors"] == {"timeout": 1}


def test_repeat_agreement():
    out = stage0.repeat_agreement({"a": [0.6, 0.7, 0.4, 0.8, 0.9], "b": [0.1] * 5})
    assert out["items"] == 2 and out["agreement"] == pytest.approx(0.9)


def test_project_full_run():
    p = stage0.project_full_run(10_000, 700, 360)
    assert p["cost_usd"] == pytest.approx(10_000 * 700 * gate.PRICE_PER_INPUT_TOKEN)
    assert p["hours_sequential"] == pytest.approx(1.0)


def _summary(**over):
    s = {"calls": {"p99_ms": 900, "failure_rate": 0.0, "schema_valid_rate": 1.0},
         "repeat": {"agreement": 0.95}, "dev_candidates": 400, "projection": {"cost_usd": 0.5}}
    s.update(over)
    return s


def test_evaluate_go_boundaries():
    assert all(stage0.evaluate_go(_summary(), TH).values())
    go = stage0.evaluate_go(_summary(dev_candidates=299, repeat={"agreement": 0.89}), TH)
    assert go["dev_candidates"] is False and go["repeat_agreement"] is False and go["latency_p99"] is True


def test_render_report_verdicts():
    base = {"n_selected": 60, "rule": {"60": {"candidates": 400, "trades": 300, "net_compound": -0.1,
                                              "net_mean": -0.001, "gross_mean": 0.0005, "win_rate": 0.4,
                                              "reasons": {"stop": 1}}},
            "candidate_counts": {"dev": 400}, "sessions": [], "by_session": {},
            "calls": {"calls": 500, "p50_ms": 300, "p95_ms": 600, "p99_ms": 900, "failure_rate": 0.0,
                      "schema_valid_rate": 1.0, "mean_input_tokens": 700, "models": ["jev-1.13.0"], "errors": {}},
            "repeat": {"items": 50, "agreement": 0.95, "mean_std": 0.01}, "dev_candidates": 400,
            "projection": {"calls": 600, "cost_usd": 0.02, "hours_sequential": 0.05}, "complete": True}
    go = stage0.evaluate_go(base, TH)
    assert "판정: GO" in stage0.render_report(base, go, TH)
    assert "판정: PARTIAL" in stage0.render_report({**base, "complete": False}, go, TH)
    assert "판정: NO-GO" in stage0.render_report(base, {**go, "dev_candidates": False}, TH)
