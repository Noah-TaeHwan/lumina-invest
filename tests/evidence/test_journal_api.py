# tests/evidence/test_journal_api.py
"""판단 일지 저장·API(모듈 C spec 10절 P1, 수용 기준 8.1의 1~4, 6, 7, 9~12).

마이그레이션 0010, 기록 생성 규칙(5-5), 스냅샷 보존(5-3·5-4), 다시 보기 원장(5-2), 삭제·내보내기(7-2·7-3),
메모가 밖으로 나가지 않음(7-4), 기능 플래그(5-6), 관리자 초기화(5-7).
가짜 검색·생성기·JEV만 쓴다. DB가 필요한 테스트는 pg 픽스처(EVIDENCE_TEST_DATABASE_URL)를 쓴다.
"""
import asyncio
import json
import logging
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models import AuditEvent, EvidenceClaim, EvidenceRun, JudgmentEntry, JudgmentUpdate
from app.services.evidence import records
from app.services.evidence import runner as rn
from tests.evidence.conftest import alembic_downgrade, alembic_upgrade
from tests.evidence.p2_support import (ANSWER, CO, CORP, PASSAGES, FakeJev, FakeLLM, Who, client, database, drain,
                                       make_app, make_runner, seed_user)

MEMO = "비밀메모-보유 300주, 다음 달 추가 매수 계획"
FORBIDDEN = {"s", "c", "lex", "jev_request_key"}


@pytest.fixture
def journal(monkeypatch):
    monkeypatch.setattr(settings, "JOURNAL_ENABLED", True)


@pytest.fixture
def evidence_on(monkeypatch):
    monkeypatch.setattr(settings, "EVIDENCE_CHAT_ENABLED", True)
    from app.routes import evidence

    async def nothing(*a, **k):
        return None
    monkeypatch.setattr(evidence, "set_active_conversation", nothing)  # Redis 사용자 상태 대신


@pytest.fixture
def audit_db(monkeypatch):
    """감사 로그가 테스트 DB에 실제로 쓰이게 한다(audit()는 예외를 삼키므로 연결하지 않으면 아무것도 남지 않는다)."""
    from app.services import audit

    holder = {}
    monkeypatch.setattr(audit, "get_session_factory", lambda: holder["factory"])
    return holder


def _new_run(seed: dict, *, policy=rn.DEFAULT_POLICY) -> EvidenceRun:
    return records.new_run(chat_id=uuid.UUID(seed["chat_id"]), conversation_id=uuid.UUID(seed["conversation_id"]),
                           user_id=uuid.UUID(seed["user"]["id"]), answer=ANSWER, company=CO, corp_code=CORP,
                           passages=PASSAGES, generator_model="llama3.1:8b", policy=policy)


async def _judged_run(factory, seed: dict) -> uuid.UUID:
    """가짜 JEV로 끝까지 판정한 실행(문장 행에 s·c·lex·jev_request_key가 있다)."""
    run = _new_run(seed)
    run.status = "running"
    async with factory() as db:
        db.add(run)
        await db.commit()
    result = await make_runner().run(company=CO, answer=ANSWER, passages=[p["text"] for p in PASSAGES],
                                     user_id=seed["user"]["id"])
    async with factory() as db:
        assert await records.save_result(db, await db.get(EvidenceRun, run.id), result)
        await db.commit()
    return run.id


async def _run_with(factory, seed: dict, status: str, *, policy=rn.DEFAULT_POLICY, code=None) -> uuid.UUID:
    run = _new_run(seed, policy=policy)
    run.status, run.error_code = status, code
    if status not in records.ACTIVE:
        run.finished_at = records.now()
        for c in run.claims:
            if c.status == "pending":
                c.status, c.reason = "unjudged", code or "deadline"
    async with factory() as db:
        db.add(run)
        await db.commit()
    return run.id


def _body(run_id, **kw):
    return {"run_id": str(run_id), "decision": "watch", "conviction": 3, "memo": MEMO, "relied_claims": [0],
            "review_on": None, **kw}


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


# ── 설정·마이그레이션 ─────────────────────────────────────────────────────────

def test_journal_flag_default_off():
    from app.config import Settings

    assert Settings(_env_file=None).JOURNAL_ENABLED is False


def test_migration_0010_tables_constraints_and_downgrade(pg):
    async def info():
        async with database(pg) as factory, factory() as db:
            tables = sorted(r[0] for r in await db.execute(text(
                "SELECT tablename FROM pg_tables WHERE tablename LIKE 'judgment_%'")))
            fks = sorted(tuple(r) for r in await db.execute(text(
                "SELECT tc.table_name, ccu.table_name, rc.delete_rule FROM information_schema.referential_constraints rc "
                "JOIN information_schema.table_constraints tc ON tc.constraint_name = rc.constraint_name "
                "JOIN information_schema.constraint_column_usage ccu ON ccu.constraint_name = rc.constraint_name "
                "WHERE tc.table_name LIKE 'judgment_%'")))
            checks = sorted(r[0] for r in await db.execute(text(
                "SELECT conname FROM pg_constraint WHERE contype = 'c' AND conrelid::regclass::text LIKE 'judgment_%'")))
            return tables, fks, checks

    tables, fks, checks = asyncio.run(info())
    assert tables == ["judgment_entries", "judgment_updates"]
    # run_id에는 FK가 없다(원 대화가 지워져도 기록은 남는다, 결정 5-4)
    assert fks == [("judgment_entries", "users", "NO ACTION"), ("judgment_updates", "judgment_entries", "CASCADE")]
    assert checks == ["ck_judgment_updates_conviction", "ck_judgment_updates_decision", "ck_judgment_updates_kind"]
    alembic_downgrade(pg, "0009")
    try:
        assert asyncio.run(info())[0] == []
    finally:
        alembic_upgrade(pg)
    assert asyncio.run(info())[0] == ["judgment_entries", "judgment_updates"]


@pytest.mark.parametrize("column,value", [("kind", "edit"), ("decision", "buy"), ("conviction", 0),
                                          ("conviction", 6)])
def test_check_constraints_reject_bad_values(pg, column, value):
    """수용 기준 12: decision·kind(와 conviction) CHECK 제약이 DB 수준에서 거부한다."""
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            async with factory() as db:
                entry = JudgmentEntry(user_id=uuid.UUID(seed["user"]["id"]), run_id=uuid.uuid4(), company=CO,
                                      corp_code=CORP, snapshot={}, snapshot_version="c1")
                db.add(entry)
                await db.flush()
                row = {"entry_id": entry.id, "kind": "initial", "decision": "watch", "conviction": 3, "memo": "",
                       "relied_claims": [], **{column: value}}
                db.add(JudgmentUpdate(**row))
                with pytest.raises(IntegrityError):
                    await db.flush()

    asyncio.run(go())


# ── 기능 플래그(수용 기준 9) ──────────────────────────────────────────────────

class _NoDb:
    def __call__(self):
        raise AssertionError("DB에 닿으면 안 된다")


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/journal"),
    ("GET", "/api/journal"),
    ("GET", "/api/journal/export"),
    ("GET", f"/api/journal/{uuid.uuid4()}"),
    ("POST", f"/api/journal/{uuid.uuid4()}/updates"),
    ("DELETE", f"/api/journal/{uuid.uuid4()}"),
    ("DELETE", "/api/journal?confirm=delete-all"),
])
def test_flag_off_every_journal_path_is_404(monkeypatch, method, path):
    monkeypatch.setattr(settings, "JOURNAL_ENABLED", False)
    app = make_app(_NoDb(), Who(None))

    async def go():
        async with client(app) as c:
            return await c.request(method, path, json={"memo": MEMO})

    r = asyncio.run(go())
    assert r.status_code == 404 and MEMO not in r.text


def test_flag_off_evidence_api_does_not_touch_journal_tables(pg, evidence_on, monkeypatch):
    """0010이 적용되지 않은 환경 보호: 일지가 꺼져 있으면 판정 API가 judgment_entries를 조회하지 않는다.
    실제로 0009까지만 적용한 DB(일지 표 없음)에서 판정 API가 200이어야 한다."""
    monkeypatch.setattr(settings, "JOURNAL_ENABLED", False)

    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run_id = await _judged_run(factory, seed)
            app = make_app(factory, Who(seed["user"]))
            async with client(app) as c:
                one = await c.get(f"/api/evidence/runs/{run_id}")
                timeline = await c.get(f"/api/conversations/{seed['conversation_id']}/evidence")
            return one, timeline

    alembic_downgrade(pg, "0009")
    try:
        one, timeline = asyncio.run(go())
    finally:
        alembic_upgrade(pg)
    assert (one.status_code, timeline.status_code) == (200, 200)
    assert one.json()["journal_entry_id"] is None
    assert timeline.json()["runs"][0]["journal_entry_id"] is None


def test_serialize_run_reports_journal_entry_id(pg, journal, evidence_on):
    """기록 있음 표시는 serialize_run의 journal_entry_id 한 곳(폴링과 스레드 다시 열기가 같은 값)."""
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run_id = await _judged_run(factory, seed)
            app = make_app(factory, Who(seed["user"]))
            async with client(app) as c:
                before = (await c.get(f"/api/evidence/runs/{run_id}")).json()["journal_entry_id"]
                created = (await c.post("/api/journal", json=_body(run_id))).json()
                one = (await c.get(f"/api/evidence/runs/{run_id}")).json()
                timeline = (await c.get(f"/api/conversations/{seed['conversation_id']}/evidence")).json()
            return before, created, one, timeline

    before, created, one, timeline = asyncio.run(go())
    assert before is None
    assert one["journal_entry_id"] == created["id"]
    assert timeline["runs"][0]["journal_entry_id"] == created["id"]
    assert "journal_entry_id" not in json.dumps(timeline["runs"][0]["claims"])


def test_journal_works_with_evidence_mode_off(pg, journal, monkeypatch):
    """일지 켜짐·근거 모드 꺼짐: 생성(원 실행이 있으면)·목록·상세·내보내기·삭제가 동작한다."""
    monkeypatch.setattr(settings, "EVIDENCE_CHAT_ENABLED", False)

    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run_id = await _judged_run(factory, seed)
            app = make_app(factory, Who(seed["user"]))
            async with client(app) as c:
                evidence = await c.get(f"/api/evidence/runs/{run_id}")
                created = await c.post("/api/journal", json=_body(run_id))
                eid = created.json()["id"]
                listed = await c.get("/api/journal")
                detail = await c.get(f"/api/journal/{eid}")
                export = await c.get("/api/journal/export")
                deleted = await c.delete(f"/api/journal/{eid}")
                after = await c.get("/api/journal")
            return evidence, created, listed, detail, export, deleted, after

    evidence, created, listed, detail, export, deleted, after = asyncio.run(go())
    assert evidence.status_code == 404  # 근거 모드 라우터는 꺼져 있다
    assert (created.status_code, listed.status_code, detail.status_code, export.status_code,
            deleted.status_code) == (201, 200, 200, 200, 200)
    assert listed.json()["total"] == 1 and after.json()["total"] == 0


# ── 생성 규칙(수용 기준 4) ────────────────────────────────────────────────────

def test_create_entry_copies_snapshot_and_initial_update(pg, journal):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run_id = await _judged_run(factory, seed)
            app = make_app(factory, Who(seed["user"]))
            async with client(app) as c:
                r = await c.post("/api/journal", json=_body(run_id, decision="consider_buy", conviction=4,
                                                            relied_claims=[1, 0, 1], review_on="2027-01-04"))
            async with factory() as db:
                entry = await db.get(JudgmentEntry, uuid.UUID(r.json()["id"]))
                updates = (await db.execute(select(JudgmentUpdate))).scalars().all()
            return r, entry, updates, run_id, seed

    r, entry, updates, run_id, seed = asyncio.run(go())
    assert r.status_code == 201
    body = r.json()
    assert (body["run_id"], body["company"], body["corp_code"], body["snapshot_version"]) == (str(run_id), CO, CORP,
                                                                                              "c1")
    assert body["source_available"] is True
    snap = body["snapshot"]
    assert snap["run"]["id"] == str(run_id) and snap["run"]["status"] == "done"
    assert snap["question"] == "q" and snap["answer"] == ANSWER and snap["rcept_no"] == "20260312000123"
    assert [c["status"] for c in snap["claims"]] == ["supported", "supported", "not_claim"]
    assert snap["claims"][0]["confidence"] == "보통"
    assert [p["passage_id"] for p in snap["passages"]] == [p["passage_id"] for p in PASSAGES]
    assert entry.snapshot == snap and entry.user_id == uuid.UUID(seed["user"]["id"])
    assert [(u.kind, u.decision, u.conviction, u.memo, u.relied_claims, str(u.review_on)) for u in updates] == [
        ("initial", "consider_buy", 4, MEMO, [0, 1], "2027-01-04")]
    assert body["updates"][0]["kind"] == "initial" and body["updates"][0]["memo"] == MEMO


def test_client_snapshot_is_not_accepted(pg, journal):
    """요청 본문은 run_id와 판단 입력뿐이다(결정 5-3). 스냅샷을 보내면 422."""
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run_id = await _judged_run(factory, seed)
            app = make_app(factory, Who(seed["user"]))
            async with client(app) as c:
                return await c.post("/api/journal", json=_body(run_id, snapshot={"answer": "조작"}))

    r = asyncio.run(go())
    assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", "snapshot"]


@pytest.mark.parametrize("status", ["pending", "running"])
def test_active_run_is_409(pg, journal, status):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run_id = await _run_with(factory, seed, status)
            app = make_app(factory, Who(seed["user"]))
            async with client(app) as c:
                r = await c.post("/api/journal", json=_body(run_id))
            async with factory() as db:
                n = await db.scalar(select(func.count()).select_from(JudgmentEntry))
            return r, n

    r, n = asyncio.run(go())
    assert (r.status_code, r.json()["detail"], n) == (409, "판정이 끝난 뒤 기록할 수 있습니다.", 0)


def test_stale_active_run_is_expired_then_recordable(pg, journal):
    """5-5의 2: stale 정리를 먼저 적용한다. 60초가 지난 pending은 failed(stale)로 바뀌어 기록할 수 있다."""
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run = _new_run(seed)
            run.created_at = records.now() - timedelta(seconds=records.STALE_AFTER_S + 1)
            async with factory() as db:
                db.add(run)
                await db.commit()
            app = make_app(factory, Who(seed["user"]))
            async with client(app) as c:
                return await c.post("/api/journal", json=_body(run.id, relied_claims=[]))

    r = asyncio.run(go())
    assert r.status_code == 201
    snap = r.json()["snapshot"]
    assert (snap["run"]["status"], snap["run"]["error_code"]) == ("failed", "stale")
    assert [c["status"] for c in snap["claims"]] == ["unjudged", "unjudged", "not_claim"]


@pytest.mark.parametrize("status,code", [("done", None), ("partial", "deadline"), ("failed", "http_5xx"),
                                         ("limited", "user_calls"), ("skipped", "no_passages")])
def test_finished_statuses_are_recordable(pg, journal, status, code):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run_id = await _run_with(factory, seed, status, code=code)
            app = make_app(factory, Who(seed["user"]))
            async with client(app) as c:
                return await c.post("/api/journal", json=_body(run_id, relied_claims=[]))

    r = asyncio.run(go())
    assert r.status_code == 201
    assert (r.json()["snapshot"]["run"]["status"], r.json()["snapshot"]["run"]["error_code"]) == (status, code)


def test_second_entry_for_same_run_is_409_with_existing_id(pg, journal):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run_id = await _judged_run(factory, seed)
            app = make_app(factory, Who(seed["user"]))
            async with client(app) as c:
                first = await c.post("/api/journal", json=_body(run_id))
                second = await c.post("/api/journal", json=_body(run_id, decision="exclude"))
            async with factory() as db:
                n = await db.scalar(select(func.count()).select_from(JudgmentUpdate))
            return first, second, n

    first, second, n = asyncio.run(go())
    assert second.status_code == 409
    assert second.json()["entry_id"] == first.json()["id"] and n == 1
    assert MEMO not in second.text


def test_other_users_run_and_entry_are_404(pg, journal):
    async def go():
        async with database(pg) as factory:
            a, b = await seed_user(factory), await seed_user(factory)
            run_a = await _judged_run(factory, a)
            run_b = await _judged_run(factory, b)
            who = Who(a["user"])
            app = make_app(factory, who)
            async with client(app) as c:
                entry_a = (await c.post("/api/journal", json=_body(run_a))).json()["id"]
                who.user = b["user"]
                out = {
                    "create_other_run": await c.post("/api/journal", json=_body(run_a)),
                    "missing_run": await c.post("/api/journal", json=_body(uuid.uuid4())),
                    "detail": await c.get(f"/api/journal/{entry_a}"),
                    "revisit": await c.post(f"/api/journal/{entry_a}/updates",
                                            json={"decision": "exclude", "conviction": 1}),
                    "delete": await c.delete(f"/api/journal/{entry_a}"),
                    "bad_id": await c.get("/api/journal/not-a-uuid"),
                }
                await c.post("/api/journal", json=_body(run_b, memo="b의 메모"))
                out["list"] = await c.get("/api/journal")
                out["export"] = await c.get("/api/journal/export")
                who.user = a["user"]
                out["still_mine"] = await c.get(f"/api/journal/{entry_a}")
            async with factory() as db:
                out["a_updates"] = await db.scalar(select(func.count()).select_from(JudgmentUpdate)
                                                   .where(JudgmentUpdate.entry_id == uuid.UUID(entry_a)))
            return out, entry_a

    out, entry_a = asyncio.run(go())
    for k in ("create_other_run", "missing_run", "detail", "revisit", "delete", "bad_id"):
        assert out[k].status_code == 404, k
    assert out["list"].json()["total"] == 1 and out["list"].json()["items"][0]["id"] != entry_a
    exported = out["export"].json()["entries"]
    assert len(exported) == 1 and exported[0]["id"] != entry_a
    assert MEMO not in out["export"].text and MEMO not in out["list"].text
    assert out["still_mine"].status_code == 200
    assert out["a_updates"] == 1 and len(out["still_mine"].json()["updates"]) == 1  # B의 다시 보기·삭제가 닿지 않았다


def test_relied_claims_must_be_judgeable_claims(pg, journal):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run_id = await _judged_run(factory, seed)
            app = make_app(factory, Who(seed["user"]))
            async with client(app) as c:
                out_of_range = await c.post("/api/journal", json=_body(run_id, relied_claims=[3]))
                not_claim = await c.post("/api/journal", json=_body(run_id, relied_claims=[2]))
                negative = await c.post("/api/journal", json=_body(run_id, relied_claims=[-1]))
                ok = await c.post("/api/journal", json=_body(run_id, relied_claims=[]))
            return out_of_range, not_claim, negative, ok

    *bad, ok = asyncio.run(go())
    for r in bad:
        assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", "relied_claims"]
        assert MEMO not in r.text
    assert ok.status_code == 201


# ── 원 대화 삭제·새 실행에도 스냅샷 보존(수용 기준 1·2·3) ──────────────────────

def test_thread_delete_keeps_snapshot_and_reports_source_gone(pg, journal, monkeypatch):
    from app.routes import conversations

    async def no_active(*a, **k):
        return None
    monkeypatch.setattr(conversations, "get_active_conversation", no_active)  # Redis 사용자 상태 대신

    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run_id = await _judged_run(factory, seed)
            app = make_app(factory, Who(seed["user"]))
            async with client(app) as c:
                eid = (await c.post("/api/journal", json=_body(run_id))).json()["id"]
                before = (await c.get(f"/api/journal/{eid}")).json()
                gone = await c.delete(f"/api/conversations/{seed['conversation_id']}")
                after = (await c.get(f"/api/journal/{eid}")).json()
            async with factory() as db:
                runs = await db.scalar(select(func.count()).select_from(EvidenceRun))
            return before, gone, after, runs

    before, gone, after, runs = asyncio.run(go())
    assert gone.status_code in (200, 204) and runs == 0  # cascade로 판정 기록이 사라졌다
    assert before["source_available"] is True and after["source_available"] is False
    assert after["snapshot"] == before["snapshot"] and after["updates"] == before["updates"]


def test_retry_and_rejudge_do_not_change_snapshot(pg, journal, evidence_on):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            failed = await _run_with(factory, seed, "failed", code="http_5xx")
            app = make_app(factory, Who(seed["user"]), runner=make_runner(FakeJev()))
            async with client(app) as c:
                e1 = (await c.post("/api/journal", json=_body(failed, relied_claims=[]))).json()
                retry = await c.post(f"/api/evidence/runs/{failed}/retry")
                await drain()
                new_run = (await c.get(f"/api/evidence/runs/{retry.json()['run_id']}")).json()
                d1 = (await c.get(f"/api/journal/{e1['id']}")).json()
            # 이전 정책 실행 → 재판정
            seed2 = await seed_user(factory)
            old = _new_run(seed2, policy=rn.A2_PROVISIONAL)
            old.status = "partial"
            async with factory() as db:
                db.add(old)
                await db.commit()
            async with factory() as db:
                from sqlalchemy import update
                await db.execute(update(EvidenceClaim).where(EvidenceClaim.run_id == old.id,
                                                             EvidenceClaim.status == "pending")
                                 .values(status="unjudged", reason="deadline", route="jev"))
                await db.commit()
            app2 = make_app(factory, Who(seed2["user"]))
            async with client(app2) as c:
                e2 = (await c.post("/api/journal", json=_body(old.id, relied_claims=[]))).json()
                rj = await c.post(f"/api/evidence/runs/{old.id}/rejudge")
                d2 = (await c.get(f"/api/journal/{e2['id']}")).json()
            return e1, new_run, d1, e2, rj, d2

    e1, new_run, d1, e2, rj, d2 = asyncio.run(go())
    assert new_run["status"] == "done" and new_run["journal_entry_id"] is None
    assert [c["status"] for c in d1["snapshot"]["claims"]] == ["unjudged", "unjudged", "not_claim"]
    assert d1["snapshot"] == e1["snapshot"] and d1["snapshot"]["run"]["status"] == "failed"
    assert rj.status_code == 201
    assert d2["snapshot"] == e2["snapshot"] and d2["snapshot"]["run"]["policy_version"] == rn.A2_PROVISIONAL.version


def test_snapshot_and_export_have_no_jev_raw_values(pg, journal):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run_id = await _judged_run(factory, seed)
            async with factory() as db:
                raw = (await records.load_claims(db, [run_id]))[run_id]
            app = make_app(factory, Who(seed["user"]))
            async with client(app) as c:
                eid = (await c.post("/api/journal", json=_body(run_id))).json()["id"]
                detail = (await c.get(f"/api/journal/{eid}")).json()
                export = (await c.get("/api/journal/export")).json()
            async with factory() as db:
                stored = (await db.get(JudgmentEntry, uuid.UUID(eid))).snapshot
            return raw, detail, export, stored

    raw, detail, export, stored = asyncio.run(go())
    assert raw[0].s is not None and raw[0].jev_request_key is not None  # 원 실행에는 확률이 있다
    for obj in (stored, detail, export):
        assert not FORBIDDEN & set(_keys(obj))


# ── 다시 보기 원장(수용 기준 6) ───────────────────────────────────────────────

def test_revisit_appends_without_touching_existing_rows(pg, journal):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run_id = await _judged_run(factory, seed)
            app = make_app(factory, Who(seed["user"]))
            async with client(app) as c:
                eid = (await c.post("/api/journal", json=_body(run_id))).json()["id"]
                async with factory() as db:
                    before = [(u.id, u.kind, u.decision, u.conviction, u.memo, u.relied_claims, u.review_on,
                               u.created_at) for u in (await db.execute(select(JudgmentUpdate))).scalars()]
                r = await c.post(f"/api/journal/{eid}/updates",
                                 json={"decision": "exclude", "conviction": 2, "memo": "다시 보니 제외",
                                       "relied_claims": [1], "review_on": "2027-04-01"})
                bad = await c.post(f"/api/journal/{eid}/updates", json={"decision": "exclude", "conviction": 2,
                                                                         "relied_claims": [2]})
                kind = await c.post(f"/api/journal/{eid}/updates", json={"decision": "exclude", "conviction": 2,
                                                                          "kind": "initial"})
                detail = (await c.get(f"/api/journal/{eid}")).json()
            async with factory() as db:
                after = [(u.id, u.kind, u.decision, u.conviction, u.memo, u.relied_claims, u.review_on,
                          u.created_at) for u in (await db.execute(
                              select(JudgmentUpdate).order_by(JudgmentUpdate.created_at))).scalars()]
            return before, r, bad, kind, detail, after

    before, r, bad, kind, detail, after = asyncio.run(go())
    assert r.status_code == 201 and r.json()["kind"] == "revisit"
    assert (bad.status_code, kind.status_code) == (422, 422)
    assert len(after) == 2 and after[0] == before[0]
    assert [u["kind"] for u in detail["updates"]] == ["initial", "revisit"]
    assert detail["updates"][1]["decision"] == "exclude" and detail["updates"][1]["review_on"] == "2027-04-01"


def test_no_route_deletes_or_edits_a_single_update():
    from app.routes import journal

    for route in journal.router.routes:
        if "updates" in route.path:
            assert route.methods == {"POST"}, route.path
        assert not ({"PUT", "PATCH"} & route.methods), route.path


# ── 목록·거르기 ──────────────────────────────────────────────────────────────

def test_list_filters_by_latest_update_and_counts_due(pg, journal, monkeypatch):
    from datetime import date

    from app.routes import journal as jr

    monkeypatch.setattr(jr, "today_kst", lambda: date(2026, 10, 4))

    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            app = make_app(factory, Who(seed["user"]))
            ids = []
            async with client(app) as c:
                for decision, review_on in (("watch", "2026-10-04"), ("consider_buy", "2026-10-05"),
                                            ("exclude", None)):
                    async with factory() as db:
                        chat_seed = {**seed}
                        from app.models import Chat
                        chat = Chat(user_id=uuid.UUID(seed["user"]["id"]), client_id="x",
                                    conversation_id=uuid.UUID(seed["conversation_id"]), question="삼성 HBM 질문",
                                    answer=ANSWER, steps=[], citations=[])
                        db.add(chat)
                        await db.commit()
                        chat_seed["chat_id"] = str(chat.id)
                    run_id = await _run_with(factory, chat_seed, "done")
                    r = await c.post("/api/journal", json=_body(run_id, decision=decision, review_on=review_on,
                                                                relied_claims=[]))
                    ids.append(r.json()["id"])
                # 첫 기록의 최근 판단을 바꾼다: 관망(오늘 다시 보기) → 매수 검토(다시 볼 날짜 없음)
                await c.post(f"/api/journal/{ids[0]}/updates", json={"decision": "consider_buy", "conviction": 5})
                out = {
                    "all": (await c.get("/api/journal")).json(),
                    "due": (await c.get("/api/journal?due=true")).json(),
                    "buy": (await c.get("/api/journal?decision=consider_buy")).json(),
                    "corp": (await c.get(f"/api/journal?corp_code={CORP}&limit=1&offset=1")).json(),
                    "other_corp": (await c.get("/api/journal?corp_code=00000000")).json(),
                    "bad_decision": await c.get("/api/journal?decision=buy"),
                }
            return out, ids

    out, ids = asyncio.run(go())
    a = out["all"]
    assert a["total"] == 3 and a["due_count"] == 0
    assert [i["id"] for i in a["items"]] == ids[::-1]  # 최근 기록 먼저
    first = a["items"][2]
    assert first["current"]["decision"] == "consider_buy" and first["current"]["conviction"] == 5
    assert first["update_count"] == 2 and first["question"] == "삼성 HBM 질문" and "memo" not in json.dumps(first)
    assert out["due"]["total"] == 0
    assert {i["id"] for i in out["buy"]["items"]} == {ids[0], ids[1]}
    assert out["corp"]["total"] == 3 and len(out["corp"]["items"]) == 1 and out["corp"]["items"][0]["id"] == ids[1]
    assert out["other_corp"]["total"] == 0
    assert out["bad_decision"].status_code == 422


def test_due_uses_latest_review_on_up_to_today_kst(pg, journal, monkeypatch):
    from datetime import date

    from app.routes import journal as jr

    monkeypatch.setattr(jr, "today_kst", lambda: date(2026, 10, 4))

    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run_id = await _judged_run(factory, seed)
            app = make_app(factory, Who(seed["user"]))
            async with client(app) as c:
                eid = (await c.post("/api/journal", json=_body(run_id, review_on="2026-10-04"))).json()["id"]
                due = (await c.get("/api/journal?due=true")).json()
                await c.post(f"/api/journal/{eid}/updates",
                             json={"decision": "watch", "conviction": 3, "review_on": "2026-10-05"})
                later = (await c.get("/api/journal")).json()
            return due, later

    due, later = asyncio.run(go())
    assert due["total"] == 1 and due["due_count"] == 1 and due["items"][0]["due"] is True
    assert later["due_count"] == 0 and later["items"][0]["due"] is False


# ── 삭제(수용 기준 11) ───────────────────────────────────────────────────────

def test_delete_one_entry_removes_updates(pg, journal):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run_id = await _judged_run(factory, seed)
            app = make_app(factory, Who(seed["user"]))
            async with client(app) as c:
                eid = (await c.post("/api/journal", json=_body(run_id))).json()["id"]
                await c.post(f"/api/journal/{eid}/updates", json={"decision": "exclude", "conviction": 1})
                r = await c.delete(f"/api/journal/{eid}")
                again = await c.get(f"/api/journal/{eid}")
            async with factory() as db:
                n = [await db.scalar(select(func.count()).select_from(m)) for m in (JudgmentEntry, JudgmentUpdate)]
            return r, again, n

    r, again, n = asyncio.run(go())
    assert (r.status_code, r.json(), again.status_code, n) == (200, {"deleted": 1}, 404, [0, 0])


@pytest.mark.parametrize("query", ["", "?confirm=", "?confirm=yes", "?confirm=DELETE-ALL"])
def test_delete_all_requires_confirm(pg, journal, query):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run_id = await _judged_run(factory, seed)
            app = make_app(factory, Who(seed["user"]))
            async with client(app) as c:
                await c.post("/api/journal", json=_body(run_id))
                r = await c.delete(f"/api/journal{query}")
            async with factory() as db:
                n = [await db.scalar(select(func.count()).select_from(m)) for m in (JudgmentEntry, JudgmentUpdate)]
            return r, n

    r, n = asyncio.run(go())
    assert r.status_code == 400 and n == [1, 1]


def test_delete_all_removes_only_my_entries(pg, journal):
    async def go():
        async with database(pg) as factory:
            a, b = await seed_user(factory), await seed_user(factory)
            ra, ra2, rb = await _judged_run(factory, a), await _run_with(factory, a, "done"), \
                await _judged_run(factory, b)
            who = Who(a["user"])
            app = make_app(factory, who)
            async with client(app) as c:
                await c.post("/api/journal", json=_body(ra))
                await c.post("/api/journal", json=_body(ra2))
                who.user = b["user"]
                await c.post("/api/journal", json=_body(rb))
                who.user = a["user"]
                r = await c.delete("/api/journal?confirm=delete-all")
                mine = (await c.get("/api/journal")).json()
                who.user = b["user"]
                theirs = (await c.get("/api/journal")).json()
            return r, mine, theirs

    r, mine, theirs = asyncio.run(go())
    assert r.status_code == 200 and r.json() == {"deleted": 2}
    assert mine["total"] == 0 and theirs["total"] == 1


def test_admin_reset_empties_journal_and_stats_count_rows(pg, journal):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run_id = await _judged_run(factory, seed)
            who = Who(seed["user"])
            app = make_app(factory, who)
            async with client(app) as c:
                eid = (await c.post("/api/journal", json=_body(run_id))).json()["id"]
                await c.post(f"/api/journal/{eid}/updates", json={"decision": "exclude", "conviction": 1})
                who.user = {**seed["user"], "roles": ["admin"]}
                stats = (await c.get("/api/admin/stats")).json()["stats"]
                reset = await c.post("/api/admin/reset")
            async with factory() as db:
                n = [await db.scalar(select(func.count()).select_from(m)) for m in (JudgmentEntry, JudgmentUpdate)]
            return stats, reset, n

    stats, reset, n = asyncio.run(go())
    assert stats["postgres.judgment_entries"] == 1 and stats["postgres.judgment_updates"] == 2
    assert MEMO not in json.dumps(stats)
    assert reset.status_code == 200 and n == [0, 0]


# ── 내보내기 ─────────────────────────────────────────────────────────────────

def test_export_json_with_notice_and_ai_marks(pg, journal, monkeypatch):
    from datetime import date

    from app.routes import journal as jr

    monkeypatch.setattr(jr, "today_kst", lambda: date(2026, 10, 4))

    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run_id = await _judged_run(factory, seed)
            app = make_app(factory, Who(seed["user"]))
            async with client(app) as c:
                eid = (await c.post("/api/journal", json=_body(run_id))).json()["id"]
                await c.post(f"/api/journal/{eid}/updates", json={"decision": "exclude", "conviction": 1})
                return await c.get("/api/journal/export"), eid

    r, eid = asyncio.run(go())
    assert r.status_code == 200
    assert r.headers["content-disposition"] == 'attachment; filename="lumina-journal-20261004.json"'
    body = r.json()
    assert body["notice"] == jr.NOTICE and "투자 권유" in body["notice"] and "외부 판정 모델" in body["notice"]
    assert body["count"] == 1
    entry = body["entries"][0]
    assert entry["id"] == eid and [u["kind"] for u in entry["updates"]] == ["initial", "revisit"]
    assert entry["updates"][0]["memo"] == MEMO and entry["updates"][0]["ai_generated"] is False
    snap = entry["snapshot"]
    assert snap["answer"] == {"text": ANSWER, "ai_generated": True}
    assert all(c["ai_generated"] is True for c in snap["claims"])
    assert snap["claims"][0]["status"] == "supported" and snap["claims"][0]["confidence"] == "보통"


# ── 메모가 밖으로 나가지 않음(수용 기준 7·8) ──────────────────────────────────

@pytest.mark.parametrize("case", ["missing_decision", "missing_run_id", "memo_too_long", "bad_decision",
                                  "bad_conviction", "not_json", "not_object"])
def test_validation_422_never_echoes_memo(pg, journal, case):
    long_memo = MEMO + "가" * 2000

    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run_id = await _judged_run(factory, seed)
            app = make_app(factory, Who(seed["user"]))
            body = _body(run_id)
            kw = {}
            if case == "missing_decision":
                body.pop("decision")
            elif case == "missing_run_id":
                body.pop("run_id")
            elif case == "memo_too_long":
                body["memo"] = long_memo
            elif case == "bad_decision":
                body["decision"] = MEMO
            elif case == "bad_conviction":
                body["conviction"] = MEMO
            elif case == "not_json":
                kw = {"content": ("{" + MEMO).encode(), "headers": {"content-type": "application/json"}}
            elif case == "not_object":
                body = [MEMO]
            async with client(app) as c:
                created = await c.post("/api/journal", **kw) if kw else await c.post("/api/journal", json=body)
                ok = await c.post("/api/journal", json=_body(run_id))
                revisit = await c.post(f"/api/journal/{ok.json()['id']}/updates", **kw) if kw else \
                    await c.post(f"/api/journal/{ok.json()['id']}/updates",
                                 json=[MEMO] if case == "not_object" else
                                 {k: v for k, v in body.items() if k != "run_id"})
            return created, revisit

    created, revisit = asyncio.run(go())
    for r in (created, revisit):
        if case == "missing_run_id" and r is revisit:
            continue  # 다시 보기에는 run_id가 없다 — 메모만 있는 정상 요청은 decision 누락 사례가 맡는다
        assert r.status_code == 422, (case, r.text)
        assert MEMO not in r.text and "가" * 50 not in r.text
        for err in r.json()["detail"]:
            assert set(err) == {"loc", "msg"}


def test_memo_never_reaches_audit_logs_or_outside(pg, journal, audit_db, caplog, capsys, monkeypatch):
    """감사 로그 payload·로그 출력 어디에도 메모가 없다."""
    caplog.set_level(logging.DEBUG)

    async def go():
        async with database(pg) as factory:
            audit_db["factory"] = factory
            seed = await seed_user(factory)
            run_id = await _judged_run(factory, seed)
            app = make_app(factory, Who(seed["user"]))
            async with client(app) as c:
                eid = (await c.post("/api/journal", json=_body(run_id))).json()["id"]
                await c.post(f"/api/journal/{eid}/updates", json={"decision": "exclude", "conviction": 1,
                                                                   "memo": MEMO + " 다시"})
                await c.post("/api/journal", json=_body(run_id))  # 409
                await c.post("/api/journal", json=_body(run_id, decision="bad"))  # 422
                await c.get("/api/journal")
                await c.get(f"/api/journal/{eid}")
                await c.get("/api/journal/export")
                await c.delete(f"/api/journal/{eid}")
            async with factory() as db:
                events = [(e.event_type, e.payload) for e in
                          (await db.execute(select(AuditEvent).order_by(AuditEvent.created_at))).scalars()]
            return events, eid

    events, eid = asyncio.run(go())
    assert events == [("journal.create", {"entry_id": eid}), ("journal.export", {"count": 1}),
                      ("journal.delete", {"count": 1})]
    out = capsys.readouterr()
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for blob in (json.dumps(events, ensure_ascii=False), logged, out.out, out.err):
        assert "비밀메모" not in blob


def test_journal_modules_do_not_import_jev_llm_or_notification():
    """일지 코드는 JEV·LLM·알림 모듈을 직접 import하지 않는다(메모가 밖으로 나갈 길이 없다, 결정 7-1·7-4)."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    files = sorted((root / "app/services/journal").glob("*.py")) + [root / "app/routes/journal.py"]
    banned = ("app.lib.jev", "app.lib.jev_service", "app.lib.llm_client", "app.lib.ollama", "app.services.notification")
    seen = []
    for f in files:
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                seen += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                seen += [node.module] + [f"{node.module}.{a.name}" for a in node.names]
    assert len(files) >= 2 and "app.services.evidence.records" in seen  # 실제로 읽었다
    assert not [m for m in seen if m.startswith(banned)]


# ── 리뷰 반영: 저장 오류 구분·NUL·정렬 ──────────────────────────────────────────

def test_create_race_on_unique_is_409_with_existing_id(pg, journal, monkeypatch):
    """존재 확인과 커밋 사이에 같은 실행의 기록이 생기면(유일 제약 uq_judgment_entries_user_run) 409와 기존 id."""
    real = records.load_claims
    holder = {}

    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run_id = await _judged_run(factory, seed)

            async def racing(db, run_ids):  # 존재 확인 뒤·커밋 전에 다른 세션이 먼저 저장한다
                async with factory() as other:
                    e = JudgmentEntry(user_id=uuid.UUID(seed["user"]["id"]), run_id=run_id, company=CO,
                                      corp_code=CORP, snapshot={}, snapshot_version="c1")
                    other.add(e)
                    await other.commit()
                    holder["competitor"] = str(e.id)
                return await real(db, run_ids)
            monkeypatch.setattr(records, "load_claims", racing)
            app = make_app(factory, Who(seed["user"]))
            async with client(app) as c:
                r = await c.post("/api/journal", json=_body(run_id))
            async with factory() as db:
                n = await db.scalar(select(func.count()).select_from(JudgmentEntry))
            return r, n

    r, n = asyncio.run(go())
    assert r.status_code == 409 and r.json()["entry_id"] == holder["competitor"] and n == 1
    assert MEMO not in r.text


def test_other_integrity_error_is_500_not_409(pg, journal, caplog):
    """유일 제약이 아닌 무결성 오류(여기서는 시험용 CHECK)는 '이미 기록이 있습니다'가 아니라 500이다."""
    caplog.set_level(logging.DEBUG)

    async def ddl(sql):
        async with database(pg) as factory, factory() as db:
            await db.execute(text(sql))
            await db.commit()

    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run_id = await _judged_run(factory, seed)
            app = make_app(factory, Who(seed["user"]))
            async with client(app) as c:
                r = await c.post("/api/journal", json=_body(run_id))
            async with factory() as db:
                n = await db.scalar(select(func.count()).select_from(JudgmentEntry))
            return r, n

    asyncio.run(ddl("ALTER TABLE judgment_entries ADD CONSTRAINT ck_test_block CHECK (company <> '삼성전자')"))
    try:
        r, n = asyncio.run(go())
    finally:
        asyncio.run(ddl("ALTER TABLE judgment_entries DROP CONSTRAINT ck_test_block"))
    assert r.status_code == 500 and "이미 기록" not in r.text and n == 0
    assert MEMO not in r.text and "비밀메모" not in "\n".join(x.getMessage() for x in caplog.records)


def test_memo_with_nul_is_422_not_500(pg, journal, caplog):
    """PostgreSQL text는 NUL을 받지 않는다. 저장 전에 422로 거르고 메모 값은 응답·로그에 넣지 않는다."""
    caplog.set_level(logging.DEBUG)
    memo = "비밀메모\u0000끝"

    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run_id = await _judged_run(factory, seed)
            app = make_app(factory, Who(seed["user"]))
            async with client(app) as c:
                created = await c.post("/api/journal", json=_body(run_id, memo=memo))
                ok = await c.post("/api/journal", json=_body(run_id))
                revisit = await c.post(f"/api/journal/{ok.json()['id']}/updates",
                                       json={"decision": "watch", "conviction": 2, "memo": memo})
            async with factory() as db:
                n = await db.scalar(select(func.count()).select_from(JudgmentUpdate))
            return created, revisit, n

    created, revisit, n = asyncio.run(go())
    for r in (created, revisit):
        assert r.status_code == 422, r.text
        assert r.json()["detail"][0]["loc"] == ["body", "memo"] and set(r.json()["detail"][0]) == {"loc", "msg"}
        assert "비밀메모" not in r.text and "끝" not in r.text
    assert n == 1  # 정상 기록의 initial 1건만
    assert "비밀메모" not in "\n".join(x.getMessage() for x in caplog.records)


def test_updates_with_same_created_at_are_ordered_by_id(pg, journal):
    """같은 created_at의 update가 둘이면 id로 보조 정렬한다(상세·내보내기·현재 판단이 매번 같다)."""
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run_id = await _judged_run(factory, seed)
            app = make_app(factory, Who(seed["user"]))
            async with client(app) as c:
                eid = (await c.post("/api/journal", json=_body(run_id, review_on=None))).json()["id"]
                at = records.now()
                hi, lo = uuid.UUID(int=(1 << 128) - 1), uuid.UUID(int=1)
                async with factory() as db:  # 큰 id를 먼저 넣는다(삽입 순서와 id 순서가 반대)
                    db.add(JudgmentUpdate(id=hi, entry_id=uuid.UUID(eid), kind="revisit", decision="exclude",
                                          conviction=1, memo="", relied_claims=[], created_at=at))
                    await db.commit()
                async with factory() as db:
                    db.add(JudgmentUpdate(id=lo, entry_id=uuid.UUID(eid), kind="revisit", decision="consider_buy",
                                          conviction=5, memo="", relied_claims=[], created_at=at))
                    await db.commit()
                detail = (await c.get(f"/api/journal/{eid}")).json()
                listed = (await c.get("/api/journal")).json()
                export = (await c.get("/api/journal/export")).json()
            return detail, listed, export, str(lo), str(hi)

    detail, listed, export, lo, hi = asyncio.run(go())
    assert [u["id"] for u in detail["updates"]][1:] == [lo, hi]
    assert [u["id"] for u in export["entries"][0]["updates"]][1:] == [lo, hi]
    assert listed["items"][0]["current"]["decision"] == "exclude"  # 최근 = 정렬의 마지막(id가 큰 쪽)
