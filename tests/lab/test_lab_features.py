"""익명 특징 검증 — 미래 데이터 미사용, 알려진 값, 익명성."""
import json

import numpy as np
import pandas as pd

from lab.jev_gate import features as ft
from tests.lab.bars import MINUTE_US, T0_US, make_minute_bars

COLS = ["ret_1", "ret_5", "ret_15", "ret_60", "atr14", "atr_regime", "volume_z",
        "taker_buy_ratio", "range_pos_240", "trend_slope_atr"]


def _flat_bars(n: int = 300) -> pd.DataFrame:
    t = T0_US + np.arange(n, dtype=np.int64) * MINUTE_US
    vol = np.where(np.arange(n) % 2 == 0, 10.0, 20.0)
    return pd.DataFrame({"open_time": t, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                         "volume": vol, "taker_buy_base": vol * 0.4, "t_close": t + MINUTE_US})


def test_features_are_causal():
    bars = make_minute_bars(600)
    full = ft.compute_features(bars)
    part = ft.compute_features(bars.iloc[:450])
    for col in COLS:
        pd.testing.assert_series_equal(full[col].iloc[:450], part[col], check_names=False)


def test_known_values_on_flat_market():
    last = ft.compute_features(_flat_bars()).iloc[-1]
    assert last["ret_1"] == 0 and last["ret_60"] == 0
    assert last["atr14"] == 2.0 and last["atr_regime"] == 1.0
    assert last["range_pos_240"] == 0.5
    assert last["taker_buy_ratio"] == 0.4
    assert abs(last["trend_slope_atr"]) < 1e-9
    assert np.isfinite(last["volume_z"])


def test_zero_volume_bar_gives_nan_not_inf():
    bars = _flat_bars()
    bars.loc[299, ["volume", "taker_buy_base"]] = 0.0
    last = ft.compute_features(bars).iloc[-1]
    assert np.isnan(last["taker_buy_ratio"])
    assert not np.isinf(last["volume_z"])


def test_build_state_is_anonymous_and_rounded():
    row = {name: 1.23456 for name in ft.STATE_FEATURES}
    row.update(close=123456.7, open_time=1759276800000000, t_close=1759276860000000)
    state = ft.build_state(row)
    assert set(state["features"]) == set(ft.STATE_FEATURES)
    assert set(state["feature_definitions"]) == set(ft.STATE_FEATURES)
    assert state["features"]["ret_1"] == 1.235
    text = json.dumps(state)
    assert "123456" not in text and "1759276" not in text
