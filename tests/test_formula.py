"""자유 산식 DSL: 안전성 · causal(룩어헤드 차단) · 계산 · 버전 체크섬 · 코드 생성."""
import pandas as pd
import pytest

from app.services import formula as fx
from tests.conftest import make_candles


def test_blocked_constructs():
    for bad in ["__import__('os')", "close.mean()", "[1,2]", "lambda x: x", "open(1)", "shift(close, 0)", "shift(close, -1)",
                "'abc'", "close ** 50", "unknown_fn(close)", "foo + 1"]:
        with pytest.raises(fx.FormulaError):
            fx.compute(make_candles(120), bad)


def test_validate_reports_names_and_checksum():
    info = fx.validate_definition("zscore(close, n)", "zscore(close, n) < -2", "zscore(close, n) > 0", {"n": 20})
    assert info["ok"] and "zscore" in info["functions"] and "n" in info["variables"] and len(info["checksum"]) == 16
    with pytest.raises(fx.FormulaError):
        fx.validate_definition("zscore(close, n)", params={})            # n 미정의
    with pytest.raises(fx.FormulaError):
        fx.validate_definition("sma(close, 20)", params={"close": 1})    # 예약어 충돌


def test_checksum_changes_only_with_definition():
    a = fx.definition_checksum("sma(close, 20)", "", "", {"n": 5})
    b = fx.definition_checksum("sma(close, 20) ", None, None, {"n": 5.0})  # 공백/None/float 동일 취급
    c = fx.definition_checksum("sma(close, 20)", "", "", {"n": 6})
    assert a == b != c


def test_compute_is_causal():
    """마지막 봉을 바꿔도 이전 봉의 지표·신호는 변하지 않는다."""
    candles = make_candles(300)
    r1 = fx.compute(candles, "zscore(close, 20)", "crossover(ema(close,5), ema(close,20))", "crossunder(ema(close,5), ema(close,20))")
    tampered = [dict(c) for c in candles]; tampered[-1]["close"] *= 1.5
    r2 = fx.compute(tampered, "zscore(close, 20)", "crossover(ema(close,5), ema(close,20))", "crossunder(ema(close,5), ema(close,20))")
    assert r1["series"]["indicator"][:-1] == r2["series"]["indicator"][:-1]
    assert r1["series"]["position"][:-1] == r2["series"]["position"][:-1]


def test_compute_backtest_and_signals():
    r = fx.compute(make_candles(400), "rsi(close, 14)", "rsi(close, 14) < 35 and close > sma(close, 50)", "rsi(close, 14) > 70",
                   commission_bps=10, slippage_bps=5)
    assert r["backtest"] and "total_return_pct" in r["backtest"] and r["backtest"]["commission_bps"] == 10
    assert r["latest_signal"] in ("BUY", "SELL", "HOLD", "HOLD_LONG")
    assert len(r["series"]["times"]) == len(r["series"]["indicator"]) <= 250
    r2 = fx.compute(make_candles(200), "close / sma(close, n) - 1", params={"n": 10})
    assert r2["backtest"] is None and r2["latest_value"] is not None


def test_where_and_params_and_templates_run():
    for t in fx.TEMPLATES:
        r = fx.compute(make_candles(400), t["indicator_expr"], t["buy_expr"], t["sell_expr"], t["params"])
        assert r["rows"] > 0
    r = fx.compute(make_candles(150), "where(rsi(close) < 30, 1, 0) + nz(shift(close, 1) / close - 1)")
    assert r["latest_value"] is not None


def test_code_generation():
    pine = fx.to_pine("Test", "zscore(close, n)", "crossover(ema(close,5), ema(close,20))", "rsi(close,14) > 70", {"n": 20})
    assert "//@version=5" in pine and "ta.crossover(ta.ema(close,5), ta.ema(close,20))" in pine and "input.float(20.0" in pine
    py = fx.to_python("Test", "sma(close, 20)", None, None, {})
    assert "compute(candles" in py


def test_negative_shift_rejected_at_validation():
    with pytest.raises(fx.FormulaError):
        fx.validate_definition("shift(close, -1)")
    with pytest.raises(fx.FormulaError):
        fx.validate_definition("shift(close, 0)")
    assert fx.validate_definition("shift(close, 1)")["ok"]


def test_pine_no_double_prefix():
    pine = fx.to_pine("Z", "zscore(close, n)", None, None, {"n": 20})
    assert "ta.ta." not in pine and "ta.sma(close, n)" in pine
