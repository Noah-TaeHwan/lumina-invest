# tests/factcheck/test_fc_runs_message.py
"""익명 하루 실행 횟수 안내가 실제 한도 값을 따른다(FACTCHECK_DAILY_ANON_RUNS / QuotaLimits.runs).

- 한도 초과(cap_runs) 응답 문구의 숫자 = 그 한도 객체의 runs. 기본 3이면 지금처럼 '3회'.
- 회사 목록 응답(GET /api/factcheck/companies)이 anon_runs를 준다: 연결된 한도 객체가 있으면 그 값, 없으면 설정 값.
  화면은 이 값으로 '로그인 없이 하루 N회까지' 문장을 채운다(e2e).
DB가 필요한 테스트는 fc_pg 픽스처를 쓴다.
"""
from app.services.factcheck import quota as fq
from tests.factcheck.test_fc_api import BIG, body, poll_done, run


def _exhaust(runs):
    async def go(env):
        out = []
        async with env.client() as c:
            for _ in range(runs + 1):
                r = await c.post("/api/factcheck", json=body())
                out.append(r)
                if r.status_code == 202:
                    await poll_done(c, r.json()["job_id"])
        return out
    return go


def test_cap_runs_message_uses_configured_limit(fc_pg, monkeypatch):
    monkeypatch.setenv("FACTCHECK_DAILY_ANON_RUNS", "5")  # 진입점과 같은 경로(limits_from_env)로 한도를 읽는다
    monkeypatch.setenv("FACTCHECK_DAILY_KEY_TOKENS", str(BIG))
    monkeypatch.setenv("FACTCHECK_DAILY_GLOBAL_TOKENS", str(BIG))
    out = run(fc_pg, _exhaust(5), limits=fq.limits_from_env())
    assert [r.status_code for r in out] == [202] * 5 + [429]
    d = out[-1].json()["detail"]
    assert d["code"] == "cap_runs" and "5회" in d["message"] and "3회" not in d["message"]


def test_cap_runs_message_default_is_three(fc_pg):
    out = run(fc_pg, _exhaust(3), limits=fq.QuotaLimits(runs=3, key_tokens=BIG, global_tokens=BIG))
    d = out[-1].json()["detail"]
    assert d["code"] == "cap_runs" and "하루 3회까지" in d["message"] and "오늘 3회를 모두" in d["message"]


def test_companies_reports_anon_runs_from_settings_or_configured_quota(fc_pg, monkeypatch):
    from app.routes import factcheck

    async def companies(env):
        async with env.client() as c:
            return (await c.get("/api/factcheck/companies")).json()

    monkeypatch.setenv("FACTCHECK_DAILY_ANON_RUNS", "5")
    factcheck.configure(quota=None)
    assert run(fc_pg, companies)["anon_runs"] == 5  # 한도 객체가 아직 없으면 설정 값

    class Q:
        limits = fq.QuotaLimits(runs=7)

    factcheck.configure(quota=Q())
    try:
        assert run(fc_pg, companies)["anon_runs"] == 7  # 실제로 쓰는 한도 객체의 값이 우선
    finally:
        factcheck.configure(quota=None)
