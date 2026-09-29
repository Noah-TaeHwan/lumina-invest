"""캔들 패턴·지지/저항·돌파·멀티타임프레임 종합 로직."""
import pandas as pd
import pytest

from app.services import patterns as pt
from tests.conftest import make_candles


def _df(rows):
    idx = pd.date_range("2026-01-01", periods=len(rows), freq="D")
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close", "volume"])


def _flat(n=24, price=100.0):
    return [[price, price + 1, price - 1, price, 1000] for _ in range(n)]


def test_hammer_and_bullish_engulfing_detected():
    rows = _flat() + [[100, 100.5, 90, 99.5, 1000]]                     # 긴 아래꼬리 해머
    keys = {p["key"] for p in pt.detect_candle_patterns(_df(rows), lookback=1)}
    assert "hammer" in keys
    rows = _flat() + [[100, 101, 97, 98, 1000], [97, 104, 96.5, 103, 1500]]  # 음봉 뒤 장악 양봉
    keys = {p["key"] for p in pt.detect_candle_patterns(_df(rows), lookback=1)}
    assert "bullish_engulfing" in keys


def test_doji_and_three_black_crows():
    rows = _flat() + [[100, 103, 97, 100.1, 1000]]
    assert "doji" in {p["key"] for p in pt.detect_candle_patterns(_df(rows), lookback=1)}
    rows = _flat() + [[100, 100.5, 96, 96.5, 1000], [96.5, 97, 92, 92.5, 1000], [92.5, 93, 88, 88.5, 1000]]
    assert "three_black_crows" in {p["key"] for p in pt.detect_candle_patterns(_df(rows), lookback=1)}


def test_support_resistance_levels_found():
    rows = []
    for i in range(120):   # 90~110 사이 진동 → 110 저항, 90 지지 반복 터치
        c = 100 + 10 * (1 if (i // 10) % 2 == 0 else -1) * ((i % 10) / 9)
        rows.append([c, c + 0.5, c - 0.5, c, 1000])
    sr = pt.support_resistance(_df(rows))
    assert sr["levels"]
    prices = [l["price"] for l in sr["levels"]]
    assert any(abs(p - 110) < 2 for p in prices) or any(abs(p - 90) < 2 for p in prices)
    assert all(l["type"] in ("support", "resistance") for l in sr["levels"])


def test_breakout_52w_high_and_20d():
    rows = _flat(300)
    rows.append([100, 130, 100, 128, 5000])  # 급등 신고가 + 거래량 5배
    events = {e["key"]: e for e in pt.detect_breakouts(_df(rows))}
    assert "breakout_20d" in events and events["breakout_20d"]["confirmed"]
    assert "high_52w" in events


def test_pattern_summary_on_synthetic_candles():
    r = pt.pattern_summary(make_candles(300))
    assert {"patterns", "support_resistance", "breakouts", "pattern_score", "pattern_bias"} <= set(r)


def test_combine_timeframes_confidence_and_action():
    r = pt.combine_timeframes([{"label": "m", "weight": .2, "score": 3}, {"label": "d", "weight": .5, "score": 4}, {"label": "w", "weight": .3, "score": 2}])
    assert r["action"] == "강력 매수" and r["agreement"] == 100 and r["confidence"] >= 80
    mixed = pt.combine_timeframes([{"weight": .5, "score": 2}, {"weight": .5, "score": -2}])
    assert mixed["action"] == "관망" and mixed["confidence"] < 30
    assert pt.combine_timeframes([{"weight": 1, "error": "x"}])["action"] == "관망"


def test_timeframe_score_reasons():
    r = pt.timeframe_score(make_candles(120))
    assert -6 <= r["score"] <= 6 and len(r["reasons"]) >= 4
