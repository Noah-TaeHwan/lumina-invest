"""투자성향 진단 점수화 · 목표 달성 확률 시뮬레이션."""
import pytest

from app.services.robo_profile import QUESTIONS, goal_simulation, score_answers


def _answers(idx):
    return {q["id"]: idx for q in QUESTIONS}


def test_profile_levels_by_score():
    assert score_answers(_answers(0))["level"] == "안정형"          # 7점
    assert score_answers(_answers(2))["risk_profile"] == "moderate"   # 21점 → 위험중립형
    r = score_answers(_answers(4))                                    # 연령(역순) 1점 + 나머지 5점 = 31점
    assert r["level"] == "공격투자형" and r["risk_profile"] == "aggressive"
    best = {q["id"]: max(range(len(q["options"])), key=lambda i: q["options"][i]["score"]) for q in QUESTIONS}
    assert score_answers(best)["score"] == score_answers(best)["max_score"]


def test_profile_requires_all_answers():
    a = _answers(2); del a["loss"]
    with pytest.raises(ValueError):
        score_answers(a)
    with pytest.raises(ValueError):
        score_answers({**_answers(2), "age": 9})


def test_goal_simulation_deterministic_when_no_volatility():
    r = goal_simulation(1000, 5, target_return_pct=5, expected_return_pct=8, expected_volatility_pct=0, n_paths=500)
    assert r["probability_pct"] == 100.0
    assert r["percentiles_manwon"]["p50"] == pytest.approx(1000 * 1.08 ** 5, rel=1e-3)
    r2 = goal_simulation(1000, 5, target_return_pct=10, expected_return_pct=8, expected_volatility_pct=0, n_paths=500)
    assert r2["probability_pct"] == 0.0


def test_goal_simulation_with_contributions_and_volatility():
    r = goal_simulation(1000, 3, target_return_pct=6, expected_return_pct=7, expected_volatility_pct=15,
                        monthly_contribution_manwon=50, n_paths=2000)
    assert r["invested_manwon"] == 1000 + 50 * 36
    assert 0 < r["probability_pct"] < 100
    p = r["percentiles_manwon"]
    assert p["p5"] <= p["p25"] <= p["p50"] <= p["p75"] <= p["p95"]
    assert len(r["curves"]["p50"]) == len(r["curve_years"])
    assert r["required_annual_return_pct"] > 0
