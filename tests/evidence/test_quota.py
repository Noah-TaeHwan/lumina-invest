# tests/evidence/test_quota.py
"""Redis 일일 한도(spec 7.2): 시작 전 잔여 확인, 호출 뒤 실제 토큰 INCRBY, KST 날짜."""
import asyncio
from datetime import datetime, timezone

import pytest

from app.lib import jev_service

NOW = datetime(2026, 10, 2, 3, 0, tzinfo=timezone.utc)


def _quota(redis, now=NOW):
    return jev_service.Quota(redis, now=lambda: now)


def test_default_limits_match_spec():
    lim = jev_service.QuotaLimits()
    assert (lim.user_calls, lim.user_tokens, lim.global_tokens) == (150, 750_000, 3_000_000)
    assert jev_service.EST_TOKENS_PER_CALL == 5070


def test_day_uses_kst_boundary(fake_redis):
    assert _quota(fake_redis, datetime(2026, 10, 2, 14, 59, tzinfo=timezone.utc)).day() == "20261002"
    assert _quota(fake_redis, datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)).day() == "20261003"


def test_record_increments_calls_and_actual_tokens_with_48h_expiry(fake_redis):
    q = _quota(fake_redis)
    asyncio.run(q.record("u1", 4560))
    asyncio.run(q.record("u1", 0))
    d = fake_redis.data
    assert d["evidence:quota:20261002:user:u1:calls"] == "2"
    assert d["evidence:quota:20261002:user:u1:tokens"] == "4560"
    assert d["evidence:quota:20261002:global:tokens"] == "4560"
    assert set(fake_redis.ttl.values()) == {48 * 3600}


def test_check_passes_with_room(fake_redis):
    assert asyncio.run(_quota(fake_redis).check("u1", 8)) is None


@pytest.mark.parametrize("key,value,n,expected", [
    ("evidence:quota:20261002:user:u1:calls", "149", 2, "cap_user"),
    ("evidence:quota:20261002:user:u1:calls", "148", 2, None),
    ("evidence:quota:20261002:user:u1:tokens", str(750_000 - 5070 * 2 + 1), 2, "cap_user"),
    ("evidence:quota:20261002:user:u1:tokens", str(750_000 - 5070 * 2), 2, None),
    ("evidence:quota:20261002:global:tokens", str(3_000_000 - 5070), 2, "cap_global"),
])
def test_check_compares_remaining_with_estimate(fake_redis, key, value, n, expected):
    fake_redis.data[key] = value
    assert asyncio.run(_quota(fake_redis).check("u1", n)) == expected


def test_previous_kst_day_does_not_count(fake_redis):
    fake_redis.data["evidence:quota:20261001:user:u1:calls"] = "150"
    assert asyncio.run(_quota(fake_redis).check("u1", 1)) is None


def test_redis_down_raises_quota_unavailable(down_redis):
    with pytest.raises(jev_service.QuotaUnavailable):
        asyncio.run(_quota(down_redis).check("u1", 1))
    with pytest.raises(jev_service.QuotaUnavailable):
        asyncio.run(_quota(down_redis).record("u1", 10))
