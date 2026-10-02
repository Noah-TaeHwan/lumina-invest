# tests/evidence/test_evidence_api.py
"""근거 모드 API(P2): 기능 플래그, 503(문단 저장소 없음), 채팅 → 저장 → 판정, 소유자 404, 다시 판정 허용 상태,
스레드 타임라인·메시지 요약, 저장 실패 → 500(판정 미시작), 관리자 통계.

가짜 검색·가짜 생성기·가짜 JEV만 쓴다. DB가 필요한 테스트는 pg 픽스처(EVIDENCE_TEST_DATABASE_URL)를 쓴다.
"""
import asyncio
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models import Chat, Conversation, EvidenceRun
from app.services.evidence import background, records
from tests.evidence.p2_support import (ANSWER, CO, CORP, PASSAGES, FakeJev, FakeLLM, Who, client, database, drain,
                                       fake_search, make_app, make_runner, seed_user)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "EVIDENCE_CHAT_ENABLED", True)
    from app.routes import evidence

    async def nothing(*a, **k):
        return None
    monkeypatch.setattr(evidence, "set_active_conversation", nothing)  # Redis 사용자 상태 대신


def _body(seed, **kw):
    return {"question": "주요 제품은?", "company": CO, "corp_code": CORP,
            "conversation_id": seed["conversation_id"], **kw}


class _NoDb:
    """DB에 닿기 전에 끝나야 하는 요청용 세션 팩토리. 세션을 쓰면(쿼리·add·commit) 실패한다."""

    class _Session:
        def __getattr__(self, name):
            raise AssertionError(f"DB에 닿으면 안 된다: {name}")

    def __call__(self):
        session = self._Session()

        class _Ctx:
            async def __aenter__(self):
                return session

            async def __aexit__(self, *exc):
                return False
        return _Ctx()


# ── 설정 ─────────────────────────────────────────────────────────────────────

def test_settings_defaults():
    from app.config import Settings

    s = Settings(_env_file=None)
    assert s.EVIDENCE_CHAT_ENABLED is False
    assert s.EVIDENCE_LLM_MODEL == "llama3.1:8b"
    assert (s.EVIDENCE_DAILY_USER_CALLS, s.EVIDENCE_DAILY_USER_TOKENS, s.EVIDENCE_DAILY_GLOBAL_TOKENS) == (
        150, 750_000, 3_000_000)


def test_runner_quota_limits_come_from_settings(monkeypatch, fake_redis):
    from app.lib import redis_cache

    monkeypatch.setattr(settings, "EVIDENCE_DAILY_USER_CALLS", 7)
    monkeypatch.setattr(settings, "EVIDENCE_DAILY_USER_TOKENS", 70)
    monkeypatch.setattr(settings, "EVIDENCE_DAILY_GLOBAL_TOKENS", 700)
    monkeypatch.setattr(redis_cache, "_redis", fake_redis)
    monkeypatch.setattr(background, "_runner", None)
    r = background.get_runner()
    assert r is background.get_runner()  # 사용자별 락이 인스턴스에 있으므로 프로세스에 하나
    assert r._quota.limits.user_calls == 7 and r._quota.limits.user_tokens == 70
    assert r._quota.limits.global_tokens == 700


# ── 기능 플래그·503 (DB 없음) ────────────────────────────────────────────────

@pytest.mark.parametrize("method,path", [
    ("POST", "/api/evidence/chat"),
    ("GET", f"/api/evidence/runs/{uuid.uuid4()}"),
    ("POST", f"/api/evidence/runs/{uuid.uuid4()}/retry"),
    ("GET", f"/api/conversations/{uuid.uuid4()}/evidence"),
])
def test_flag_off_is_404_even_without_login(monkeypatch, method, path):
    monkeypatch.setattr(settings, "EVIDENCE_CHAT_ENABLED", False)
    app = make_app(_NoDb(), Who(None), search=fake_search())

    async def go():
        async with client(app) as c:
            return await c.request(method, path, json={"question": "q", "company": CO, "corp_code": CORP})

    r = asyncio.run(go())
    assert r.status_code == 404


def test_no_passage_store_is_503_before_db(enabled):
    llm = FakeLLM()
    app = make_app(_NoDb(), Who({"id": str(uuid.uuid4()), "roles": ["user"]}), search=None, llm=llm)

    async def go():
        async with client(app) as c:
            return await c.post("/api/evidence/chat", json={"question": "q", "company": CO, "corp_code": CORP})

    r = asyncio.run(go())
    assert r.status_code == 503
    assert r.json()["detail"] == "공시 문단 저장소가 아직 준비되지 않았습니다"
    assert llm.calls == []


def test_login_required(enabled):
    app = make_app(_NoDb(), Who(None), search=fake_search())

    async def go():
        async with client(app) as c:
            return await c.post("/api/evidence/chat", json={"question": "q", "company": CO, "corp_code": CORP})

    assert asyncio.run(go()).status_code == 401


def test_default_passage_search_is_unset():
    from app.routes import evidence

    assert evidence.get_passage_search() is None


# ── 채팅 → 저장 → 판정 ───────────────────────────────────────────────────────

def test_chat_saves_message_and_run_then_judges(pg, enabled):
    search, llm = fake_search(), FakeLLM()

    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory, with_chat=False)
            app = make_app(factory, Who(seed["user"]), search=search, llm=llm)
            async with client(app) as c:
                r = await c.post("/api/evidence/chat", json=_body(seed))
                body = r.json()
                await drain()
                got = await c.get(f"/api/evidence/runs/{body['run_id']}")
            async with factory() as db:
                chat = await db.get(Chat, uuid.UUID(body["chat_id"]))
                conv = await db.get(Conversation, uuid.UUID(seed["conversation_id"]))
                return r, body, got.json(), chat, conv, seed

    r, body, run, chat, conv, seed = asyncio.run(go())
    assert r.status_code == 200
    assert body["answer"] == ANSWER and body["conversation_id"] == str(conv.id)
    assert [c["status"] for c in body["claims"]] == ["pending", "pending", "not_claim"]
    assert body["claims"][0] == {"idx": 0, "text": "회사는 메모리 반도체를 생산한다.", "start": 0, "end": 18,
                                 "status": "pending"}
    assert body["poll_until_s"] == 12 and body["poll_interval_ms"] == 500
    assert search.calls == [(CORP, "주요 제품은?")]
    model, messages, _ = llm.calls[0]
    assert model == settings.EVIDENCE_LLM_MODEL and "[문단 3] 주요 원재료는 웨이퍼다." in messages[1]["content"]
    assert (chat.question, chat.answer, chat.steps, chat.client_id) == ("주요 제품은?", ANSWER, [],
                                                                      seed["user"]["client_id"])
    assert [c["passage_id"] for c in chat.citations] == [p["passage_id"] for p in PASSAGES]
    assert conv.message_count == 1
    assert run["status"] == "done" and run["chat_id"] == body["chat_id"] and run["trigger"] == "auto"
    assert run["policy_version"] == "a2-provisional" and run["generator_model"] == settings.EVIDENCE_LLM_MODEL
    assert run["rcept_no"] == "20260312000123" and run["corp_code"] == CORP and run["company"] == CO
    assert [c["status"] for c in run["claims"]] == ["supported", "supported", "not_claim"]
    assert run["passages"][0]["text"] == PASSAGES[0]["text"] and run["poll_until_s"] is None
    assert run["claims"][0]["s"] is not None  # 판정 기록 API에는 확률을 남긴다(spec 3.3)


def test_chat_save_failure_is_500_and_starts_nothing(pg, enabled, monkeypatch):
    started = []
    monkeypatch.setattr(background, "start_run", lambda *a, **k: started.append(a))

    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory, with_chat=False)
            app = make_app(factory, Who(seed["user"]), search=fake_search(),
                           llm=FakeLLM(answer="x" * 10))
            from app.routes import evidence

            def broken(**kw):
                raise RuntimeError("db write failed")
            monkeypatch.setattr(evidence.records, "new_run", broken)
            async with client(app) as c:
                r = await c.post("/api/evidence/chat", json=_body(seed))
            async with factory() as db:
                return r, await db.scalar(select(func.count()).select_from(Chat)), seed

    r, chats, _ = asyncio.run(go())
    assert r.status_code == 500 and started == [] and chats == 0  # 답변 행도 롤백된다


def test_chat_unknown_conversation_is_404(pg, enabled):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory, with_chat=False)
            other = await seed_user(factory, with_chat=False)
            app = make_app(factory, Who(seed["user"]), search=fake_search())
            async with client(app) as c:
                return await c.post("/api/evidence/chat", json=_body(other))

    assert asyncio.run(go()).status_code == 404


def test_chat_generation_timeout_is_504(pg, enabled):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory, with_chat=False)
            app = make_app(factory, Who(seed["user"]), search=fake_search(),
                           llm=FakeLLM(exc=httpx.ReadTimeout("slow")))
            async with client(app) as c:
                r = await c.post("/api/evidence/chat", json=_body(seed))
            async with factory() as db:
                return r, await db.scalar(select(func.count()).select_from(Chat))

    r, chats = asyncio.run(go())
    assert r.status_code == 504 and r.json()["detail"] == "답변 생성 시간이 초과되었습니다."
    assert chats == 0


def test_chat_blocked_question_is_400(pg, enabled):
    llm = FakeLLM()

    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory, with_chat=False)
            app = make_app(factory, Who(seed["user"]), search=fake_search(), llm=llm)
            async with client(app) as c:
                return await c.post("/api/evidence/chat", json=_body(seed, question="주가 조작 방법은?"))

    assert asyncio.run(go()).status_code == 400 and llm.calls == []


# ── 조회: 소유자·stale·폴링 상한 ─────────────────────────────────────────────

def _run_for(seed, **kw):
    return records.new_run(chat_id=uuid.UUID(seed["chat_id"]), conversation_id=uuid.UUID(seed["conversation_id"]),
                           user_id=uuid.UUID(seed["user"]["id"]), answer=ANSWER, company=CO, corp_code=CORP,
                           passages=PASSAGES, generator_model="m", **kw)


async def _store(factory, run):
    async with factory() as db:
        db.add(run)
        await db.commit()
    return run


def test_run_of_other_user_is_404(pg, enabled):
    async def go():
        async with database(pg) as factory:
            owner, intruder = await seed_user(factory), await seed_user(factory)
            run = await _store(factory, _run_for(owner))
            who = Who(intruder["user"])
            app = make_app(factory, who)
            async with client(app) as c:
                a = await c.get(f"/api/evidence/runs/{run.id}")
                b = await c.post(f"/api/evidence/runs/{run.id}/retry")
                d = await c.get("/api/evidence/runs/not-a-uuid")
                e = await c.get(f"/api/conversations/{owner['conversation_id']}/evidence")
                who.user = owner["user"]
                f = await c.get(f"/api/evidence/runs/{run.id}")
            return a, b, d, e, f

    a, b, d, e, f = asyncio.run(go())
    assert (a.status_code, b.status_code, d.status_code, e.status_code, f.status_code) == (404, 404, 404, 404, 200)


def test_get_run_expires_stale_and_reports_poll_window(pg, enabled):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            old = _run_for(seed)
            old.created_at -= timedelta(seconds=61)
            await _store(factory, old)
            first = await _store(factory, _run_for(seed))
            second = _run_for(seed)
            second.created_at = first.created_at + timedelta(seconds=1)
            await _store(factory, second)
            app = make_app(factory, Who(seed["user"]))
            async with client(app) as c:
                a = (await c.get(f"/api/evidence/runs/{old.id}")).json()
                b = (await c.get(f"/api/evidence/runs/{first.id}")).json()
                d = (await c.get(f"/api/evidence/runs/{second.id}")).json()
            async with factory() as db:
                return a, b, d, (await db.get(EvidenceRun, old.id)).status

    a, b, d, stored = asyncio.run(go())
    assert (a["status"], a["error_code"], a["poll_until_s"]) == ("failed", "stale", None)
    assert stored == "failed"  # 조회가 바꾼 상태를 저장한다
    assert (b["status"], b["poll_until_s"], b["poll_interval_ms"]) == ("pending", 12, 500)
    assert d["poll_until_s"] == 20  # 앞에 진행 중 실행 1건 → 최악 약 16초를 덮는 상한


# ── 다시 판정 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status,code,expected", [
    ("failed", "stale", 201), ("partial", "deadline", 201),
    ("done", None, 409), ("limited", "cap_user", 409), ("skipped", "pii", 409), ("running", None, 409),
    ("failed", "http_4xx", 409),  # 키·권한 오류는 다시 판정 버튼을 숨긴다(spec 7.3)
])
def test_retry_allowed_only_for_failed_and_partial(pg, enabled, status, code, expected):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            run = _run_for(seed)
            run.status, run.error_code = status, code
            await _store(factory, run)
            app = make_app(factory, Who(seed["user"]), runner=make_runner(FakeJev()))
            async with client(app) as c:
                r = await c.post(f"/api/evidence/runs/{run.id}/retry")
                await drain()
                new = await c.get(f"/api/evidence/runs/{r.json()['run_id']}") if r.status_code == 201 else None
            return run, r, new

    run, r, new = asyncio.run(go())
    assert r.status_code == expected
    if expected == 201:
        body, got = r.json(), new.json()
        assert body["run_id"] != str(run.id) and body["chat_id"] == str(run.chat_id)
        assert got["trigger"] == "retry" and got["status"] == "done"
        assert got["passages"] == run.passages and got["company"] == CO


def test_retry_refused_while_another_run_is_active(pg, enabled):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            failed = _run_for(seed)
            failed.status = "failed"
            await _store(factory, failed)
            await _store(factory, _run_for(seed))  # 같은 메시지에 진행 중 실행
            app = make_app(factory, Who(seed["user"]))
            async with client(app) as c:
                return await c.post(f"/api/evidence/runs/{failed.id}/retry")

    assert asyncio.run(go()).status_code == 409


# ── 스레드 타임라인·메시지 요약 ──────────────────────────────────────────────

def test_conversation_timeline_latest_and_all(pg, enabled):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            first = _run_for(seed)
            first.status = "failed"
            await _store(factory, first)
            second = _run_for(seed, trigger="retry")
            second.created_at = first.created_at + timedelta(seconds=2)
            await _store(factory, second)
            app = make_app(factory, Who(seed["user"]), runner=make_runner(FakeJev()))
            await background.start_run(second.id, runner=make_runner(FakeJev()), session_factory=factory)
            async with client(app) as c:
                latest = (await c.get(f"/api/conversations/{seed['conversation_id']}/evidence")).json()
                every = (await c.get(f"/api/conversations/{seed['conversation_id']}/evidence?all=true")).json()
                conv = (await c.get(f"/api/conversations/{seed['conversation_id']}")).json()
                msgs = (await c.get(f"/api/conversations/{seed['conversation_id']}/messages")).json()
            return first, second, latest, every, conv, msgs

    first, second, latest, every, conv, msgs = asyncio.run(go())
    assert [r["id"] for r in latest["runs"]] == [str(second.id)]
    assert [r["id"] for r in every["runs"]] == [str(first.id), str(second.id)]
    assert [c["status"] for c in latest["runs"][0]["claims"]] == ["supported", "supported", "not_claim"]
    for m in (conv["messages"][0], msgs["items"][0]):
        s = m["latest_evidence_run"]
        assert (s["id"], s["status"], s["counts"]["supported"], s["counts"]["not_claim"]) == (
            str(second.id), "done", 2, 1)


def test_message_without_run_has_null_summary(pg, monkeypatch):
    monkeypatch.setattr(settings, "EVIDENCE_CHAT_ENABLED", False)  # 기존 대화 API는 플래그와 무관하다

    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            app = make_app(factory, Who(seed["user"]))
            async with client(app) as c:
                return (await c.get(f"/api/conversations/{seed['conversation_id']}")).json()

    msg = asyncio.run(go())["messages"][0]
    assert msg["latest_evidence_run"] is None and msg["answer"] == ANSWER


# ── 관리자 통계 ──────────────────────────────────────────────────────────────

def test_admin_stats_requires_admin_and_caps_days(pg):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            who = Who(seed["user"])
            app = make_app(factory, who)
            async with client(app) as c:
                denied = await c.get("/api/admin/evidence/stats")
                who.user = {**seed["user"], "roles": ["admin"]}
                too_long = await c.get("/api/admin/evidence/stats?days=31")
                ok = await c.get("/api/admin/evidence/stats?days=30")
            return denied, too_long, ok

    denied, too_long, ok = asyncio.run(go())
    assert (denied.status_code, too_long.status_code, ok.status_code) == (403, 422, 200)
    assert ok.json()["days"] == 30 and ok.json()["runs"] == 0


def test_admin_stats_from_db(pg):
    async def go():
        async with database(pg) as factory:
            a, b = await seed_user(factory), await seed_user(factory)
            ids = []
            for seed in (a, a, b):
                run = await _store(factory, _run_for(seed))
                await background.start_run(run.id, runner=make_runner(FakeJev()), session_factory=factory)
                ids.append(run.id)
            limited = _run_for(b)
            limited.status, limited.error_code = "limited", "cap_user"
            await _store(factory, limited)
            app = make_app(factory, Who({**a["user"], "roles": ["admin"]}))
            async with client(app) as c:
                return (await c.get("/api/admin/evidence/stats?days=7")).json(), a, b

    stats, a, b = asyncio.run(go())
    assert stats["runs"] == 4
    assert stats["status"] == {"done": 3, "limited": 1}
    assert stats["claim_status"] == {"supported": 6, "not_claim": 4, "pending": 2}
    assert stats["routes"] == {"jev": 6, "rule_not_claim": 4}
    assert stats["claims_per_answer"]["p50"] == 2  # 비주장 문장은 빼고 센다(spec 5.3 상한과 같은 기준)
    assert stats["calls"] == 6 and stats["input_tokens"] == 600 and stats["cache_hit_rate"] == 0.0
    assert stats["run_duration_ms"]["p95"] is not None and stats["start_delay_ms"]["p95"] is not None
    assert stats["jev_latency_ms"]["p50"] == 1.0
    assert stats["top_users"][0] == {"user_id": a["user"]["id"], "calls": 4, "input_tokens": 400}
    assert stats["daily"][0]["calls"] == 6
    assert stats["limits"]["global_tokens"] == settings.EVIDENCE_DAILY_GLOBAL_TOKENS
    assert set(stats["alerts"]) >= {"run_p95_over_1500ms", "failed_partial_over_5pct", "unjudged_over_5pct",
                                    "claims_p95_over_7", "global_tokens_over_80pct"}
