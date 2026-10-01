"""lab 테스트용 합성 1분봉(랜덤워크) — 외부 시세 없이 계산 로직만 검증한다."""
from __future__ import annotations

import numpy as np
import pandas as pd

MINUTE_US = 60_000_000
T0_US = 1_759_276_800_000_000  # 2025-10-01 00:00 UTC


def make_minute_bars(n: int = 600, seed: int = 7, start: float = 100_000.0) -> pd.DataFrame:
    """data.load_klines와 같은 열을 가진 합성 1분봉."""
    rng = np.random.default_rng(seed)
    close = start * np.exp(np.cumsum(rng.normal(0, 0.0008, n)))
    open_ = np.concatenate([[start], close[:-1]])
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.0003, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.0003, n)))
    volume = rng.uniform(5, 50, n)
    open_time = T0_US + np.arange(n, dtype=np.int64) * MINUTE_US
    return pd.DataFrame({"open_time": open_time, "open": open_, "high": high, "low": low, "close": close,
                         "volume": volume, "taker_buy_base": volume * rng.uniform(0.3, 0.7, n),
                         "t_close": open_time + MINUTE_US})
