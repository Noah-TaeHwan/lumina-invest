"""공용 픽스처: 합성 OHLCV 캔들(랜덤워크) — 외부 시세·DB 없이 순수 계산 로직만 검증한다."""
from __future__ import annotations

import os

import numpy as np
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


def make_candles(n: int = 400, seed: int = 42, start: float = 10_000.0) -> list[dict]:
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0004, 0.015, n)
    close = start * np.cumprod(1 + rets)
    high = close * (1 + np.abs(rng.normal(0, 0.006, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.006, n)))
    open_ = np.concatenate([[start], close[:-1]])
    vol = rng.integers(50_000, 500_000, n)
    t0 = 1_600_000_000
    return [{"time": t0 + i * 86_400, "open": float(open_[i]), "high": float(high[i]), "low": float(low[i]),
             "close": float(close[i]), "volume": int(vol[i])} for i in range(n)]


@pytest.fixture
def candles() -> list[dict]:
    return make_candles()
