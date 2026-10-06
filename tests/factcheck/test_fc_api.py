# tests/factcheck/test_fc_api.py
"""팩트체커 익명 API(T3 + 검수 결과 반영): 회사 목록·검수 실행·결과 폴링·건너뛴 문장 수동 검수.

- 파이프라인은 T2와 같은 모양의 가짜(fc_support.FakePipeline): 요청마다 for_user(익명 키) 사본,
  check(corp_code, text, *, as_of=None, force_check=False). 유료 JEV는 계량 래퍼(MeteredJev)를 거친다.
- 예약 = 문장 전부(+분류 묶음 1회) × 요청 상한 × 최대 시도. 정산은 결과 스트림이 아니라 호출별 원장으로 한다.
- 입력: 본문 16KB(읽는 단계에서), 2,000자, 입력 전체 문장 30개. 교차 출처 POST는 Origin·Content-Type으로 막는다.
- job 저장소는 개수·메모리·동시 실행 상한이 있고, TTL 만료는 백그라운드 sweeper가 지우며 작업을 취소·대기한다.
- job_id + 익명 쿠키가 둘 다 맞아야 조회(아니면 같은 404). 익명 원문은 DB·로그에 남기지 않는다.
DB가 필요한 테스트는 fc_pg 픽스처를 쓴다.
"""
import asyncio
import logging

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services.evidence.claims import claim_spans
from app.services.factcheck import jobs as fj
from app.services.factcheck import metering as fm
from app.services.factcheck import quota as fq
from tests.factcheck.fc_support import FakePipeline, FakeServiceJev

SAMSUNG, HYNIX = "00126380", "00164779"
MARKER = "표식ZQX9-원문"
DRAFT = (f"삼성전자의 2026년 상반기 매출은 153조원이다. {MARKER} 문장은 표식이다. 메모리 사업은 성장했다. "
         "앞으로도 좋을까? 2025년 영업이익은 32조 7,260억원이다.")
ORIGIN = {"origin": "http://t"}
BIG = 10 ** 9


class Env:
    """테스트용 앱과 그 부품(가짜 파이프라인·계량 JEV·한도·job 저장소·익명 키)."""

    def __init__(self, url, *, jev=None, pipeline=None, limits=None, jobs=None, metered=True, **pl_kw):
        self.engine = create_async_engine(url)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        self.jev = jev or FakeServiceJev()
        self.pipeline = pipeline or FakePipeline(fm.MeteredJev(self.jev) if metered else self.jev, **pl_kw)
        self.quota = fq.FactcheckQuota(self.factory, limits or fq.QuotaLimits(runs=3, key_tokens=BIG, global_tokens=BIG))
        self.jobs = jobs or fj.JobStore()
        self.keyer = fq.AnonKeyer(self.factory)
        from app.routes import factcheck

        self.app = FastAPI()
        self.app.include_router(factcheck.router)
        o = self.app.dependency_overrides
        o[factcheck.get_pipeline] = lambda: factcheck.require_metered(self.pipeline)
        o[factcheck.get_quota] = lambda: self.quota
        o[factcheck.get_jobs] = lambda: self.jobs
        o[factcheck.get_keyer] = lambda: self.keyer

    def client(self, client_ip="127.0.0.1", headers=None):
        transport = httpx.ASGITransport(app=self.app, client=(client_ip, 1234))
        return httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers or ORIGIN)

    async def close(self):
        await self.jobs.close()
        await self.engine.dispose()


def body(text_=DRAFT, corp=SAMSUNG, source="my_draft"):
    return {"corp_code": corp, "text": text_, "source": source}


async def quota_rows(env) -> dict:
    async with env.engine.connect() as conn:
        rows = (await conn.execute(text("SELECT key, count, used, reserved FROM factcheck_quota"))).all()
    return {r.key: (r.count, r.used, r.reserved) for r in rows}


async def reservations(env) -> list:
    async with env.engine.connect() as conn:
        return [tuple(r) for r in (await conn.execute(text(
            "SELECT est, actual, count_run, settled_at IS NOT NULL FROM factcheck_reservations ORDER BY created_at"))).all()]


async def poll_done(c, job_id, limit=300):
    for _ in range(limit):
        r = await c.get(f"/api/factcheck/{job_id}")
        assert r.status_code == 200, r.text
        if r.json()["status"] != "running":
            return r.json()
        await asyncio.sleep(0.01)
    raise AssertionError("job이 끝나지 않았다")


def run(url, coro_fn, **kw):
    async def go():
        env = Env(url, **kw)
        try:
            return await coro_fn(env)
        finally:
            await env.close()
    return asyncio.run(go())


def slot(sentence):
    """가짜 파이프라인이 그 문장으로 보내는 요청의 원장 한 몫(상한 × 최대 시도)."""
    return fm.request_bound(f"회사: {SAMSUNG}\n주장: {sentence}", {"p1": {"type": "choice"}}) * fm.MAX_ATTEMPTS


# ── 회사 목록 ────────────────────────────────────────────────────────────────

def test_companies_lists_only_the_two_demo_companies(fc_pg):
    async def go(env):
        async with env.client() as c:
            return await c.get("/api/factcheck/companies")

    r = run(fc_pg, go)
    assert r.status_code == 200
    data = r.json()
    assert [(x["corp_code"], x["corp_name"]) for x in data["companies"]] == [(SAMSUNG, "삼성전자"), (HYNIX, "SK하이닉스")]
    assert "삼성전자·SK하이닉스" in data["scope"] and data["max_sentences"] == 30


# ── 검수 실행·폴링·정산 ─────────────────────────────────────────────────────

def test_post_reserves_max_for_all_sentences_and_settles_from_call_ledger(fc_pg):
    async def go(env):
        async with env.client() as c:
            r = await c.post("/api/factcheck", json=body())
            assert r.status_code == 202, r.text
            started, cookie = r.json(), r.headers.get("set-cookie", "")
            mid = await reservations(env)
            done = await poll_done(c, started["job_id"])
        key = await env.keyer.key("127.0.0.1")
        return started, cookie, mid, done, await quota_rows(env), await reservations(env), key

    started, cookie, mid, done, rows, res, key = run(fc_pg, go)
    assert len(started["job_id"]) >= 43 and started["total"] == 5 and started["poll_interval_ms"] == 1000
    assert "fc_anon=" in cookie and "httponly" in cookie.lower() and "samesite=lax" in cookie.lower()
    # 예약: 비주장도 빼지 않고 문장 5개 + 분류 묶음 1회, 요청 상한 × 최대 시도
    assert mid == [(fm.reservation_for(5), None, True, False)]
    assert done["status"] == "done" and done["poll_interval_ms"] is None
    assert [x["idx"] for x in done["results"]] == [0, 1, 2, 3, 4]
    assert done["counts"] == {"supported": 4, "contradicted": 0, "no_evidence": 0, "unjudged": 0, "skipped": 1}
    assert set(done["results"][0]) == {"idx", "text", "category", "status", "evidence", "xbrl", "reason"}
    # 정산: 원장의 실제 보고 토큰(호출 4번 × 1,000), 예약은 0
    assert res == [(fm.reservation_for(5), 4_000, True, True)]
    assert rows[key] == (1, 4_000, 0) and rows["global"] == (1, 4_000, 0)


def test_pipeline_copy_per_request_uses_anon_key(fc_pg):
    async def go(env):
        async with env.client() as c:
            job = (await c.post("/api/factcheck", json=body())).json()["job_id"]
            await poll_done(c, job)
        return env.pipeline.users, env.pipeline.calls, env.jev.users, await env.keyer.key("127.0.0.1")

    users, calls, jev_users, key = run(fc_pg, go)
    assert users == [key]
    assert calls == [(SAMSUNG, DRAFT, None, False, key)]
    assert set(jev_users) == {key}


def test_poll_shows_sentence_results_as_they_finish(fc_pg):
    gate = asyncio.Event()

    async def go(env):
        async with env.client() as c:
            job = (await c.post("/api/factcheck", json=body())).json()["job_id"]
            seen = []
            for _ in range(3):
                gate.set()
                for _ in range(100):
                    data = (await c.get(f"/api/factcheck/{job}")).json()
                    if len(data["results"]) > (seen[-1] if seen else 0):
                        break
                    await asyncio.sleep(0.01)
                seen.append(len(data["results"]))
                assert data["status"] == "running" and data["poll_interval_ms"] == 1000
            while (await c.get(f"/api/factcheck/{job}")).json()["status"] == "running":
                gate.set()
                await asyncio.sleep(0.01)
        return seen

    assert run(fc_pg, go, gate=gate) == [1, 2, 3]


def test_unknown_usage_is_charged_at_slot_not_zero(fc_pg):
    async def go(env):
        async with env.client() as c:
            job = (await c.post("/api/factcheck", json=body())).json()["job_id"]
            done = await poll_done(c, job)
        return done, await quota_rows(env)

    done, rows = run(fc_pg, go, jev=FakeServiceJev(exc=RuntimeError("network")))
    checked = [s.text for s in claim_spans(DRAFT)][:1]  # 첫 검수 문장에서 예외 → 실행 실패
    assert done["status"] == "failed"
    assert rows["global"][1:] == (slot(checked[0]), 0)  # 모르는 사용량은 그 호출의 상한 그대로


def test_pipeline_error_marks_job_failed_and_settles(fc_pg):
    async def go(env):
        async with env.client() as c:
            job = (await c.post("/api/factcheck", json=body())).json()["job_id"]
            done = await poll_done(c, job)
        return done, await quota_rows(env), await reservations(env)

    done, rows, res = run(fc_pg, go, exc=RuntimeError(f"boom {MARKER}"))
    assert done["status"] == "failed" and done["error"]["code"] == "pipeline_error"
    assert MARKER not in str(done["error"])
    assert len(done["results"]) == 1
    assert rows["global"] == (1, 1_000, 0) and res[0][3] is True  # 나간 호출 1번만 정산, 예약은 풀림


def test_pipeline_missing_or_unmetered_returns_503_without_quota(fc_pg):
    from app.routes import factcheck

    async def missing(env):
        env.app.dependency_overrides[factcheck.get_pipeline] = factcheck.get_pipeline
        factcheck.configure(quota=None, pipeline=None)
        async with env.client() as c:
            r = await c.post("/api/factcheck", json=body())
        return r, await quota_rows(env)

    async def unmetered(env):
        async with env.client() as c:
            r = await c.post("/api/factcheck", json=body())
        return r, await quota_rows(env), env.jev.calls

    r, rows = run(fc_pg, missing)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "pipeline_unavailable" and rows == {}
    r, rows, calls = run(fc_pg, unmetered, metered=False)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "unmetered" and rows == {} and calls == 0


# ── 입력 검증 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("payload,code", [
    (body(text_="가" * 1995 + f" {MARKER}"), "too_long"),
    (body(text_="   "), "empty"),
    (body(source="newsletter"), "bad_source"),
    (body(corp="00266961"), "bad_company"),
    ({"corp_code": SAMSUNG, "source": "my_draft", "text": 123}, "empty"),
])
def test_invalid_input_is_422_with_guidance_and_no_echo(fc_pg, payload, code):
    async def go(env):
        async with env.client() as c:
            r = await c.post("/api/factcheck", json=payload)
        return r, env.pipeline.calls, await quota_rows(env)

    r, calls, rows = run(fc_pg, go)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == code and r.json()["detail"]["message"]
    assert MARKER not in r.text
    assert calls == [] and rows == {}


def test_more_than_30_sentences_in_input_is_422_even_if_opinions(fc_pg):
    over = " ".join(f"{2000 + i}년 매출은 {i}조원이다." for i in range(29)) + " 좋을까? 그럴까?"  # 31문장(비주장 2)
    exact = " ".join(f"{2000 + i}년 매출은 {i}조원이다." for i in range(29)) + " 좋을까?"

    async def go(env):
        async with env.client() as c:
            return await c.post("/api/factcheck", json=body(text_=over)), await c.post("/api/factcheck",
                                                                                       json=body(text_=exact))

    r1, r2 = run(fc_pg, go)
    assert r1.status_code == 422
    d = r1.json()["detail"]
    assert d["code"] == "too_many_sentences" and d["limit"] == 30 and d["sentences"] == 31 and "30" in d["message"]
    assert r2.status_code == 202 and r2.json()["total"] == 30


def test_body_over_16kb_is_413_while_reading(fc_pg):
    big = ("{" + '"text": "' + "가" * 6000 + '"}').encode()  # 18KB

    async def stream():
        for i in range(0, len(big), 1024):
            yield big[i:i + 1024]

    async def go(env):
        async with env.client() as c:
            declared = await c.post("/api/factcheck", content=big, headers={"content-type": "application/json"})
            chunked = await c.post("/api/factcheck", content=stream(), headers={"content-type": "application/json"})
        return declared, chunked, await quota_rows(env)

    declared, chunked, rows = run(fc_pg, go)
    assert declared.status_code == chunked.status_code == 413
    assert declared.json()["detail"]["code"] == chunked.json()["detail"]["code"] == "too_large"
    assert rows == {}


def test_malformed_json_is_422_without_echo(fc_pg):
    async def go(env):
        async with env.client() as c:
            return await c.post("/api/factcheck", content=f'{{"text": "{MARKER}"'.encode(),
                                headers={"content-type": "application/json"})

    r = run(fc_pg, go)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "bad_json" and MARKER not in r.text


# ── 교차 출처 POST ──────────────────────────────────────────────────────────

def test_cross_origin_post_and_non_json_content_type_are_refused(fc_pg):
    async def go(env):
        async with env.client(headers={"origin": "https://evil.example"}) as c:
            evil = await c.post("/api/factcheck", json=body())
        async with env.client(headers={"origin": "null"}) as c:
            null = await c.post("/api/factcheck", json=body())
        async with env.client() as c:
            form = await c.post("/api/factcheck", content=b"corp_code=00126380",
                                headers={"content-type": "application/x-www-form-urlencoded"})
            plain = await c.post("/api/factcheck", content=b"{}", headers={"content-type": "text/plain"})
        async with env.client(headers={}) as c:  # Origin 없음(브라우저 밖): JSON이면 받는다
            no_origin = await c.post("/api/factcheck", json=body())
        await env.jobs.drain()
        return evil, null, form, plain, no_origin, env.pipeline.calls

    evil, null, form, plain, no_origin, calls = run(fc_pg, go)
    assert evil.status_code == null.status_code == 403 and evil.json()["detail"]["code"] == "bad_origin"
    assert form.status_code == plain.status_code == 415
    assert no_origin.status_code == 202
    assert len(calls) == 1


def test_origin_check_runs_before_pipeline_and_quota_dependencies(fc_pg):
    from app.routes import factcheck

    async def go(env):
        env.app.dependency_overrides[factcheck.get_pipeline] = factcheck.get_pipeline
        factcheck.configure(quota=None, pipeline=None)  # 엔진이 없어도 교차 출처는 403
        async with env.client(headers={"origin": "https://evil.example"}) as c:
            return await c.post("/api/factcheck", json=body()), await c.post("/api/factcheck/x/recheck/1", json={})

    a, b = run(fc_pg, go)
    assert a.status_code == b.status_code == 403


def test_allowed_origins_setting_is_exact(fc_pg, monkeypatch):
    from app.routes import factcheck

    monkeypatch.setattr(factcheck.FC, "FACTCHECK_ALLOWED_ORIGINS", "https://fc.example")

    async def go(env):
        async with env.client(headers={"origin": "https://fc.example"}) as c:
            ok = await c.post("/api/factcheck", json=body())
        async with env.client() as c:  # Host와 같아도 목록에 없으면 거부
            same_host = await c.post("/api/factcheck", json=body())
        return ok, same_host

    ok, same_host = run(fc_pg, go)
    assert ok.status_code == 202 and same_host.status_code == 403


# ── 쿠키 결속·만료·sweeper ──────────────────────────────────────────────────

def test_other_cookie_or_no_cookie_gets_same_404_as_unknown_job(fc_pg):
    async def go(env):
        async with env.client() as owner:
            job = (await owner.post("/api/factcheck", json=body())).json()["job_id"]
            await poll_done(owner, job)
            async with env.client() as stranger:
                await stranger.post("/api/factcheck", json=body(text_="2025년 매출은 300조원이다."))
                other = await stranger.get(f"/api/factcheck/{job}")
            async with env.client() as anon:
                none = await anon.get(f"/api/factcheck/{job}")
            unknown = await owner.get("/api/factcheck/" + "x" * 43)
            mine = await owner.get(f"/api/factcheck/{job}")
        return other, none, unknown, mine

    other, none, unknown, mine = run(fc_pg, go)
    assert mine.status_code == 200
    assert other.status_code == none.status_code == unknown.status_code == 404
    assert other.json() == none.json() == unknown.json()


def test_job_expires_after_15_minutes(fc_pg):
    clock = [1000.0]
    store = fj.JobStore(now=lambda: clock[0])

    async def go(env):
        async with env.client() as c:
            job = (await c.post("/api/factcheck", json=body())).json()["job_id"]
            await poll_done(c, job)
            clock[0] += 14 * 60
            alive = await c.get(f"/api/factcheck/{job}")
            clock[0] += 61
            gone = await c.get(f"/api/factcheck/{job}")  # sweeper 전에도 조회는 막힌다
            await store.sweep()
        return alive, gone, len(store._jobs)

    alive, gone, left = run(fc_pg, go, jobs=store)
    assert fj.TTL_S == 15 * 60
    assert alive.status_code == 200 and gone.status_code == 404 and left == 0


def test_background_sweeper_removes_expired_jobs_and_cancels_their_work(fc_pg):
    clock = [1000.0]
    store = fj.JobStore(now=lambda: clock[0])
    block = asyncio.Event()

    async def go(env):
        store.start_sweeper(interval_s=0.02)
        async with env.client() as c:
            job = (await c.post("/api/factcheck", json=body())).json()["job_id"]
            await asyncio.sleep(0.05)
            task = next(iter(store._jobs[job].tasks))
            clock[0] += fj.TTL_S + 1
            for _ in range(100):
                if not store._jobs:
                    break
                await asyncio.sleep(0.02)
            await store.drain()
        return len(store._jobs), task.cancelled() or task.done(), await quota_rows(env), await reservations(env)

    left, stopped, rows, res = run(fc_pg, go, jobs=store, block=block)
    assert left == 0 and stopped
    assert rows["global"][2] == 0 and res[0][3] is True  # 취소돼도 예약은 정산됐다


# ── job 저장소 상한 ─────────────────────────────────────────────────────────

def test_store_limits_refuse_with_busy_before_reserving(fc_pg):
    block = asyncio.Event()
    store = fj.JobStore(max_running=1)

    async def go(env):
        async with env.client() as c:
            first = await c.post("/api/factcheck", json=body())
            second = await c.post("/api/factcheck", json=body())  # 동시 실행 1개 상한
            rows = await quota_rows(env)
            block.set()
            await poll_done(c, first.json()["job_id"])
        return first, second, rows

    first, second, rows = run(fc_pg, go, jobs=store, block=block)
    assert first.status_code == 202 and second.status_code == 503
    assert second.json()["detail"]["code"] == "busy" and "잠시 뒤" in second.json()["detail"]["message"]
    assert next(v for k, v in rows.items() if k != "global")[0] == 1  # 거부된 요청은 한도를 쓰지 않았다


def test_store_count_and_memory_limits(fc_pg):
    async def go(env):
        async with env.client() as c:
            env.jobs.max_jobs = 1
            a = await c.post("/api/factcheck", json=body())
            await poll_done(c, a.json()["job_id"])
            b = await c.post("/api/factcheck", json=body())  # 끝났어도 TTL 동안은 개수에 든다
            env.jobs.max_jobs, env.jobs.max_chars = 100, 10
            m = await c.post("/api/factcheck", json=body())
        return b, m

    b, m = run(fc_pg, go)
    assert b.status_code == m.status_code == 503


# ── 취소·정산 견고성 ────────────────────────────────────────────────────────

def test_cancel_before_run_starts_still_settles_reservation(fc_pg):
    async def go(env):
        orig = env.jobs.spawn

        def spawn_then_cancel(job, coro):
            task = orig(job, coro)
            task.cancel()  # 작업이 첫 단계를 돌기 전에 취소(코루틴의 try/finally에 들어가지 못한다)
            return task

        env.jobs.spawn = spawn_then_cancel
        async with env.client() as c:
            r = await c.post("/api/factcheck", json=body())
            assert r.status_code == 202
            await env.jobs.drain()
        return await quota_rows(env), await reservations(env)

    rows, res = run(fc_pg, go)
    assert rows["global"][2] == 0 and res[0][3] is True
    assert res[0][1] == 0  # 호출이 하나도 안 나갔다


def test_settlement_survives_second_cancel(fc_pg):
    block = asyncio.Event()

    async def go(env):
        orig = env.quota.settle

        async def slow_settle(res, actual):
            await asyncio.sleep(0.1)
            return await orig(res, actual)

        env.quota.settle = slow_settle
        async with env.client() as c:
            r = await c.post("/api/factcheck", json=body())
            await asyncio.sleep(0.02)
            job = env.jobs._jobs[r.json()["job_id"]]
            task = next(iter(job.tasks))
            task.cancel()
            await asyncio.sleep(0.02)
            task.cancel()  # 정산 중 다시 취소
            await env.jobs.drain()
        return await quota_rows(env), await reservations(env)

    rows, res = run(fc_pg, go, block=block)
    assert rows["global"][2] == 0 and res[0][3] is True


def test_settle_db_error_leaves_reservation_for_stale_settlement(fc_pg):
    async def go(env):
        async def broken(res, actual):
            raise ConnectionError("db down")

        real = env.quota.settle
        env.quota.settle = broken
        async with env.client() as c:
            job = (await c.post("/api/factcheck", json=body())).json()["job_id"]
            await poll_done(c, job)
        await env.jobs.drain()
        env.quota.settle = real
        mid = await reservations(env)
        async with env.engine.begin() as conn:
            await conn.execute(text("UPDATE factcheck_reservations SET created_at = now() - interval '31 minutes'"))
        await env.quota.settle_stale(older_than_s=1800)
        return mid, await quota_rows(env)

    mid, rows = run(fc_pg, go)
    assert mid[0][3] is False  # 정산 실패 → 미정산으로 남는다
    assert rows["global"][1:] == (fm.reservation_for(5), 0)  # 오래된 예약 정산이 예약량 그대로 차감


# ── 한도 ─────────────────────────────────────────────────────────────────────

def test_fourth_anonymous_run_is_429_and_pipeline_not_called(fc_pg):
    async def go(env):
        out = []
        async with env.client() as c:
            for _ in range(4):
                r = await c.post("/api/factcheck", json=body())
                out.append(r)
                if r.status_code == 202:
                    await poll_done(c, r.json()["job_id"])
        return out, len(env.pipeline.calls)

    out, calls = run(fc_pg, go)
    assert [r.status_code for r in out] == [202, 202, 202, 429]
    assert out[-1].json()["detail"]["code"] == "cap_runs" and "3회" in out[-1].json()["detail"]["message"]
    assert calls == 3


def test_global_cap_refuses_new_run_without_calling_pipeline(fc_pg):
    async def go(env):
        async with env.client() as c:
            r = await c.post("/api/factcheck", json=body())
        return r, env.pipeline.calls, env.jev.calls, await quota_rows(env)

    r, calls, jev_calls, rows = run(fc_pg, go, limits=fq.QuotaLimits(runs=3, key_tokens=BIG,
                                                                     global_tokens=fm.reservation_for(5) - 1))
    assert r.status_code == 429 and r.json()["detail"]["code"] == "cap_global"
    assert "오늘 검수 한도에 도달했습니다" in r.json()["detail"]["message"]
    assert calls == [] and jev_calls == 0
    assert all(v == (0, 0, 0) for v in rows.values())


def test_forwarded_for_only_from_listed_trusted_proxy(fc_pg, monkeypatch):
    from app.config import settings
    from app.routes import factcheck

    monkeypatch.setattr(settings, "TRUST_PROXY", True)
    monkeypatch.setattr(factcheck.FC, "FACTCHECK_TRUSTED_PROXIES", "10.0.0.0/8")

    async def go(env):
        async with env.client(client_ip="10.0.0.5") as via_proxy, env.client(client_ip="203.0.113.9") as direct:
            for i in range(3):  # 프록시 뒤 같은 사용자(198.51.100.7), 앞쪽 값은 꾸밈
                r = await via_proxy.post("/api/factcheck", json=body(),
                                         headers={"x-forwarded-for": f"1.2.3.{i}, 198.51.100.7, 10.0.0.9"})
                await poll_done(via_proxy, r.json()["job_id"])
            spoof_via_proxy = await via_proxy.post("/api/factcheck", json=body(),
                                                   headers={"x-forwarded-for": "9.9.9.9, 198.51.100.7"})
            for i in range(3):  # 프록시가 아닌 상대의 XFF는 무시: 모두 203.0.113.9
                r = await direct.post("/api/factcheck", json=body(), headers={"x-forwarded-for": f"5.5.5.{i}"})
                await poll_done(direct, r.json()["job_id"])
            spoof_direct = await direct.post("/api/factcheck", json=body(), headers={"x-forwarded-for": "6.6.6.6"})
        return spoof_via_proxy, spoof_direct

    spoof_via_proxy, spoof_direct = run(fc_pg, go)
    assert spoof_via_proxy.status_code == 429  # 오른쪽부터 첫 비신뢰 주소 = 198.51.100.7
    assert spoof_direct.status_code == 429


def test_trust_proxy_without_list_ignores_forwarded_for(fc_pg, monkeypatch):
    from app.config import settings
    from app.routes import factcheck

    monkeypatch.setattr(settings, "TRUST_PROXY", True)
    monkeypatch.setattr(factcheck.FC, "FACTCHECK_TRUSTED_PROXIES", "")

    async def go(env):
        async with env.client() as c:
            for i in range(3):
                r = await c.post("/api/factcheck", json=body(), headers={"x-forwarded-for": f"198.51.100.{i}"})
                await poll_done(c, r.json()["job_id"])
            return await c.post("/api/factcheck", json=body(), headers={"x-forwarded-for": "198.51.100.99"})

    assert run(fc_pg, go).status_code == 429


# ── 익명 원문 미저장 ─────────────────────────────────────────────────────────

def test_anonymous_text_is_not_stored_in_db_or_logs(fc_pg, caplog):
    caplog.set_level(logging.DEBUG)

    async def go(env):
        async with env.client() as c:
            job = (await c.post("/api/factcheck", json=body())).json()["job_id"]
            await poll_done(c, job)
            await c.post("/api/factcheck", json=body(text_="가" * 2001 + MARKER))
        found = []
        async with env.engine.connect() as conn:
            tables = [r[0] for r in (await conn.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))).all()]
            for t in tables:
                hit = (await conn.execute(text(f'SELECT count(*) FROM "{t}" x WHERE x::text LIKE :m'),
                                          {"m": f"%{MARKER}%"})).scalar()
                if hit:
                    found.append(t)
        return tables, found

    tables, found = run(fc_pg, go)
    assert {"factcheck_quota", "factcheck_reservations", "factcheck_salt"} <= set(tables)
    assert found == []
    assert MARKER not in caplog.text
    assert all(MARKER not in str(r.args) for r in caplog.records)


# ── 건너뛴 문장 수동 검수 ────────────────────────────────────────────────────

def test_recheck_skipped_sentence_forces_check_and_replaces_result(fc_pg):
    async def go(env):
        async with env.client() as c:
            job = (await c.post("/api/factcheck", json=body())).json()["job_id"]
            done = await poll_done(c, job)
            skipped = next(x for x in done["results"] if x["status"] == "skipped")
            checked = next(x for x in done["results"] if x["status"] != "skipped")
            r = await c.post(f"/api/factcheck/{job}/recheck/{skipped['idx']}", json={})
            after = await poll_done(c, job)
            again = await c.post(f"/api/factcheck/{job}/recheck/{skipped['idx']}", json={})
            not_skipped = await c.post(f"/api/factcheck/{job}/recheck/{checked['idx']}", json={})
            async with env.client() as stranger:
                foreign = await stranger.post(f"/api/factcheck/{job}/recheck/{skipped['idx']}", json={})
            no_json = await c.post(f"/api/factcheck/{job}/recheck/{skipped['idx']}")
        key = await env.keyer.key("127.0.0.1")
        return (skipped, r, after, again, not_skipped, foreign, no_json, env.pipeline.calls, await quota_rows(env),
                await reservations(env), key)

    skipped, r, after, again, not_skipped, foreign, no_json, calls, rows, res, key = run(fc_pg, go)
    assert r.status_code == 202
    assert calls[-1] == (SAMSUNG, skipped["text"], None, True, key)  # force_check=True, 익명 키 사본
    redone = next(x for x in after["results"] if x["idx"] == skipped["idx"])
    assert redone["status"] == "supported" and redone["text"] == skipped["text"]
    assert after["counts"]["skipped"] == 0
    assert again.status_code == 409 and not_skipped.status_code == 409 and foreign.status_code == 404
    assert no_json.status_code == 415
    assert rows[key][0] == 1 and rows[key][1] == 5_000  # 실행 횟수는 그대로, 토큰은 센다
    assert res[-1][:3] == (fm.reservation_for(1, triage=False), 1_000, False)


def test_concurrent_recheck_of_same_sentence_admits_one(fc_pg):
    async def go(env):
        async with env.client() as c:
            job = (await c.post("/api/factcheck", json=body())).json()["job_id"]
            done = await poll_done(c, job)
            idx = next(x for x in done["results"] if x["status"] == "skipped")["idx"]
            rs = await asyncio.gather(*(c.post(f"/api/factcheck/{job}/recheck/{idx}", json={}) for _ in range(3)))
            await poll_done(c, job)
        return sorted(r.status_code for r in rs), await reservations(env)

    codes, res = run(fc_pg, go)
    assert codes == [202, 409, 409]
    assert sum(1 for r in res if r[2] is False) == 1  # 수동 검수 예약은 하나뿐


def test_recheck_reservation_failure_restores_ownership(fc_pg):
    async def go(env):
        async with env.client() as c:
            job = (await c.post("/api/factcheck", json=body())).json()["job_id"]
            done = await poll_done(c, job)
            idx = next(x for x in done["results"] if x["status"] == "skipped")["idx"]
            env.quota.limits = fq.QuotaLimits(runs=3, key_tokens=BIG, global_tokens=0)
            refused = await c.post(f"/api/factcheck/{job}/recheck/{idx}", json={})
            state = (await c.get(f"/api/factcheck/{job}")).json()
            env.quota.limits = fq.QuotaLimits(runs=3, key_tokens=BIG, global_tokens=BIG)
            retry = await c.post(f"/api/factcheck/{job}/recheck/{idx}", json={})
            await poll_done(c, job)
        return refused, state, retry

    refused, state, retry = run(fc_pg, go)
    assert refused.status_code == 429 and state["status"] == "done"
    assert retry.status_code == 202  # 실패하면 소유권을 되돌려 다시 요청할 수 있다
