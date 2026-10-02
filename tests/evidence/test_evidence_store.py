# tests/evidence/test_evidence_store.py
"""판정 기록 저장(P2): 마이그레이션 0009, 결과 → 행 저장, cascade, 시작 시·조회 시 stale, 백그라운드 작업.

PostgreSQL이 필요한 테스트는 pg 픽스처를 쓴다(EVIDENCE_TEST_DATABASE_URL, conftest 참고).
"""
import asyncio
import uuid
from datetime import timedelta

from sqlalchemy import func, select, text

from app.models import Chat, EvidenceClaim, EvidenceRun
from app.services.evidence import background, records
from app.services.evidence import runner as rn
from tests.evidence.conftest import alembic_downgrade, alembic_upgrade
from tests.evidence.p2_support import ANSWER, CO, CORP, PASSAGES, FakeJev, database, make_runner, seed_user


def _new_run(seed: dict, **kw) -> EvidenceRun:
    return records.new_run(chat_id=uuid.UUID(seed["chat_id"]), conversation_id=uuid.UUID(seed["conversation_id"]),
                           user_id=uuid.UUID(seed["user"]["id"]), answer=ANSWER, company=CO, corp_code=CORP,
                           passages=PASSAGES, generator_model="llama3.1:8b", **kw)


async def _claim_states(db, run_id):
    rows = await db.execute(select(EvidenceClaim.status, EvidenceClaim.reason)
                            .where(EvidenceClaim.run_id == run_id).order_by(EvidenceClaim.idx))
    return [tuple(r) for r in rows]


async def _add(factory, run):
    async with factory() as db:
        db.add(run)
        await db.commit()
    return run.id


# ── 순수 함수 ────────────────────────────────────────────────────────────────

def test_preview_claims_marks_not_claim_and_pending():
    out = records.preview_claims(ANSWER)
    assert [c["status"] for c in out] == ["pending", "pending", "not_claim"]
    for c in out:
        assert ANSWER[c["start"]:c["end"]] == c["text"]


def test_new_run_snapshot_and_pending_claims():
    seed = {"chat_id": str(uuid.uuid4()), "conversation_id": str(uuid.uuid4()), "user": {"id": str(uuid.uuid4())}}
    run = _new_run(seed)
    assert (run.status, run.trigger, run.policy_version, run.jev_model) == ("pending", "auto", "a2-provisional",
                                                                         "jev-1.13.0")
    assert run.rcept_no == "20260312000123" and run.company == CO and run.corp_code == CORP
    assert run.passages[0] == {k: PASSAGES[0][k] for k in ("passage_id", "section", "idx", "sha256", "text")}
    assert [c.status for c in run.claims] == ["pending", "pending", "not_claim"]
    assert run.created_at is not None


def test_poll_until_grows_with_queue():
    assert records.poll_until_s(0) == 12
    assert records.poll_until_s(1) == 20  # 같은 사용자의 두 번째 실행 최악 약 16초를 덮는다
    assert records.poll_until_s(10) == records.STALE_AFTER_S


# ── 마이그레이션 ────────────────────────────────────────────────────────────

def test_migration_0009_tables_cascade_and_downgrade(pg):
    async def fk_rules():
        async with database(pg) as factory, factory() as db:
            rows = await db.execute(text(
                "SELECT tc.table_name, rc.delete_rule FROM information_schema.referential_constraints rc "
                "JOIN information_schema.table_constraints tc ON tc.constraint_name = rc.constraint_name "
                "WHERE tc.table_name IN ('evidence_runs', 'evidence_claims')"))
            return sorted(tuple(r) for r in rows)

    async def tables():
        async with database(pg) as factory, factory() as db:
            rows = await db.execute(text("SELECT tablename FROM pg_tables WHERE tablename LIKE 'evidence_%'"))
            return sorted(r[0] for r in rows)

    assert asyncio.run(fk_rules()) == [("evidence_claims", "CASCADE"), ("evidence_runs", "CASCADE")]
    alembic_downgrade(pg, "0008")
    try:
        assert asyncio.run(tables()) == []
    finally:
        alembic_upgrade(pg)
    assert asyncio.run(tables()) == ["evidence_claims", "evidence_runs"]


# ── 저장 ─────────────────────────────────────────────────────────────────────

def test_save_result_writes_claims_and_usage(pg):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run = _new_run(seed)
            run.status = "running"  # 백그라운드 작업이 시작한 실행만 결과를 받는다
            run_id = await _add(factory, run)
            result = await make_runner().run(company=CO, answer=ANSWER, passages=[p["text"] for p in PASSAGES],
                                              user_id=seed["user"]["id"])
            async with factory() as db:
                run = await db.get(EvidenceRun, run_id)
                assert await records.save_result(db, run, result) is True
                await db.commit()
            async with factory() as db:
                run = await db.get(EvidenceRun, run_id)
                claims = (await db.execute(select(EvidenceClaim).where(EvidenceClaim.run_id == run_id)
                                           .order_by(EvidenceClaim.idx))).scalars().all()
                return run, claims, result

    run, claims, result = asyncio.run(go())
    assert (run.status, run.error_code, run.calls, run.input_tokens) == ("done", None, 2, 200)
    assert run.finished_at is not None
    assert [c.status for c in claims] == ["supported", "supported", "not_claim"]
    assert [c.route for c in claims] == ["jev", "jev", "rule_not_claim"]
    first = claims[0]
    assert first.s == result.claims[0].s and len(first.s) == 3 and first.source_idx == result.claims[0].source_idx
    assert first.number_ok == result.claims[0].number_ok and first.jev_request_key == result.claims[0].jev_request_key
    assert (first.text, first.start, first.end) == (result.claims[0].text, result.claims[0].start, result.claims[0].end)


def test_sql_delete_of_thread_cascades_to_runs_and_claims(pg):
    """스레드 삭제는 chats를 SQL DELETE로 지운다(conversations.delete_conversation). 판정 행도 사라져야 한다."""
    from app.routes import conversations

    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            other = await seed_user(factory)
            await _add(factory, _new_run(seed))
            await _add(factory, _new_run(other))
            orig = conversations.get_active_conversation

            async def none(_uid):  # Redis 활성 스레드 확인만 대신한다
                return None
            conversations.get_active_conversation = none
            try:
                async with factory() as db:
                    await conversations.delete_conversation(seed["conversation_id"], seed["user"], db)
            finally:
                conversations.get_active_conversation = orig
            async with factory() as db:
                runs = await db.scalar(select(func.count()).select_from(EvidenceRun))
                claims = await db.scalar(select(func.count()).select_from(EvidenceClaim))
                chats = await db.scalar(select(func.count()).select_from(Chat))
                return runs, claims, chats

    assert asyncio.run(go()) == (1, 3, 1)  # 다른 사용자의 실행 1건(문장 3개)만 남는다


# ── stale 처리 ───────────────────────────────────────────────────────────────

def test_startup_marks_pending_and_running_failed_stale(pg):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            ids = {}
            for status in ("pending", "running", "done", "partial"):
                run = _new_run(seed)
                run.status = status
                ids[status] = await _add(factory, run)
            async with factory() as db:
                n = await records.fail_active_runs(db)
                await db.commit()
            async with factory() as db:
                claims = {s: await _claim_states(db, i) for s, i in ids.items()}
                return n, {s: (await db.get(EvidenceRun, i)) for s, i in ids.items()}, claims

    n, runs, claims = asyncio.run(go())
    assert n == 2
    # 실패한 실행의 문장은 pending으로 남지 않는다(⊘ 판정 불가, 사유는 실행의 error_code)
    assert claims["pending"] == claims["running"] == [("unjudged", "stale"), ("unjudged", "stale"),
                                                      ("not_claim", None)]
    assert claims["done"] == [("pending", None), ("pending", None), ("not_claim", None)]  # 다른 실행은 그대로
    assert (runs["pending"].status, runs["pending"].error_code) == ("failed", "stale")
    assert (runs["running"].status, runs["running"].error_code) == ("failed", "stale")
    assert runs["pending"].finished_at is not None
    assert (runs["done"].status, runs["partial"].status) == ("done", "partial")


def test_expire_stale_only_after_60s(pg):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            at = records.now()
            old, fresh, done = _new_run(seed), _new_run(seed), _new_run(seed)
            old.status, done.status = "running", "done"
            old.created_at = at - timedelta(seconds=61)
            fresh.created_at = at - timedelta(seconds=59)
            done.created_at = at - timedelta(hours=1)
            for r in (old, fresh, done):
                await _add(factory, r)
            async with factory() as db:
                runs = [await db.get(EvidenceRun, r.id) for r in (old, fresh, done)]
                n = await records.expire_stale(db, runs, at)
                await db.commit()
            async with factory() as db:
                return n, [await db.get(EvidenceRun, r.id) for r in (old, fresh, done)], \
                    [await _claim_states(db, r.id) for r in (old, fresh)]

    n, (old, fresh, done), (old_claims, fresh_claims) = asyncio.run(go())
    assert n == 1
    assert (old.status, old.error_code) == ("failed", "stale") and old.finished_at is not None
    assert (fresh.status, done.status) == ("pending", "done")
    assert old_claims[0] == ("unjudged", "stale") and fresh_claims[0] == ("pending", None)


# ── 백그라운드 작업 ──────────────────────────────────────────────────────────

class _Boom:
    async def run(self, **kw):
        raise RuntimeError("boom")


def test_background_success_saves_done(pg):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run_id = await _add(factory, _new_run(seed))
            task = background.start_run(run_id, runner=make_runner(), session_factory=factory)
            assert task in background._TASKS
            await task
            assert task not in background._TASKS  # 끝나면 참조를 뺀다
            async with factory() as db:
                return await db.get(EvidenceRun, run_id)

    run = asyncio.run(go())
    assert run.status == "done" and run.started_at is not None and run.finished_at is not None
    assert run.started_at >= run.created_at


def test_background_exception_leaves_run_failed(pg):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run_id = await _add(factory, _new_run(seed))
            await background.start_run(run_id, runner=_Boom(), session_factory=factory)
            async with factory() as db:
                return await db.get(EvidenceRun, run_id), await _claim_states(db, run_id)

    run, claims = asyncio.run(go())
    assert (run.status, run.error_code) == ("failed", "internal")
    assert run.finished_at is not None
    assert claims == [("unjudged", "internal"), ("unjudged", "internal"), ("not_claim", None)]


def test_stale_run_is_not_revived_by_late_finish(pg):
    """작업 중 조회가 stale로 바꾸고 retry가 새 실행을 만든 뒤 원 작업이 끝나도 원 실행은 failed(stale)로 남는다."""
    gate = asyncio.Event

    class Slow:
        def __init__(self):
            self.entered, self.release = gate(), gate()

        async def run(self, **kw):
            self.entered.set()
            await self.release.wait()
            return await make_runner().run(**kw)

    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run_id = await _add(factory, _new_run(seed))
            slow = Slow()
            task = background.start_run(run_id, runner=slow, session_factory=factory)
            await slow.entered.wait()
            async with factory() as db:  # 60초가 지난 것으로 보고 조회 시 stale
                run = await db.get(EvidenceRun, run_id)
                await records.expire_stale(db, [run], run.created_at + timedelta(seconds=61))
                await db.commit()
            retry = _new_run(seed, trigger="retry")
            await _add(factory, retry)
            await background.start_run(retry.id, runner=make_runner(), session_factory=factory)
            slow.release.set()
            await task
            async with factory() as db:
                return (await db.get(EvidenceRun, run_id), await _claim_states(db, run_id),
                        await db.get(EvidenceRun, retry.id), await _claim_states(db, retry.id))

    old, old_claims, new, new_claims = asyncio.run(go())
    assert (old.status, old.error_code, old.calls) == ("failed", "stale", 0)
    assert old_claims == [("unjudged", "stale"), ("unjudged", "stale"), ("not_claim", None)]
    assert (new.status, new.error_code) == ("done", None)
    assert [c[0] for c in new_claims] == ["supported", "supported", "not_claim"]


def test_background_uses_snapshot_and_company(pg):
    seen = {}

    class Spy:
        async def run(self, **kw):
            seen.update(kw)
            return await make_runner().run(**kw)

    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run_id = await _add(factory, _new_run(seed, trigger="retry"))
            await background.start_run(run_id, runner=Spy(), session_factory=factory)
            return run_id

    run_id = asyncio.run(go())
    assert seen["company"] == CO and seen["answer"] == ANSWER and seen["trigger"] == "retry"
    assert seen["passages"] == [p["text"] for p in PASSAGES] and seen["run_id"] == str(run_id)
    assert seen["policy"] is rn.A2_PROVISIONAL


def test_latest_run_summary_per_chat(pg):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            first = _new_run(seed)
            first.status = "failed"
            await _add(factory, first)
            second = _new_run(seed, trigger="retry")
            second.created_at = first.created_at + timedelta(seconds=5)
            await _add(factory, second)
            await background.start_run(second.id, runner=make_runner(FakeJev()), session_factory=factory)
            async with factory() as db:
                return second.id, await records.latest_runs_for_chats(db, [uuid.UUID(seed["chat_id"])]), seed

    second_id, latest, seed = asyncio.run(go())
    summary = latest[uuid.UUID(seed["chat_id"])]
    assert summary["id"] == str(second_id) and summary["status"] == "done" and summary["trigger"] == "retry"
    assert summary["counts"] == {"supported": 2, "contradicted": 0, "no_evidence": 0, "not_claim": 1,
                                 "unjudged": 0, "pending": 0}


# ── 앱 종료(lifespan) ────────────────────────────────────────────────────────

def test_close_runner_closes_jev_http_client(monkeypatch, fake_redis):
    from app.lib import redis_cache

    monkeypatch.setattr(redis_cache, "_redis", fake_redis)
    monkeypatch.setattr(background, "_runner", None)
    monkeypatch.setattr(background, "_jev", None)

    async def go():
        background.get_runner()
        http = background._jev._client
        await background.close_runner()
        await background.close_runner()  # 두 번 불러도 된다
        return http.is_closed, background._runner, background._jev

    assert asyncio.run(go()) == (True, None, None)
