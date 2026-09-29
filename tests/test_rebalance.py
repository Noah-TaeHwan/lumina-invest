"""리밸런싱 엔진 순수 로직: 목표 비중 정규화 · 주기 계산."""
from datetime import datetime, timezone

import pytest

from app.services import rebalance as rb


def test_normalize_targets_merges_duplicates_and_rounds():
    out = rb.normalize_targets([
        {"symbol": "005930.KS", "name": "삼성전자", "weight_pct": 30},
        {"symbol": "005930.ks", "weight_pct": 10.004},
        {"symbol": "AAPL", "weight_pct": "20"},
    ])
    by = {t["symbol"]: t for t in out}
    assert by["005930.KS"]["weight_pct"] == 40.0
    assert by["AAPL"]["weight_pct"] == 20.0


def test_normalize_targets_rejects_over_100():
    with pytest.raises(rb.RebalanceError):
        rb.normalize_targets([{"symbol": "A", "weight_pct": 60}, {"symbol": "B", "weight_pct": 50}])


def test_normalize_targets_rejects_negative_or_nonnumeric():
    with pytest.raises(rb.RebalanceError):
        rb.normalize_targets([{"symbol": "A", "weight_pct": -1}])
    with pytest.raises(rb.RebalanceError):
        rb.normalize_targets([{"symbol": "A", "weight_pct": "abc"}])


@pytest.mark.parametrize("now,period,expected", [
    (datetime(2026, 9, 29, 12, tzinfo=timezone.utc), "monthly",   datetime(2026, 10, 1, tzinfo=timezone.utc)),
    (datetime(2026, 12, 15, tzinfo=timezone.utc),    "monthly",   datetime(2027, 1, 1, tzinfo=timezone.utc)),
    (datetime(2026, 9, 29, tzinfo=timezone.utc),     "quarterly", datetime(2026, 10, 1, tzinfo=timezone.utc)),
    (datetime(2026, 11, 2, tzinfo=timezone.utc),     "quarterly", datetime(2027, 1, 1, tzinfo=timezone.utc)),
    (datetime(2026, 3, 3, tzinfo=timezone.utc),      "yearly",    datetime(2027, 1, 1, tzinfo=timezone.utc)),
])
def test_next_period_start(now, period, expected):
    assert rb.next_period_start(now, period) == expected


def test_next_period_start_none():
    assert rb.next_period_start(datetime.now(timezone.utc), "none") is None


def test_add_months_clamps_day():
    assert rb._add_months(datetime(2026, 1, 31, tzinfo=timezone.utc), 1) == datetime(2026, 2, 28, tzinfo=timezone.utc)
