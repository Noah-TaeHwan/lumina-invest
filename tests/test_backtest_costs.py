"""백테스트 비용(수수료·슬리피지)·손절·익절 반영 검증."""
import pandas as pd
import pytest

from app.services.quant_pipeline import apply_stops, backtest, feature_engineer, preprocess
from app.services.investment_research import backtest_strategy
from tests.conftest import make_candles


def _df():
    return feature_engineer(preprocess(make_candles(260)))


def test_costs_reduce_return_by_turnover():
    df = _df()
    sig = pd.Series(0, index=df.index); sig.iloc[10:30] = 1   # 1회 진입 + 1회 청산 = 회전 2회
    free = backtest(df, sig)
    paid = backtest(df, sig, commission_bps=10, slippage_bps=5)
    assert paid["trade_count"] == 2
    assert paid["cost_pct"] == pytest.approx(2 * 0.15, abs=1e-6)          # 15bp × 2
    assert paid["total_return_pct"] < free["total_return_pct"]
    assert paid["gross_return_pct"] == free["total_return_pct"]


def test_stop_loss_exits_and_blocks_until_reentry():
    idx = pd.date_range("2026-01-01", periods=8, freq="D")
    close = pd.Series([100, 100, 90, 95, 99, 100, 101, 102], index=idx, dtype=float)
    held = pd.Series([0, 1, 1, 1, 1, 1, 1, 1], index=idx, dtype=float)  # 2일차부터 계속 보유 신호
    out, n_sl, n_tp = apply_stops(close, held, stop_loss_pct=5)
    # 진입가 100(전일 종가) → 3일차 종가 90 ≤ 95 → 손절, 이후 신호가 계속 1이라 재진입 없음
    assert n_sl == 1 and n_tp == 0
    assert out.tolist() == [0, 1, 1, 0, 0, 0, 0, 0]


def test_take_profit_then_reentry_on_fresh_signal():
    idx = pd.date_range("2026-01-01", periods=8, freq="D")
    close = pd.Series([100, 100, 112, 113, 114, 110, 111, 112], index=idx, dtype=float)
    held = pd.Series([0, 1, 1, 1, 0, 0, 1, 1], index=idx, dtype=float)
    out, n_sl, n_tp = apply_stops(close, held, take_profit_pct=10)
    assert n_tp == 1
    assert out.tolist() == [0, 1, 1, 0, 0, 0, 1, 1]  # 익절 후 신호 0→1 재진입 허용


def test_no_stops_is_identity():
    idx = pd.date_range("2026-01-01", periods=5, freq="D")
    held = pd.Series([0, 1, 1, 0, 1], index=idx, dtype=float)
    out, a, b = apply_stops(pd.Series(range(5), index=idx, dtype=float), held)
    assert out.equals(held) and (a, b) == (0, 0)


def test_backtest_strategy_accepts_costs_and_stops():
    r = backtest_strategy(make_candles(400), strategy="ma", cost_bps=10, slippage_bps=5, stop_loss_pct=5, take_profit_pct=20)
    assert "error" not in r
    for k in ("gross_return_pct", "cost_pct", "stop_loss_exits", "take_profit_exits"):
        assert k in r
    assert r["total_return_pct"] <= r["gross_return_pct"] + 1e-9
