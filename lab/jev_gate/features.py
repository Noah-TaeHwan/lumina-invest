"""진입 신호 시점의 익명 특징.

모든 값은 해당 봉 마감 시점까지의 데이터만 쓴다(shift·rolling·ewm만 사용).
JEV 입력(state)에는 종목명·날짜·시각·절대 가격을 넣지 않는다.
breakout_margin_atr·bars_since_prev_signal은 돌파 채널이 필요해서 rule.find_candidates가 붙인다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.services import ta_utils as ta

STATE_FEATURES = ("ret_1", "ret_5", "ret_15", "ret_60", "breakout_margin_atr", "atr_regime",
                  "volume_z", "taker_buy_ratio", "range_pos_240", "trend_slope_atr", "bars_since_prev_signal")

FEATURE_DEFINITIONS = {
    "ret_1": "log return over the last 1 bar, percent",
    "ret_5": "log return over the last 5 bars, percent",
    "ret_15": "log return over the last 15 bars, percent",
    "ret_60": "log return over the last 60 bars, percent",
    "breakout_margin_atr": "(close - prior channel high) / ATR(14)",
    "atr_regime": "ATR(14) / ATR(240); above 1 means volatility is higher than usual",
    "volume_z": "z-score of this bar's volume against the previous 60 bars",
    "taker_buy_ratio": "taker buy volume / total volume of this bar",
    "range_pos_240": "position of close within the last 240 bars' low-high range, 0 to 1",
    "trend_slope_atr": "(EMA(60) now - EMA(60) 15 bars ago) / ATR(14)",
    "bars_since_prev_signal": "bars since the previous breakout signal, capped at 240",
}


def compute_features(k: pd.DataFrame) -> pd.DataFrame:
    """1분봉에 지표 열을 더한 새 DataFrame. 0으로 나누는 경우는 NaN으로 둔다."""
    f = k.copy()
    close, high, low, vol = f["close"], f["high"], f["low"], f["volume"]
    logc = np.log(close)
    for n in (1, 5, 15, 60):
        f[f"ret_{n}"] = (logc - logc.shift(n)) * 100
    f["atr14"] = ta.atr(high, low, close, 14)
    f["atr_regime"] = f["atr14"] / ta.atr(high, low, close, 240)
    prev_vol = vol.shift(1).rolling(60)
    f["volume_z"] = (vol - prev_vol.mean()) / prev_vol.std().replace(0, np.nan)
    f["taker_buy_ratio"] = f["taker_buy_base"] / vol.replace(0, np.nan)
    lo, hi = low.rolling(240).min(), high.rolling(240).max()
    f["range_pos_240"] = (close - lo) / (hi - lo).replace(0, np.nan)
    ema60 = ta.ema(close, 60)
    f["trend_slope_atr"] = (ema60 - ema60.shift(15)) / f["atr14"]
    return f


def build_state(row) -> dict:
    """후보 한 행을 JEV state(익명 특징 + 정의)로 바꾼다. 값은 소수 셋째 자리로 반올림한다."""
    return {"features": {name: round(float(row[name]), 3) for name in STATE_FEATURES},
            "feature_definitions": FEATURE_DEFINITIONS}
