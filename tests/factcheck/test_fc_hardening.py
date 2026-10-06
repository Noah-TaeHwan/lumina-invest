# tests/factcheck/test_fc_hardening.py
"""배포 전 검수(B·C) 회귀: job 저장소·라우트의 경계 상황.

B1 drain 무한 루프, B2 재검수 예약 대기 중 만료, B3 비문자열 입력, B7 입력 문장 수, B8 조회 시 만료 삭제, B9 Origin 스킴,
C2 IPv6 /64 익명 키, C3 재검수가 실패한 job의 오류를 덮지 않음·sweeper 예외 로그.
DB가 필요한 테스트는 fc_pg 픽스처를 쓴다.
"""
import asyncio
import logging

import pytest

from app.services.factcheck import jobs as fj
from tests.factcheck.fc_support import FakePipeline, FakeServiceJev
from tests.factcheck.test_fc_api import SAMSUNG, body, poll_done, quota_rows, reservations, run


# ── B1·B8: job 저장소 ───────────────────────────────────────────────────────

def test_drain_terminates_with_finished_future_still_tracked():
    """완료된 정산 Future가 제거 콜백 전에 남아 있어도 drain이 돌기만 하지 않는다(B1)."""
    async def go():
        store = fj.JobStore()
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(None)
        store._settling.add(fut)  # 제거 콜백 없이 남은 완료 항목
        await asyncio.wait_for(store.drain(), 1)
        return len(store._settling)

    assert asyncio.run(go()) == 0


def test_get_removes_expired_job_immediately_and_cancels_its_work():
    """30초 주기 sweeper를 기다리지 않고 조회 시점에 15분 넘은 job을 지우고 작업을 취소한다(B8)."""
    async def go():
        clock = [100.0]
        store = fj.JobStore(now=lambda: clock[0])
        store.admit(10)
        job = store.create("owner", SAMSUNG, "my_draft", total=1, size=10)
        task = store.spawn(job, asyncio.sleep(60))
        clock[0] += fj.TTL_S + 1
        got = store.get(job.id, "owner")
        gone = job.id not in store._jobs
        await store.drain()
        return got, gone, task.cancelled(), store.alive(job)

    got, gone, cancelled, alive = asyncio.run(go())
    assert got is None and gone and cancelled and alive is False


def test_sweeper_logs_exceptions(caplog):
    caplog.set_level(logging.ERROR)

    async def go():
        store = fj.JobStore()

        async def boom():
            raise RuntimeError("x")

        store.start_sweeper(interval_s=0.01, also=boom)
        await asyncio.sleep(0.05)
        await store.stop_sweeper()

    asyncio.run(go())
    assert "factcheck_sweep_failed" in caplog.text


# ── B2: 재검수 예약 대기 중 만료 ────────────────────────────────────────────

def test_recheck_expired_while_reserving_settles_and_starts_nothing(fc_pg):
    clock = [1000.0]
    store = fj.JobStore(now=lambda: clock[0])

    async def go(env):
        async with env.client() as c:
            job = (await c.post("/api/factcheck", json=body())).json()["job_id"]
            done = await poll_done(c, job)
            idx = next(x for x in done["results"] if x["status"] == "skipped")["idx"]
            entered, release = asyncio.Event(), asyncio.Event()
            orig = env.quota.reserve

            async def slow_reserve(*a, **kw):
                entered.set()
                await release.wait()
                return await orig(*a, **kw)

            env.quota.reserve = slow_reserve
            t = asyncio.ensure_future(c.post(f"/api/factcheck/{job}/recheck/{idx}", json={}))
            await entered.wait()
            clock[0] += fj.TTL_S + 1  # 예약을 기다리는 동안 만료
            release.set()
            r = await t
            await env.jobs.drain()
        return r, env.pipeline.calls, await reservations(env), await quota_rows(env)

    r, calls, res, rows = run(fc_pg, go, jobs=store)
    assert r.status_code == 404
    assert len(calls) == 1  # 처음 검수만, 고아 유료 작업은 시작하지 않았다
    assert res[-1][1:] == (0, False, True)  # 수동 검수 예약은 바로 0으로 정산
    assert rows["global"][2] == 0


# ── B3·B7·B9: 입력 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("payload,code", [
    ({"corp_code": ["00126380"], "source": "my_draft", "text": "2025년 매출은 300조원이다."}, "bad_company"),
    ({"corp_code": {"x": 1}, "source": "my_draft", "text": "2025년 매출은 300조원이다."}, "bad_company"),
    ({"corp_code": SAMSUNG, "source": ["my_draft"], "text": "2025년 매출은 300조원이다."}, "bad_source"),
    ({"corp_code": SAMSUNG, "source": 1, "text": "2025년 매출은 300조원이다."}, "bad_source"),
])
def test_non_string_fields_are_422_not_500(fc_pg, payload, code):
    async def go(env):
        async with env.client() as c:
            return await c.post("/api/factcheck", json=payload)

    r = run(fc_pg, go)
    assert r.status_code == 422 and r.json()["detail"]["code"] == code


def test_sentence_limit_counts_short_sentences(fc_pg):
    """'네.' 31개는 claim_spans로는 0문장(4자 미만 버림)이지만 입력 제한은 31문장으로 센다(B7)."""
    async def go(env):
        async with env.client() as c:
            short = await c.post("/api/factcheck", json=body(text_="네. " * 31))
            decimals = await c.post("/api/factcheck", json=body(text_="매출은 3.5조원이다. 이익은 1.2조원이다."))
        return short, decimals

    short, decimals = run(fc_pg, go)
    assert short.status_code == 422 and short.json()["detail"]["sentences"] == 31
    assert decimals.status_code == 202 and decimals.json()["total"] == 2  # 소수점은 문장 경계가 아니다


def test_origin_scheme_must_match(fc_pg):
    async def go(env):
        async with env.client(headers={"origin": "https://t"}) as c:  # 요청은 http://t
            return await c.post("/api/factcheck", json=body())

    assert run(fc_pg, go).status_code == 403


# ── C2: IPv6 /64 ────────────────────────────────────────────────────────────

def test_ipv6_clients_in_same_64_share_anon_key(fc_pg):
    async def go(env):
        async with env.client(client_ip="2001:db8:1:2::1") as a, env.client(client_ip="2001:db8:1:2:ffff::9") as b:
            for _ in range(3):
                r = await a.post("/api/factcheck", json=body())
                await poll_done(a, r.json()["job_id"])
            same64 = await b.post("/api/factcheck", json=body())
        async with env.client(client_ip="2001:db8:1:3::1") as other:
            other64 = await other.post("/api/factcheck", json=body())
        return same64, other64, await env.keyer.key("2001:db8:1:2::/64"), await quota_rows(env)

    same64, other64, k64, rows = run(fc_pg, go)
    assert same64.status_code == 429 and other64.status_code == 202
    assert rows[k64][0] == 3


def test_ipv4_mapped_ipv6_is_its_ipv4(fc_pg):
    async def go(env):
        async with env.client(client_ip="::ffff:203.0.113.5") as c:
            for _ in range(3):
                r = await c.post("/api/factcheck", json=body())
                await poll_done(c, r.json()["job_id"])
        async with env.client(client_ip="203.0.113.5") as c:
            return await c.post("/api/factcheck", json=body())

    assert run(fc_pg, go).status_code == 429


# ── C3: 재검수가 실패한 job의 오류를 덮지 않는다 ───────────────────────────

def test_recheck_on_failed_job_keeps_original_error(fc_pg):
    text = "앞으로도 좋을까? 2025년 매출은 300조원이다. 2024년 매출은 250조원이다."

    async def go(env):
        async with env.client() as c:
            job = (await c.post("/api/factcheck", json=body(text_=text))).json()["job_id"]
            failed = await poll_done(c, job)
            r = await c.post(f"/api/factcheck/{job}/recheck/0", json={})
            after = await poll_done(c, job)
        return failed, r, after

    failed, r, after = run(fc_pg, go, exc=RuntimeError("boom"))
    assert failed["status"] == "failed" and failed["error"]["code"] == "pipeline_error"
    assert r.status_code == 202
    assert after["results"][0]["status"] == "supported"  # 그 문장은 검수됐다
    assert after["status"] == "failed" and after["error"]["code"] == "pipeline_error"  # 원래 실패는 그대로 보인다


def test_no_api_key_calls_are_settled_at_zero(fc_pg):
    """키가 없어 요청이 나가지 않은 호출은 0으로 정산한다(A4: 한도만 소진되지 않게)."""
    class NoKey(FakeServiceJev):
        async def ask(self, state, questions, *, user_id, log_ctx=None, usage=None):
            from app.lib import jev
            from app.lib.jev_service import ServiceJevResult
            return ServiceJevResult(jev.request_key(state, questions), False, None, 0.0, 0, 0, "FileNotFoundError",
                                    "no_api_key", None)

    async def go(env):
        async with env.client() as c:
            job = (await c.post("/api/factcheck", json=body())).json()["job_id"]
            done = await poll_done(c, job)
        return done, await quota_rows(env)

    done, rows = run(fc_pg, go, jev=NoKey())
    assert all(x["status"] in ("unjudged", "skipped") for x in done["results"])
    assert rows["global"][1:] == (0, 0)
