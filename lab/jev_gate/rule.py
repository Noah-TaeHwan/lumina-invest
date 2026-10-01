"""Donchian 돌파 진입 후보와 코드 청산 규칙.

진입 후보: close[t] > max(high[t-N..t-1])이고 직전 봉에서는 같은 조건이 거짓인 봉(새 돌파).
청산: 봉 마감 종가로 손절(진입가 − 2·ATR)·익절(진입가 + 3·ATR)·시간청산(120봉)을 판정한다.
ATR은 신호 봉의 ATR14로 고정한다. 손절·익절은 봉 안의 가격 경로를 보지 않는다(모든 갈래 공통 단순화).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from lab.jev_gate.features import STATE_FEATURES

STOP_ATR = 2.0
TAKE_ATR = 3.0
MAX_BARS = 120
PREV_SIGNAL_CAP = 240


def find_candidates(f: pd.DataFrame, n: int) -> pd.DataFrame:
    """특징 DataFrame에서 새 돌파 봉만 골라 bar·channel_high·breakout_margin_atr·bars_since_prev_signal을 붙인다.

    bar는 f의 행 위치다. 특징이 하나라도 비어 있는 워밍업 구간 후보는 뺀다.
    """
    f = f.reset_index(drop=True)
    channel = f["high"].shift(1).rolling(n).max()
    above = (f["close"] > channel).to_numpy()
    bars = np.flatnonzero(above & ~np.concatenate([[False], above[:-1]]))
    c = f.iloc[bars].copy()
    c["bar"] = bars
    c["channel_high"] = channel.iloc[bars].to_numpy()
    c["breakout_margin_atr"] = (c["close"] - c["channel_high"]) / c["atr14"]
    gaps = np.diff(bars, prepend=bars[0] - PREV_SIGNAL_CAP) if len(bars) else bars
    c["bars_since_prev_signal"] = np.minimum(gaps, PREV_SIGNAL_CAP)
    return c.dropna(subset=list(STATE_FEATURES)).reset_index(drop=True)


def find_exit(close: np.ndarray, first_bar: int, entry_price: float, atr: float) -> tuple[int, str]:
    """first_bar부터 종가로 청산 봉과 사유(stop·take·time·end)를 찾는다. end는 데이터가 먼저 끝난 경우."""
    stop = entry_price - STOP_ATR * atr
    take = entry_price + TAKE_ATR * atr
    for i in range(first_bar, min(first_bar + MAX_BARS, len(close))):
        if close[i] <= stop:
            return i, "stop"
        if close[i] >= take:
            return i, "take"
    if first_bar + MAX_BARS <= len(close):
        return first_bar + MAX_BARS - 1, "time"
    return len(close) - 1, "end"


def net_return(entry: float, exit_: float, fee_rate: float, slip_bps: float) -> float:
    """슬리피지(매수 +, 매도 −)와 양방향 수수료를 뺀 거래 수익률."""
    slip = slip_bps / 10_000
    return (exit_ * (1 - slip)) / (entry * (1 + slip)) * (1 - fee_rate) ** 2 - 1


def rule_only_close_approx(f: pd.DataFrame, cands: pd.DataFrame, fee_rate: float, slip_bps: float) -> pd.DataFrame:
    """Stage 0용 근사 백테스트: 신호 봉 종가 진입, 청산 봉 종가 청산, 포지션 1개.

    f의 마지막 행 이후는 보지 않으므로, 구간 끝에서 자른 f를 넘기면 다음 구간 가격을 쓰지 않는다.
    """
    close = f["close"].to_numpy(dtype=float)
    rows, busy_until = [], -1
    for c in cands.itertuples(index=False):
        if c.bar <= busy_until or c.bar + 1 >= len(close):
            continue
        exit_bar, reason = find_exit(close, c.bar + 1, c.close, c.atr14)
        rows.append({"bar": c.bar, "exit_bar": exit_bar, "reason": reason,
                     "gross_ret": close[exit_bar] / c.close - 1,
                     "net_ret": net_return(c.close, close[exit_bar], fee_rate, slip_bps)})
        busy_until = exit_bar
    return pd.DataFrame(rows, columns=["bar", "exit_bar", "reason", "gross_ret", "net_ret"])
