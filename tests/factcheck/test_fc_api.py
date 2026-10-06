# tests/factcheck/test_fc_api.py
"""팩트체커 익명 API(T3): 회사 목록·검수 실행·결과 폴링·건너뛴 문장 수동 검수.

- 입력 2,000자·검수 대상 30문장 초과는 422 안내(입력 원문을 응답에 되돌려 주지 않는다).
- job_id는 추측 불가 토큰 + 익명 쿠키 결속, 쿠키가 다르거나 없으면 모르는 job과 같은 404.
- 한도는 예약이 먼저다: 예약에 실패하면 파이프라인을 부르지 않는다(429).
- 익명 원문은 DB·로그에 남기지 않는다(고유 표식으로 모든 표·로그를 훑는다).
파이프라인은 같은 시그니처의 가짜(FakeChecker)다. DB가 필요한 테스트는 fc_pg 픽스처를 쓴다.
"""
import asyncio
import logging
import sys

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services.factcheck import jobs as fj
from app.services.factcheck import quota as fq
from tests.factcheck.conftest import FakeChecker

SAMSUNG, HYNIX = "00126380", "00164779"
MARKER = "표식ZQX9-원문"
DRAFT = (f"삼성전자의 2026년 상반기 매출은 153조원이다. {MARKER} 문장은 표식이다. 메모리 사업은 성장했다. "
         "앞으로도 좋을까? 2025년 영업이익은 32조 7,260억원이다.")


class Env:
    """테스트용 앱과 그 부품(가짜 파이프라인·한도·job 저장소)."""

    def __init__(self, url, checker=None, limits=None, jobs=None):
        self.engine = create_async_engine(url)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        self.checker = checker or FakeChecker()
        self.quota = fq.FactcheckQuota(self.factory, limits or fq.QuotaLimits(runs=3, key_tokens=10_000_000,
                                                                               global_tokens=10_000_000))
        self.jobs = jobs or fj.JobStore()
        self.keyer = fq.AnonKeyer()
        from app.routes import factcheck

        self.app = FastAPI()
        self.app.include_router(factcheck.router)
        self.app.dependency_overrides[factcheck.get_checker] = lambda: self.checker
        self.app.dependency_overrides[factcheck.get_quota] = lambda: self.quota
        self.app.dependency_overrides[factcheck.get_jobs] = lambda: self.jobs
        self.app.dependency_overrides[factcheck.get_keyer] = lambda: self.keyer

    def client(self, cookies=None, client_ip="127.0.0.1"):
        transport = httpx.ASGITransport(app=self.app, client=(client_ip, 1234))
        return httpx.AsyncClient(transport=transport, base_url="http://t", cookies=cookies)

    async def close(self):
        await self.jobs.close()
        await self.engine.dispose()


def body(text_=DRAFT, corp=SAMSUNG, source="my_draft"):
    return {"corp_code": corp, "text": text_, "source": source}


async def quota_rows(env) -> dict:
    async with env.engine.connect() as conn:
        rows = (await conn.execute(text("SELECT key, count, used, reserved FROM factcheck_quota"))).all()
    return {r.key: (r.count, r.used, r.reserved) for r in rows}


async def poll_done(c, job_id, limit=200):
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


# ── 회사 목록 ────────────────────────────────────────────────────────────────

def test_companies_lists_only_the_two_demo_companies(fc_pg):
    async def go(env):
        async with env.client() as c:
            return await c.get("/api/factcheck/companies")

    r = run(fc_pg, go)
    assert r.status_code == 200
    data = r.json()
    assert [(x["corp_code"], x["corp_name"]) for x in data["companies"]] == [(SAMSUNG, "삼성전자"), (HYNIX, "SK하이닉스")]
    assert "삼성전자·SK하이닉스" in data["scope"]


# ── 검수 실행·폴링 ───────────────────────────────────────────────────────────

def test_post_starts_job_with_token_and_cookie_then_poll_accumulates_results(fc_pg):
    async def go(env):
        async with env.client() as c:
            r = await c.post("/api/factcheck", json=body())
            assert r.status_code == 202, r.text
            started = r.json()
            cookie = r.headers.get("set-cookie", "")
            done = await poll_done(c, started["job_id"])
        return started, cookie, done, env.checker.calls, await quota_rows(env)

    started, cookie, done, calls, rows = run(fc_pg, go)
    assert len(started["job_id"]) >= 43  # 32바이트(256비트) urlsafe 토큰
    assert started["total"] == 5 and started["targets"] == 4 and started["poll_interval_ms"] == 1000
    assert "fc_anon=" in cookie and "httponly" in cookie.lower() and "samesite=lax" in cookie.lower()
    assert calls == [(SAMSUNG, DRAFT, {"as_of": None})]
    assert done["status"] == "done" and done["poll_interval_ms"] is None
    assert [x["idx"] for x in done["results"]] == [0, 1, 2, 3, 4]
    assert done["counts"] == {"supported": 4, "contradicted": 0, "no_evidence": 0, "unjudged": 0, "skipped": 1}
    first = done["results"][0]
    assert set(first) == {"idx", "text", "category", "status", "evidence", "xbrl", "reason"}  # input_tokens는 안 낸다
    assert first["evidence"][0]["report_nm"] == "반기보고서 (2026.06)"
    # 정산: 실제 토큰(가짜는 검수 문장마다 1,000)으로 바뀌고 예약은 0
    key = next(k for k in rows if k != "global")
    assert rows[key] == (1, 4_000, 0) and rows["global"] == (1, 4_000, 0)


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
            gate.set()
            gate.set()
            while (await c.get(f"/api/factcheck/{job}")).json()["status"] == "running":
                gate.set()
                await asyncio.sleep(0.01)
        return seen

    assert run(fc_pg, go, checker=FakeChecker(gate=gate)) == [1, 2, 3]


def test_tokens_unknown_settles_full_reservation(fc_pg):
    async def go(env):
        async with env.client() as c:
            job = (await c.post("/api/factcheck", json=body())).json()["job_id"]
            await poll_done(c, job)
        return await quota_rows(env)

    rows = run(fc_pg, go, checker=FakeChecker(tokens=None))
    assert rows["global"] == (1, fq.estimate(4), 0)  # 실제를 모르면 예약 전액을 쓴 것으로 센다


def test_pipeline_error_marks_job_failed_and_settles(fc_pg):
    async def go(env):
        async with env.client() as c:
            job = (await c.post("/api/factcheck", json=body())).json()["job_id"]
            done = await poll_done(c, job)
        return done, await quota_rows(env)

    done, rows = run(fc_pg, go, checker=FakeChecker(exc=RuntimeError(f"boom {MARKER}")))
    assert done["status"] == "failed" and done["error"]["code"] == "pipeline_error"
    assert MARKER not in str(done["error"])
    assert len(done["results"]) == 1  # 실패 전까지 나온 결과는 남는다
    assert rows["global"][2] == 0  # 예약이 풀렸다


def test_pipeline_missing_returns_503_without_quota(fc_pg, monkeypatch):
    from app.routes import factcheck

    monkeypatch.setitem(sys.modules, "app.services.factcheck.pipeline", None)  # import 실패를 흉내 낸다

    async def go(env):
        del env.app.dependency_overrides[factcheck.get_checker]
        async with env.client() as c:
            r = await c.post("/api/factcheck", json=body())
        return r, await quota_rows(env)

    r, rows = run(fc_pg, go)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "pipeline_unavailable"
    assert rows == {}


# ── 입력 검증(422) ───────────────────────────────────────────────────────────

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
        return r, env.checker.calls, await quota_rows(env)

    r, calls, rows = run(fc_pg, go)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == code and r.json()["detail"]["message"]
    assert MARKER not in r.text
    assert calls == [] and rows == {}


def test_too_long_message_names_the_limit_and_length(fc_pg):
    async def go(env):
        async with env.client() as c:
            return await c.post("/api/factcheck", json=body(text_="가" * 2001))

    d = run(fc_pg, go).json()["detail"]
    assert d["limit"] == 2000 and d["length"] == 2001 and "2,000자" in d["message"]


def test_malformed_json_is_422_without_echo(fc_pg):
    async def go(env):
        async with env.client() as c:
            return await c.post("/api/factcheck", content=f'{{"text": "{MARKER}"'.encode(),
                                headers={"content-type": "application/json"})

    r = run(fc_pg, go)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "bad_json" and MARKER not in r.text


def test_more_than_30_check_target_sentences_is_422(fc_pg):
    over = " ".join(f"{2000 + i}년 매출은 {i}조원이다." for i in range(31))
    exact = " ".join(f"{2000 + i}년 매출은 {i}조원이다." for i in range(30)) + " 좋을까? 그럴까?"  # 비주장은 세지 않는다

    async def go(env):
        async with env.client() as c:
            r1 = await c.post("/api/factcheck", json=body(text_=over))
            r2 = await c.post("/api/factcheck", json=body(text_=exact))
        return r1, r2

    r1, r2 = run(fc_pg, go)
    assert r1.status_code == 422
    d = r1.json()["detail"]
    assert d["code"] == "too_many_sentences" and d["limit"] == 30 and d["targets"] == 31 and "30" in d["message"]
    assert r2.status_code == 202 and r2.json()["targets"] == 30


# ── 쿠키 결속·만료 ───────────────────────────────────────────────────────────

def test_other_cookie_or_no_cookie_gets_same_404_as_unknown_job(fc_pg):
    async def go(env):
        async with env.client() as owner:
            job = (await owner.post("/api/factcheck", json=body())).json()["job_id"]
            await poll_done(owner, job)
            async with env.client() as stranger:
                await stranger.post("/api/factcheck", json=body(text_="2025년 매출은 300조원이다."))  # 자기 쿠키를 받는다
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
            gone = await c.get(f"/api/factcheck/{job}")
        return alive, gone, len(store._jobs)

    alive, gone, left = run(fc_pg, go, jobs=store)
    assert fj.TTL_S == 15 * 60
    assert alive.status_code == 200 and gone.status_code == 404 and left == 0


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
        return out, len(env.checker.calls)

    out, calls = run(fc_pg, go)
    assert [r.status_code for r in out] == [202, 202, 202, 429]
    d = out[-1].json()["detail"]
    assert d["code"] == "cap_runs" and "3회" in d["message"]
    assert calls == 3


def test_global_cap_refuses_new_run_without_calling_pipeline(fc_pg):
    async def go(env):
        async with env.client() as c:
            r = await c.post("/api/factcheck", json=body())
        return r, env.checker.calls, await quota_rows(env)

    r, calls, rows = run(fc_pg, go, limits=fq.QuotaLimits(runs=3, key_tokens=10_000_000,
                                                          global_tokens=fq.estimate(4) - 1))
    assert r.status_code == 429
    d = r.json()["detail"]
    assert d["code"] == "cap_global" and "오늘 검수 한도에 도달했습니다" in d["message"]
    assert calls == []
    assert all(v == (0, 0, 0) for v in rows.values())


def test_forwarded_for_is_ignored_unless_proxy_trusted(fc_pg, monkeypatch):
    from app.config import settings

    async def go(env):
        async with env.client() as c:
            for i in range(3):
                r = await c.post("/api/factcheck", json=body(), headers={"x-forwarded-for": f"198.51.100.{i}"})
                await poll_done(c, r.json()["job_id"])
            spoofed = await c.post("/api/factcheck", json=body(), headers={"x-forwarded-for": "198.51.100.99"})
            monkeypatch.setattr(settings, "TRUST_PROXY", True)
            # 프록시가 붙인 마지막 값을 쓴다(앞쪽은 사용자가 꾸밀 수 있다)
            trusted = await c.post("/api/factcheck", json=body(),
                                   headers={"x-forwarded-for": "127.0.0.1, 198.51.100.99"})
        return spoofed, trusted

    monkeypatch.setattr(settings, "TRUST_PROXY", False)
    spoofed, trusted = run(fc_pg, go)
    assert spoofed.status_code == 429
    assert trusted.status_code == 202


# ── 익명 원문 미저장 ─────────────────────────────────────────────────────────

def test_anonymous_text_is_not_stored_in_db_or_logs(fc_pg, caplog):
    caplog.set_level(logging.DEBUG)

    async def go(env):
        async with env.client() as c:
            job = (await c.post("/api/factcheck", json=body())).json()["job_id"]
            await poll_done(c, job)
            await c.post("/api/factcheck", json=body(text_="가" * 2001 + MARKER))  # 거부된 입력도
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
    assert "factcheck_quota" in tables
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
            r = await c.post(f"/api/factcheck/{job}/recheck/{skipped['idx']}")
            after = await poll_done(c, job)
            again = await c.post(f"/api/factcheck/{job}/recheck/{skipped['idx']}")
            not_skipped = await c.post(f"/api/factcheck/{job}/recheck/{checked['idx']}")
            async with env.client() as stranger:
                foreign = await stranger.post(f"/api/factcheck/{job}/recheck/{skipped['idx']}")
        return skipped, r, after, again, not_skipped, foreign, env.checker.calls, await quota_rows(env)

    skipped, r, after, again, not_skipped, foreign, calls, rows = run(fc_pg, go)
    assert r.status_code == 202
    assert calls[-1] == (SAMSUNG, skipped["text"], {"as_of": None, "force": True})
    redone = next(x for x in after["results"] if x["idx"] == skipped["idx"])
    assert redone["status"] == "supported" and redone["text"] == skipped["text"]
    assert after["counts"]["skipped"] == 0
    assert again.status_code == 409 and not_skipped.status_code == 409 and foreign.status_code == 404
    key = next(k for k in rows if k != "global")
    assert rows[key][0] == 1  # 수동 검수는 하루 3회에서 세지 않는다
    assert rows[key][1] == 5_000  # 토큰은 센다
