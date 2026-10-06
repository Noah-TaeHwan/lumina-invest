# tests/factcheck/test_fc_quota.py
"""팩트체커 한도(Outside Voice #5, D8): factcheck_quota 표와 원자적 JEV 토큰 예약·정산, 익명 키.

- 예약은 `UPDATE ... WHERE used + reserved + est <= cap RETURNING`(전체·키별)로 한 번에 판정한다.
- 서로 다른 연결 두 개가 같은 잔여량을 동시에 노리면 하나만 통과한다.
- 익명 하루 3회, 예약 실패면 아무것도 남기지 않는다(롤백), 정산은 예약을 풀고 실제 토큰을 더한다.
DB가 필요한 테스트는 fc_pg 픽스처(EVIDENCE_TEST_DATABASE_URL)를 쓴다.
"""
import asyncio
import hashlib
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services.factcheck import quota as fq

NOW = datetime(2026, 10, 6, 3, 0, tzinfo=timezone.utc)  # KST 2026-10-06 12:00


def _factory(url):
    engine = create_async_engine(url)
    return engine, async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def _rows(url) -> dict:
    engine, _ = _factory(url)
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT key, day, count, used, reserved FROM factcheck_quota"))).all()
    await engine.dispose()
    return {r.key: (r.day, r.count, r.used, r.reserved) for r in rows}


def _quota(factory, **limits) -> fq.FactcheckQuota:
    return fq.FactcheckQuota(factory, fq.QuotaLimits(**{"runs": 3, "key_tokens": 100_000, "global_tokens": 1_000_000,
                                                         **limits}), now=lambda: NOW)


# ── 표·마이그레이션 ─────────────────────────────────────────────────────────

def test_migration_creates_factcheck_quota(fc_pg):
    async def go():
        engine, _ = _factory(fc_pg)
        async with engine.connect() as conn:
            cols = {r[0] for r in (await conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'factcheck_quota'"))).all()}
            ver = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
        await engine.dispose()
        return cols, ver

    cols, ver = asyncio.run(go())
    assert cols == {"key", "day", "count", "used", "reserved", "updated_at"}
    assert ver == "0012"


def test_migration_downgrade_drops_only_factcheck_quota(fc_pg_migrated):
    from tests.evidence.conftest import alembic_downgrade, alembic_upgrade

    async def tables():
        engine, _ = _factory(fc_pg_migrated)
        async with engine.connect() as conn:
            out = {r[0] for r in (await conn.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))).all()}
        await engine.dispose()
        return out

    before = asyncio.run(tables())
    alembic_downgrade(fc_pg_migrated, "0011")
    try:
        after = asyncio.run(tables())
    finally:
        alembic_upgrade(fc_pg_migrated)
    assert before - after == {"factcheck_quota"}


# ── 예약·정산 ────────────────────────────────────────────────────────────────

def test_reserve_then_settle_moves_reserved_to_used(fc_pg):
    async def go():
        engine, factory = _factory(fc_pg)
        q = _quota(factory)
        res = await q.reserve("k1", 10_000)
        mid = await _rows(fc_pg)
        await q.settle(res, 7_000)
        await engine.dispose()
        return res, mid

    res, mid = asyncio.run(go())
    assert res.key == "k1" and res.day == "20261006" and res.est == 10_000
    assert mid["k1"] == ("20261006", 1, 0, 10_000)
    assert mid["global"] == ("20261006", 1, 0, 10_000)
    end = asyncio.run(_rows(fc_pg))
    assert end["k1"] == ("20261006", 1, 7_000, 0)
    assert end["global"] == ("20261006", 1, 7_000, 0)


def test_fourth_run_of_the_day_is_refused_and_leaves_no_trace(fc_pg):
    async def go():
        engine, factory = _factory(fc_pg)
        q = _quota(factory)
        for _ in range(3):
            await q.settle(await q.reserve("k1", 1_000), 1_000)
        with pytest.raises(fq.QuotaExceeded) as ei:
            await q.reserve("k1", 1_000)
        other = await q.reserve("k2", 1_000)  # 다른 키는 그대로 된다
        await engine.dispose()
        return ei.value, other

    err, other = asyncio.run(go())
    assert err.code == "cap_runs"
    rows = asyncio.run(_rows(fc_pg))
    assert rows["k1"] == ("20261006", 3, 3_000, 0)
    assert rows["global"][1:] == (4, 3_000, 1_000)
    assert other.key == "k2"


def test_key_token_cap_refuses_before_run_count(fc_pg):
    async def go():
        engine, factory = _factory(fc_pg)
        q = _quota(factory, key_tokens=10_000)
        await q.reserve("k1", 6_000)
        with pytest.raises(fq.QuotaExceeded) as ei:
            await q.reserve("k1", 5_000)  # 6,000 예약 + 5,000 > 10,000
        await engine.dispose()
        return ei.value

    assert asyncio.run(go()).code == "cap_key_tokens"
    rows = asyncio.run(_rows(fc_pg))
    assert rows["k1"][1:] == (1, 0, 6_000)
    assert rows["global"][1:] == (1, 0, 6_000)


def test_global_cap_refusal_rolls_back_key_reservation(fc_pg):
    async def go():
        engine, factory = _factory(fc_pg)
        q = _quota(factory, global_tokens=10_000)
        await q.reserve("k1", 8_000)
        with pytest.raises(fq.QuotaExceeded) as ei:
            await q.reserve("k2", 3_000)
        await engine.dispose()
        return ei.value

    assert asyncio.run(go()).code == "cap_global"
    rows = asyncio.run(_rows(fc_pg))
    assert "k2" not in rows  # 키 행 생성·예약·회수가 모두 롤백됐다
    assert rows["global"][1:] == (1, 0, 8_000)


def test_used_tokens_count_against_cap_after_settle(fc_pg):
    async def go():
        engine, factory = _factory(fc_pg)
        q = _quota(factory, global_tokens=10_000)
        await q.settle(await q.reserve("k1", 5_000), 9_500)  # 실제가 예약보다 컸다
        with pytest.raises(fq.QuotaExceeded) as ei:
            await q.reserve("k2", 1_000)
        await engine.dispose()
        return ei.value

    assert asyncio.run(go()).code == "cap_global"


def test_concurrent_reservations_cannot_share_the_same_remainder(fc_pg):
    """서로 다른 연결 두 개가 남은 한 몫을 동시에 노리면 정확히 하나만 통과한다(전체 상한)."""
    async def go():
        e1, f1 = _factory(fc_pg)
        e2, f2 = _factory(fc_pg)
        q1, q2 = _quota(f1, global_tokens=10_000), _quota(f2, global_tokens=10_000)
        await q1.reserve("warm", 1)  # 행을 미리 만든다(동시 INSERT가 아닌 동시 UPDATE를 겨눈다)
        outs = []
        for _ in range(5):
            outs.append(await asyncio.gather(q1.reserve("a", 6_000), q2.reserve("b", 6_000), return_exceptions=True))
            async with e1.begin() as conn:
                await conn.execute(text("UPDATE factcheck_quota SET reserved = 0, used = 0, count = 0 "
                                        "WHERE key <> 'warm'"))
                await conn.execute(text("UPDATE factcheck_quota SET reserved = 1 WHERE key = 'global'"))
        await e1.dispose()
        await e2.dispose()
        return outs

    for pair in asyncio.run(go()):
        ok = [r for r in pair if isinstance(r, fq.Reservation)]
        refused = [r for r in pair if isinstance(r, fq.QuotaExceeded)]
        assert len(ok) == 1 and len(refused) == 1, pair
        assert refused[0].code == "cap_global"


def test_concurrent_same_key_reservations_respect_run_count(fc_pg):
    """같은 키로 동시에 4건이 들어와도 3건만 통과한다."""
    async def go():
        engines = [_factory(fc_pg) for _ in range(4)]
        qs = [_quota(f) for _, f in engines]
        await qs[0].reserve("other", 1)
        outs = await asyncio.gather(*(q.reserve("same", 1_000) for q in qs), return_exceptions=True)
        for e, _ in engines:
            await e.dispose()
        return outs

    outs = asyncio.run(go())
    assert sum(isinstance(r, fq.Reservation) for r in outs) == 3
    assert [r.code for r in outs if isinstance(r, fq.QuotaExceeded)] == ["cap_runs"]


def test_recheck_reservation_does_not_count_a_run(fc_pg):
    async def go():
        engine, factory = _factory(fc_pg)
        q = _quota(factory)
        for _ in range(3):
            await q.reserve("k1", 1_000)
        res = await q.reserve("k1", 1_000, count_run=False)  # 건너뛴 문장 수동 검수
        await engine.dispose()
        return res

    assert asyncio.run(go()).est == 1_000
    assert asyncio.run(_rows(fc_pg))["k1"][1:] == (3, 0, 4_000)


def test_old_anonymous_rows_are_purged_but_global_kept(fc_pg):
    async def go():
        engine, factory = _factory(fc_pg)
        old = fq.FactcheckQuota(factory, fq.QuotaLimits(), now=lambda: datetime(2026, 10, 4, 3, tzinfo=timezone.utc))
        await old.reserve("old-key", 1_000)
        await _quota(factory).reserve("k1", 1_000)
        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT key, day FROM factcheck_quota ORDER BY day, key"))).all()
        await engine.dispose()
        return [tuple(r) for r in rows]

    assert asyncio.run(go()) == [("global", "20261004"), ("global", "20261006"), ("k1", "20261006")]


# ── 익명 키·설정 ─────────────────────────────────────────────────────────────

def test_anon_key_is_salted_sha256_and_rotates_daily():
    days = iter(["20261006", "20261006", "20261007"])
    keyer = fq.AnonKeyer(day=lambda: next(days))
    k1, k2, k3 = keyer.key("203.0.113.7"), keyer.key("203.0.113.7"), keyer.key("203.0.113.7")
    assert k1 == k2 and k1 != k3  # 같은 날 같은 IP는 같은 키, 다음 날은 연결되지 않는다
    assert len(k1) == 64 and "203.0.113.7" not in k1
    assert k1 != hashlib.sha256(b"203.0.113.7").hexdigest()  # 솔트 없는 해시가 아니다


def test_estimate_uses_service_tokens_per_call():
    from app.lib.jev_service import EST_TOKENS_PER_CALL

    assert fq.estimate(4) == 4 * EST_TOKENS_PER_CALL
    assert fq.estimate(0) == 0


def test_limits_from_env_default_is_conservative(monkeypatch):
    monkeypatch.delenv("FACTCHECK_DAILY_GLOBAL_TOKENS", raising=False)
    lim = fq.limits_from_env()
    assert lim.runs == 3
    assert 0 < lim.global_tokens < 3_000_000  # 근거 모드 전체 기본값보다 작다
    monkeypatch.setenv("FACTCHECK_DAILY_GLOBAL_TOKENS", "123456")
    assert fq.limits_from_env().global_tokens == 123_456
