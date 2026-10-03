# tests/evidence/test_a2_tier.py
"""A-2 확인 세트 관문(spec 6.1·6.4절): SYS 단독과 계층형 경로 집계, 정책 a2-v1 매핑."""
import pytest

from lab.evidence import a2

CFG = {"tau_s": 0.7, "tau_c": 0.35, "theta_low": 0.2, "theta_high": 0.8}


def _row(i, *, s, y, lex, high_ok=True, cluster=None):
    return {"cid": f"c{i}", "qid": f"q{i % 12}", "cluster": i % 6 if cluster is None else cluster,
            "s": [s], "c": [0.0], "valid": [True], "y": int(y), "label": "supported" if y else "no_evidence",
            "lex": lex, "high_ok": high_ok}


def test_route_rows_follow_product_tier_function():
    rows = [_row(0, s=0.9, y=1, lex=0.1), _row(1, s=0.9, y=1, lex=0.9), _row(2, s=0.9, y=1, lex=0.9, high_ok=False),
            _row(3, s=0.5, y=0, lex=0.5)]
    out = a2.decide(rows, CFG)
    assert [r["route"] for r in out] == ["lex_low", "lex_high", "jev", "jev"]
    assert [r["tier_decision"] for r in out] == ["no_evidence", "supported", "supported", "no_evidence"]
    assert [r["sys_decision"] for r in out] == ["supported", "supported", "supported", "no_evidence"]
    assert [r["tier_score"] for r in out] == [0.0, 1.0, 0.9, 0.5]
    assert a2.decide(rows, dict(CFG, theta_low=None, theta_high=None))[0]["route"] == "jev"


def test_jev_call_reduction_over_pool():
    pool = [{"lex": 0.1, "high_ok": True}] * 3 + [{"lex": 0.9, "high_ok": True}] * 2 + [{"lex": 0.5, "high_ok": True}] * 5
    assert a2.call_reduction(pool, 0.2, 0.8) == pytest.approx(0.5)
    assert a2.call_reduction(pool, 0.2, None) == pytest.approx(0.3)
    assert a2.call_reduction([], 0.2, 0.8) is None


def _check_rows():
    """240행: 지지 확률 높은 정답 180, 경계 오답 12, 하단 구간(lex 0.1, 라벨 지지 아님) 48."""
    rows = [_row(i, s=0.9, y=1, lex=0.9) for i in range(180)]
    rows += [_row(180 + i, s=0.75, y=0, lex=0.5) for i in range(12)]
    rows += [_row(192 + i, s=0.1, y=0, lex=0.1) for i in range(48)]
    return rows


def test_gates_pass_and_policy_uses_tuned_values():
    rows = _check_rows()
    pool = [{"lex": r["lex"], "high_ok": r["high_ok"]} for r in rows]
    g = a2.check_gates(rows, pool, CFG, n_boot=200)
    assert g["h_prec"]["predicted"] == 192 and g["h_prec"]["precision"] == pytest.approx(180 / 192)
    assert g["h_prec"]["descriptive_only"] is False and g["h_prec"]["pass"] is True
    assert g["h_low"]["band"] == 48 and g["h_low"]["supported_rate"] == 0 and g["h_low"]["pass"] is True
    assert g["h_high"]["band"] == 180 and g["h_high"]["precision"] == 1.0 and g["h_high"]["pass"] is True
    assert g["h_tier"]["call_reduction"] == pytest.approx(228 / 240) and g["h_tier"]["adopted"] == ["low", "high"]
    pol = a2.policy_a2_v1(g, CFG)
    assert pol == {"version": "a2-v1", "tau_s": 0.7, "tau_c": 0.35, "theta_low": 0.2, "theta_high": 0.8,
                   "precision_target_confirmed": True}


def test_fewer_than_150_predictions_is_descriptive_and_falls_back():
    rows = _check_rows()[:100] + _check_rows()[192:]
    pool = [{"lex": r["lex"], "high_ok": r["high_ok"]} for r in rows]
    g = a2.check_gates(rows, pool, CFG, n_boot=200)
    assert g["h_prec"]["descriptive_only"] is True and g["h_prec"]["pass"] is False
    assert a2.policy_a2_v1(g, CFG)["tau_s"] == 0.85


def test_h_low_fails_on_small_band_and_h_high_on_precision_drop():
    rows = _check_rows()[:192] + [_row(300 + i, s=0.1, y=0, lex=0.1) for i in range(10)]
    rows += [_row(400 + i, s=0.1, y=0, lex=0.5) for i in range(60)]
    rows += [_row(500 + i, s=0.9, y=0, lex=0.95) for i in range(20)]  # 상단 구간 오답(SYS도 ✅)
    pool = [{"lex": r["lex"], "high_ok": r["high_ok"]} for r in rows]
    g = a2.check_gates(rows, pool, CFG, n_boot=200)
    assert g["h_low"]["share"] < 0.20 and g["h_low"]["pass"] is False
    assert g["h_high"]["precision"] == pytest.approx(180 / 200) and g["h_high"]["pass"] is False
    pol = a2.policy_a2_v1(g, CFG)
    assert pol["theta_low"] is None and pol["theta_high"] is None
    assert g["h_tier"]["status"] == "not_applicable"


def test_missing_thresholds_mean_hypothesis_not_tested():
    rows = _check_rows()
    pool = [{"lex": r["lex"], "high_ok": r["high_ok"]} for r in rows]
    g = a2.check_gates(rows, pool, dict(CFG, theta_low=None, theta_high=None), n_boot=200)
    assert g["h_low"] == {"status": "not_tested", "pass": False}
    assert g["h_high"] == {"status": "not_tested", "pass": False}


def test_policy_mapping_table():
    g = {"h_prec": {"pass": False}, "h_low": {"pass": True}, "h_high": {"pass": False}}
    assert a2.policy_a2_v1(g, CFG) == {"version": "a2-v1", "tau_s": 0.85, "tau_c": 0.35, "theta_low": 0.2,
                                       "theta_high": None, "precision_target_confirmed": False}


def test_budget_guard():
    a2.check_budget(used=1_000_000, estimate=2_000_000, cap=6_000_000, reserve=2_000_000)
    with pytest.raises(SystemExit, match="budget"):
        a2.check_budget(used=1_000_000, estimate=3_000_000, cap=6_000_000, reserve=2_000_001)


def test_latency_summary_excludes_cold_start():
    rows = [{"latency_ms": 22600.0, "cold": True}] + [{"latency_ms": float(v), "cold": False} for v in range(10000, 20001, 1000)]
    got = a2.latency_summary(rows)
    assert got["n"] == 11 and got["cold_excluded"] == 1
    assert got["p50_ms"] == 15000.0 and got["timeout_s"] == pytest.approx(2 * got["p95_ms"] / 1000)


def test_not_claim_audit_by_rule():
    rows = [{"text": "문단에 따르면 매출은 늘었다.", "not_claim_rule": True, "label": "supported"},
            {"text": "확인할 수 없습니다.", "not_claim_rule": True, "label": "non_claim"},
            {"text": "무엇인가요?", "not_claim_rule": True, "label": "non_claim"},
            {"text": "x", "not_claim_rule": True, "label": "disputed"},
            {"text": "주요 제품은 다음과 같습니다.", "not_claim_rule": True, "label": "non_claim"},
            {"text": "회사는 반도체를 만든다.", "not_claim_rule": False, "label": "supported"}]
    got = a2.not_claim_audit(rows)
    assert got["flagged"] == 4 and got["claim_share"] == pytest.approx(1 / 4) and got["over_limit"] is True
    assert got["by_reason"]["phrase:문단에"] == {"n": 1, "claim": 1}
    assert got["by_reason"]["lead"] == {"n": 1, "claim": 0} and got["drop_phrases"] == ["문단에"]
