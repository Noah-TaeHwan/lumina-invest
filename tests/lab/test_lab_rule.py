"""돌파 후보·청산 규칙 검증."""
import numpy as np
import pandas as pd
import pytest

from lab.jev_gate import features as ft
from lab.jev_gate import rule
from tests.lab.bars import MINUTE_US, T0_US, make_minute_bars


def _bars(closes) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    t = T0_US + np.arange(n, dtype=np.int64) * MINUTE_US
    vol = np.where(np.arange(n) % 2 == 0, 10.0, 20.0)
    return pd.DataFrame({"open_time": t, "open": closes, "high": closes + 0.5, "low": closes - 0.5,
                         "close": closes, "volume": vol, "taker_buy_base": vol * 0.5, "t_close": t + MINUTE_US})


def test_fresh_breakout_fires_once():
    c = rule.find_candidates(ft.compute_features(_bars([100.0] * 300 + [101.0, 102.0, 103.0])), n=30)
    assert c["bar"].tolist() == [300]
    assert c.loc[0, "breakout_margin_atr"] > 0


def test_bars_since_prev_signal_is_gap_then_capped():
    closes = [100.0] * 300 + [101.0] + [100.0] * 49 + [101.0] + [100.0] * 10
    c = rule.find_candidates(ft.compute_features(_bars(closes)), n=30)
    assert c["bar"].tolist() == [300, 350]
    assert c["bars_since_prev_signal"].tolist() == [240, 50]


@pytest.mark.parametrize("path,expected", [
    ([100.0, 99.0, 97.9], (2, "stop")),    # 진입 100, ATR 1 → 손절선 98
    ([100.0, 101.0, 103.1], (2, "take")),  # 익절선 103
])
def test_find_exit_stop_and_take(path, expected):
    assert rule.find_exit(np.array(path), 1, 100.0, 1.0) == expected


def test_find_exit_time_and_end():
    flat = np.full(rule.MAX_BARS + 50, 100.0)
    assert rule.find_exit(flat, 1, 100.0, 1.0) == (rule.MAX_BARS, "time")
    short = flat[: rule.MAX_BARS // 2]
    assert rule.find_exit(short, 1, 100.0, 1.0) == (len(short) - 1, "end")


def test_net_return_applies_fees_and_slippage():
    assert rule.net_return(100.0, 100.0, 0.0, 0.0) == 0.0
    expected = (110 * 0.9999) / (100 * 1.0001) * 0.999 ** 2 - 1
    assert rule.net_return(100.0, 110.0, 0.001, 1.0) == pytest.approx(expected)


def test_rule_only_holds_one_position():
    closes = [100.0] * 300 + [101.0] + [100.0] * 4 + [101.0] + [100.0] * 124 + [101.0] + [100.0] * 200
    f = ft.compute_features(_bars(closes))
    c = rule.find_candidates(f, n=3)
    trades = rule.rule_only_close_approx(f, c, fee_rate=0.001, slip_bps=1.0)
    assert c["bar"].tolist() == [300, 305, 430]
    assert trades["bar"].tolist() == [300, 430]          # 305는 보유 중이라 건너뜀
    assert trades["reason"].tolist() == ["time", "time"]
    assert (trades["net_ret"] < trades["gross_ret"]).all()


def test_candidates_are_causal():
    """뒤 봉을 잘라내거나 바꿔도 그 이전 후보(bar·특징 11개)는 같아야 한다."""
    bars, cut = make_minute_bars(900, seed=11), 700
    cols = ["bar", *ft.STATE_FEATURES]
    full = rule.find_candidates(ft.compute_features(bars), n=30)
    early = full[full["bar"] < cut][cols].reset_index(drop=True)
    part = rule.find_candidates(ft.compute_features(bars.iloc[:cut]), n=30)[cols]
    changed = bars.copy()
    changed.loc[cut:, ["high", "close", "volume"]] *= 1.05
    alt = rule.find_candidates(ft.compute_features(changed), n=30)
    assert len(early) > 0
    pd.testing.assert_frame_equal(early, part)
    pd.testing.assert_frame_equal(early, alt[alt["bar"] < cut][cols].reset_index(drop=True))
