# tests/factcheck/test_fc_quota.py
"""팩트체커 한도(Outside Voice #5, D8, 검수 결과 비용 상한 3·5·6, 남용 11): 원자적 예약·정산, 예약 ID, 일별 솔트.

- 예약은 `UPDATE ... WHERE used + reserved + est <= cap RETURNING`(전체·키별)로 한 번에 판정하고, 같은 트랜잭션에서
  예약 행(factcheck_reservations, ID)을 남긴다. 서로 다른 연결 두 개가 같은 잔여량을 동시에 노리면 하나만 통과한다.
- 정산은 예약 ID로 한 번만 된다(멱등). 비정상 사용량(음수·정수 아님)은 예약량 그대로 차감한다.
- 서버 시작 때 오래된(30분) 미정산 예약은 예약량으로 정산한다.
- 자정을 넘겨도 진행 중 예약이 있는 전날 행은 지우지 않는다.
- 익명 키의 일별 솔트는 DB에 날짜별로 두어 재시작해도 같은 날은 같은 키다(지난 날 솔트는 지운다).
DB가 필요한 테스트는 fc_pg 픽스처(EVIDENCE_TEST_DATABASE_URL)를 쓴다.
"""
import asyncio
import hashlib
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services.factcheck import metering as fm
from app.services.factcheck import quota as fq

NOW = datetime(2026, 10, 6, 3, 0, tzinfo=timezone.utc)  # KST 2026-10-06 12:00
YESTERDAY = datetime(2026, 10, 5, 14, 59, tzinfo=timezone.utc)  # KST 2026-10-05 23:59


def _factory(url):
    engine = create_async_engine(url)
    return engine, async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def _rows(url) -> dict:
    engine, _ = _factory(url)
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT key, day, count, used, reserved FROM factcheck_quota"))).all()
    await engine.dispose()
    return {r.key: (r.day, r.count, r.used, r.reserved) for r in rows}


async def _q(url, sql, **params):
    engine, _ = _factory(url)
    async with engine.begin() as conn:
        out = (await conn.execute(text(sql), params)).all()
    await engine.dispose()
    return [tuple(r) for r in out]


def _quota(factory, now=NOW, **limits) -> fq.FactcheckQuota:
    return fq.FactcheckQuota(factory, fq.QuotaLimits(**{"runs": 3, "key_tokens": 100_000, "global_tokens": 1_000_000,
                                                         **limits}), now=lambda: now)


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
    assert ver == "0013"
    res_cols = {r[0] for r in asyncio.run(_q(fc_pg, "SELECT column_name FROM information_schema.columns "
                                                    "WHERE table_name = 'factcheck_reservations'"))}
    assert res_cols == {"id", "key", "day", "est", "actual", "count_run", "created_at", "settled_at"}
    salt_cols = {r[0] for r in asyncio.run(_q(fc_pg, "SELECT column_name FROM information_schema.columns "
                                                     "WHERE table_name = 'factcheck_salt'"))}
    assert salt_cols == {"day", "salt", "created_at"}


def test_migration_downgrade_drops_only_factcheck_tables(fc_pg_migrated):
    from tests.evidence.conftest import alembic_downgrade, alembic_upgrade

    async def tables():
        engine, _ = _factory(fc_pg_migrated)
        async with engine.connect() as conn:
            out = {r[0] for r in (await conn.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))).all()}
        await engine.dispose()
        return out

    before = asyncio.run(tables())
    alembic_downgrade(fc_pg_migrated, "0012")
    try:
        mid = asyncio.run(tables())
        alembic_downgrade(fc_pg_migrated, "0011")
        after = asyncio.run(tables())
    finally:
        alembic_upgrade(fc_pg_migrated)
    assert before - mid == {"factcheck_reservations", "factcheck_salt"}
    assert mid - after == {"factcheck_quota"}


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

    assert asyncio.run(go()).code == "cap_key_busy"  # used 0: 진행 중 예약(6,000)이 풀리면 들어간다(T4-B3)
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

    assert asyncio.run(go()).code == "cap_global_busy"  # used 0: 다른 검수 예약 때문(T4-B3)
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
        assert refused[0].code == "cap_global_busy"  # 같은 잔여량을 다른 예약이 먼저 잡았다(T4-B3)


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
        await old.settle(await old.reserve("old-key", 1_000), 1_000)  # 정산이 끝난 전날 행만 지운다
        await _quota(factory).reserve("k1", 1_000)
        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT key, day FROM factcheck_quota ORDER BY day, key"))).all()
        await engine.dispose()
        return [tuple(r) for r in rows]

    assert asyncio.run(go()) == [("global", "20261004"), ("global", "20261006"), ("k1", "20261006")]


# ── 예약 ID·멱등 정산·비정상 사용량 ─────────────────────────────────────────

def test_reservation_has_id_row_and_settles_only_once(fc_pg):
    async def go():
        engine, factory = _factory(fc_pg)
        q = _quota(factory)
        res = await q.reserve("k1", 10_000)
        rows_mid = await _q(fc_pg, "SELECT id, key, day, est, actual, count_run, settled_at IS NULL "
                                   "FROM factcheck_reservations")
        first = await q.settle(res, 3_000)
        second = await q.settle(res, 9_000)  # 다시 불러도 바뀌지 않는다
        await engine.dispose()
        return res, rows_mid, first, second

    res, rows_mid, first, second = asyncio.run(go())
    assert len(res.id) >= 32
    assert rows_mid == [(res.id, "k1", "20261006", 10_000, None, True, True)]
    assert first is True and second is False
    rows = asyncio.run(_rows(fc_pg))
    assert rows["k1"][2:] == (3_000, 0) and rows["global"][2:] == (3_000, 0)
    assert asyncio.run(_q(fc_pg, "SELECT actual, settled_at IS NOT NULL FROM factcheck_reservations")) == [(3_000, True)]


def test_refused_reservation_leaves_no_reservation_row(fc_pg):
    async def go():
        engine, factory = _factory(fc_pg)
        q = _quota(factory, global_tokens=1_000)
        with pytest.raises(fq.QuotaExceeded):
            await q.reserve("k1", 5_000)
        await engine.dispose()

    asyncio.run(go())
    assert asyncio.run(_q(fc_pg, "SELECT count(*) FROM factcheck_reservations")) == [(0,)]


@pytest.mark.parametrize("bad", [-1, None, "100", 1.5, True])
def test_abnormal_actual_is_charged_at_reservation(fc_pg, bad):
    async def go():
        engine, factory = _factory(fc_pg)
        q = _quota(factory)
        await q.settle(await q.reserve("k1", 10_000), bad)
        await engine.dispose()

    asyncio.run(go())
    assert asyncio.run(_rows(fc_pg))["global"][2:] == (10_000, 0)


def test_settle_stale_charges_old_unsettled_reservations_at_reservation(fc_pg):
    async def go():
        engine, factory = _factory(fc_pg)
        q = _quota(factory)
        old = await q.reserve("k1", 10_000)
        fresh = await q.reserve("k2", 7_000)
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE factcheck_reservations SET created_at = now() - interval '31 minutes' "
                                    "WHERE id = :i"), {"i": old.id})
        n = await q.settle_stale(older_than_s=30 * 60)
        late = await q.settle(old, 1)  # 작업이 늦게 정산하려 해도 이미 끝났다
        await engine.dispose()
        return n, late, fresh

    n, late, fresh = asyncio.run(go())
    assert n == 1 and late is False
    rows = asyncio.run(_rows(fc_pg))
    assert rows["k1"][2:] == (10_000, 0)  # 예약량 그대로
    assert rows["k2"][2:] == (0, 7_000)  # 30분 안 된 예약은 그대로
    assert rows["global"][2:] == (10_000, 7_000)


# ── 자정 넘김 ────────────────────────────────────────────────────────────────

def test_midnight_keeps_previous_day_rows_with_pending_reservation(fc_pg):
    async def go():
        engine, factory = _factory(fc_pg)
        night = _quota(factory, now=YESTERDAY)
        pending = await night.reserve("k1", 10_000)  # 23:59에 시작한 검수
        today = _quota(factory)
        await today.reserve("k2", 1_000)  # 00:00 이후 다른 요청이 지난 행 정리를 부른다
        kept = await _rows(fc_pg)
        ok = await today.settle(pending, 4_000)  # 자정 넘어 끝난 검수는 전날 행에 정산된다
        await engine.dispose()
        return kept, ok

    kept, ok = asyncio.run(go())
    assert kept["k1"][0] == "20261005" and ok is True
    rows = asyncio.run(_q(fc_pg, "SELECT key, day, used, reserved FROM factcheck_quota ORDER BY day, key"))
    assert ("k1", "20261005", 4_000, 0) in rows and ("global", "20261005", 4_000, 0) in rows


def test_settle_reports_missing_quota_rows(fc_pg):
    async def go():
        engine, factory = _factory(fc_pg)
        q = _quota(factory)
        res = await q.reserve("k1", 1_000)
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM factcheck_quota WHERE key = 'k1'"))
        with pytest.raises(fq.SettleMismatch):
            await q.settle(res, 500)
        await engine.dispose()

    asyncio.run(go())
    # 행이 모자라면 정산을 되돌린다(예약은 미정산으로 남아 오래된 예약 정산이 다시 잡는다)
    assert asyncio.run(_q(fc_pg, "SELECT settled_at IS NULL FROM factcheck_reservations")) == [(True,)]
    assert asyncio.run(_rows(fc_pg))["global"][2:] == (0, 1_000)


# ── 익명 키(일별 솔트 DB 저장)·설정 ─────────────────────────────────────────

def test_anon_key_salt_survives_restart_and_rotates_daily(fc_pg):
    async def go():
        engine, factory = _factory(fc_pg)
        day = ["20261006"]
        a = fq.AnonKeyer(factory, day=lambda: day[0])
        b = fq.AnonKeyer(factory, day=lambda: day[0])  # 재시작한 프로세스
        k1, k2 = await a.key("203.0.113.7"), await b.key("203.0.113.7")
        other = await a.key("203.0.113.8")
        day[0] = "20261007"
        k3 = await b.key("203.0.113.7")
        salts = await _q(fc_pg, "SELECT day FROM factcheck_salt ORDER BY day")
        await engine.dispose()
        return k1, k2, other, k3, salts

    k1, k2, other, k3, salts = asyncio.run(go())
    assert k1 == k2  # 재시작해도 같은 날 같은 IP는 같은 키(익명 한도가 초기화되지 않는다)
    assert other != k1 and k3 != k1  # 다른 IP, 다음 날은 연결되지 않는다
    assert len(k1) == 64 and "203.0.113.7" not in k1
    assert k1 != hashlib.sha256(b"203.0.113.7").hexdigest()
    assert salts == [("20261007",)]  # 지난 날 솔트는 지운다


def test_concurrent_first_salt_is_single(fc_pg):
    async def go():
        engines = [_factory(fc_pg) for _ in range(4)]
        keyers = [fq.AnonKeyer(f, day=lambda: "20261006") for _, f in engines]
        keys = await asyncio.gather(*(k.key("198.51.100.1") for k in keyers))
        for e, _ in engines:
            await e.dispose()
        return keys

    assert len(set(asyncio.run(go()))) == 1


def test_limits_from_env_defaults_fit_one_max_run(monkeypatch):
    for v in ("FACTCHECK_DAILY_GLOBAL_TOKENS", "FACTCHECK_DAILY_KEY_TOKENS", "FACTCHECK_DAILY_ANON_RUNS"):
        monkeypatch.delenv(v, raising=False)
    lim = fq.limits_from_env()
    max_run = fm.reservation_for(fq.MAX_SENTENCES)
    assert lim.runs == 3
    assert lim.key_tokens >= max_run and lim.global_tokens >= max_run  # 30문장 검수가 시작될 수 있다
    assert lim.global_tokens <= 3_000_000  # 근거 모드 전체 기본값을 넘지 않는다
    monkeypatch.setenv("FACTCHECK_DAILY_GLOBAL_TOKENS", "123456")
    assert fq.limits_from_env().global_tokens == 123_456


def test_concurrent_reserve_and_settle_do_not_deadlock(fc_pg):
    """예약(키 → 전체)과 정산이 같은 순서로 행을 잠근다(C1). 여러 연결에서 예약·정산을 섞어 돌려도 교착 없이 끝난다."""
    async def go():
        engines = [_factory(fc_pg) for _ in range(4)]
        qs = [_quota(f, key_tokens=10 ** 9, global_tokens=10 ** 12, runs=10 ** 6) for _, f in engines]

        async def cycle(q, key):
            for _ in range(15):
                await q.settle(await q.reserve(key, 100), 50)

        await asyncio.wait_for(asyncio.gather(*(cycle(q, f"k{i % 2}") for i, q in enumerate(qs))), 60)
        for e, _ in engines:
            await e.dispose()

    asyncio.run(go())
    rows = asyncio.run(_rows(fc_pg))
    assert rows["global"][1:] == (60, 3_000, 0)


def test_settle_updates_key_then_global_separately(fc_pg):
    """정산 SQL이 키 행과 전체 행을 따로(키 먼저) 바꾼다 — 한 문장 IN (k, g)는 잠금 순서가 정해지지 않는다(C1)."""
    import inspect

    src = inspect.getsource(fq.FactcheckQuota._settle_in)
    assert "_SETTLE_KEY" in src and "_SETTLE_GLOBAL" in src
    assert src.index("_SETTLE_KEY") < src.index("_SETTLE_GLOBAL")


# ── T4-B5: 오래된 예약 복구는 행마다 따로 ───────────────────────────────────

def test_settle_stale_isolates_failing_row(fc_pg, caplog):
    import logging

    caplog.set_level(logging.ERROR)

    async def go():
        engine, factory = _factory(fc_pg)
        q = _quota(factory)
        bad = await q.reserve("bad", 1_000)
        good = await q.reserve("good", 2_000)
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE factcheck_reservations SET created_at = now() - interval '1 hour'"))
            await conn.execute(text("DELETE FROM factcheck_quota WHERE key = 'bad'"))  # 이 행은 SettleMismatch
        n = await q.settle_stale(older_than_s=60)
        await engine.dispose()
        return n, bad, good

    n, bad, good = asyncio.run(go())
    assert n == 1  # 한 행 실패가 나머지 복구를 막지 않는다
    rows = asyncio.run(_q(fc_pg, "SELECT id, settled_at IS NOT NULL FROM factcheck_reservations"))
    assert dict(rows) == {bad.id: False, good.id: True}
    assert "factcheck_stale_settle_failed" in caplog.text and bad.id in caplog.text


# ── T4-B3: 다른 검수의 예약 때문에 잠시 부족 vs 오늘 한도 소진 ──────────────

def test_global_shortfall_from_others_reservations_is_busy_not_exhausted(fc_pg):
    async def go():
        engine, factory = _factory(fc_pg)
        q = _quota(factory, global_tokens=10_000)
        a = await q.reserve("a", 6_000)
        with pytest.raises(fq.QuotaExceeded) as busy:
            await q.reserve("b", 6_000)  # 6,000 예약이 풀리면 들어간다(used 0)
        await q.settle(a, 6_000)  # 실제로 다 썼다
        with pytest.raises(fq.QuotaExceeded) as gone:
            await q.reserve("b", 6_000)  # 이제 오늘 남은 양이 모자란다
        await engine.dispose()
        return busy.value.code, gone.value.code

    assert asyncio.run(go()) == ("cap_global_busy", "cap_global")


def test_key_shortfall_from_own_running_reservation_is_busy(fc_pg):
    async def go():
        engine, factory = _factory(fc_pg)
        q = _quota(factory, key_tokens=10_000)
        await q.reserve("k1", 6_000)
        with pytest.raises(fq.QuotaExceeded) as busy:
            await q.reserve("k1", 6_000, count_run=False)
        await engine.dispose()
        return busy.value.code

    assert asyncio.run(go()) == "cap_key_busy"
