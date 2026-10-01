"""Stage 1 후보 표 검증."""
import numpy as np
import pandas as pd

from lab.jev_gate import candidates, features as ft, gate, rule
from tests.lab.bars import MINUTE_US, T0_US

PRE = {"rule": {"n_grid": [3]}, "costs": {"taker_fee_rate": 0.001, "slippage_bps": 1.0},
       "periods": {"dev": ["2025-10-01", "2025-10-01"], "holdout": ["2025-10-02", "2025-10-02"]}}


def _bars(closes, opens=None):
    closes = np.asarray(closes, dtype=float)
    opens = closes if opens is None else np.asarray(opens, dtype=float)
    n = len(closes)
    t = T0_US + np.arange(n, dtype=np.int64) * 5 * MINUTE_US
    vol = np.where(np.arange(n) % 2 == 0, 10.0, 20.0)
    return pd.DataFrame({"open_time": t, "open": opens, "high": np.maximum(opens, closes) + 0.5,
                         "low": np.minimum(opens, closes) - 0.5, "close": closes, "volume": vol,
                         "taker_buy_base": vol * 0.5, "t_close": t + 5 * MINUTE_US})


def test_entry_is_next_bar_open_and_label_matches_net_return():
    closes = [100.0] * 300 + [101.0] + [100.0] * 60
    opens = list(closes)
    opens[301] = 100.7                      # 다음 봉 시가가 진입가
    t = candidates.build_table(_bars(closes, opens), PRE)
    row = t.iloc[0]
    assert row["bar"] == 300
    exit_bar, reason = rule.find_exit(np.asarray(closes), 301, 100.7, row["atr14"])
    expected = rule.net_return(100.7, opens[exit_bar + 1], 0.001, 1.0)
    assert row["exit_reason"] == reason and row["net_ret"] == expected
    assert row["label"] == int(expected < 0)
    assert row["key"] == gate.cache_key(row["state"])
    assert set(row["state"]["features"]) == set(ft.STATE_FEATURES)


def test_candidates_skip_when_exit_fill_missing():
    closes = [100.0] * 300 + [101.0] + [100.0] * 5      # 시간청산 전에 데이터가 끝남
    assert len(candidates.build_table(_bars(closes), PRE)) == 0


def test_period_and_day_columns():
    closes = [100.0] * 300 + [101.0] + [100.0] * 60
    row = candidates.build_table(_bars(closes), PRE).iloc[0]
    assert row["day"] == "2025-10-02" and row["period"] == "holdout"   # 300번 봉 마감 = 10-02 01:05 UTC
