# tests/factcheck/test_fc_integration.py
"""T2 실제 파이프라인(FactcheckPipeline) ↔ T3 익명 API 통합: 가짜 JEV(ServiceJevClient 모양)·가짜 저장소·고정 XBRL.

- 요청마다 pipeline.for_user(익명 키) 사본을 쓰고, 유료 JEV는 계량 래퍼(MeteredJev)를 거친다.
- 정산 = 호출별 원장(나간 JEV 호출의 보고 토큰), 예약 = 문장 전부 × 상한. XBRL 불일치·건너뜀은 JEV를 부르지 않는다.
- 건너뛴 문장 수동 검수는 실제 check(..., force_check=True)로 그 문장을 대조한다.
- JEV 시간 초과(파이프라인의 wait_for 취소)는 모르는 사용량이라 그 호출의 상한으로 정산한다.
- T2 공개 시그니처가 라우트가 부르는 모양과 같은지(가짜 대신 실제 함수로) 확인한다.
DB가 필요하다(fc_pg).
"""
import asyncio
import inspect

from app.services.factcheck import metering as fm
from app.services.factcheck.pipeline import FactcheckPipeline
from tests.factcheck.conftest import load_facts
from tests.factcheck.fc_support import FakeServiceJev
from tests.factcheck.test_fc_api import SAMSUNG, poll_done, quota_rows, reservations, run

NAMES = {SAMSUNG: ["삼성전자"], "00164779": ["SK하이닉스"]}
TEXT = ("삼성전자는 2026년 상반기에 메모리 반도체를 생산했다. 앞으로도 좋을까? 2025년 연결 매출액은 300조원이다. "
        "SK하이닉스의 2025년 영업이익은 40조원이다.")
PASSAGE = {"rcept_no": "20260814003699", "report_nm": "반기보고서 (2026.06)", "period": "2026H1", "report_type": "half",
           "section": "II. 사업의 내용", "corp_code": SAMSUNG, "text": "회사는 메모리 반도체와 스마트폰을 생산한다."}


class Store:
    """T1 FactcheckStore.search·latest_period 모양의 가짜(문단 하나)."""

    def __init__(self):
        self.calls = 0

    async def search(self, corp_code, query, k=8, *, periods=None, report_types=None):
        self.calls += 1
        return [dict(PASSAGE)]

    async def latest_period(self, corp_code):
        return "2026H1"


def real_pipeline(jev, **kw):
    return FactcheckPipeline(store=Store(), jev=fm.MeteredJev(jev), names=NAMES, facts=load_facts(), **kw)


def test_t2_public_signature_matches_what_routes_call():
    sig = inspect.signature(FactcheckPipeline.check)
    assert list(sig.parameters) == ["self", "corp_code", "text", "as_of", "force_check"]
    assert sig.parameters["as_of"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["force_check"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["force_check"].default is False
    assert callable(FactcheckPipeline.for_user)


def test_real_pipeline_through_api_settles_from_ledger_and_uses_anon_key(fc_pg):
    jev = FakeServiceJev(tokens=1000)

    async def go(env):
        async with env.client() as c:
            r = await c.post("/api/factcheck", json={"corp_code": SAMSUNG, "source": "ai_answer", "text": TEXT})
            assert r.status_code == 202, r.text
            job = r.json()["job_id"]
            done = await poll_done(c, job)
            skipped = next(x for x in done["results"] if x["category"] == "opinion")
            rc = await c.post(f"/api/factcheck/{job}/recheck/{skipped['idx']}", json={})
            after = await poll_done(c, job)
        key = await env.keyer.key("127.0.0.1")
        return done, rc, after, await quota_rows(env), await reservations(env), key

    done, rc, after, rows, res, key = run(fc_pg, go, pipeline=real_pipeline(jev), jev=jev)
    by = {x["idx"]: x for x in done["results"]}
    assert done["status"] == "done" and done["total"] == 4
    assert by[0]["status"] == "supported" and by[0]["evidence"][0]["report_nm"] == "반기보고서 (2026.06)"
    assert by[1]["status"] == "skipped" and by[1]["category"] == "opinion"
    assert by[2]["status"] == "contradicted" and by[2]["xbrl"]["account_nm"] == "매출액"  # XBRL 불일치: JEV 안 부름
    assert set(by[2]["xbrl"]) == {"account_nm", "period", "fs_div", "amount"}  # 계약 필드만 낸다
    assert by[3]["status"] == "skipped" and by[3]["category"] == "other_company"
    # 수동 검수: 실제 force_check로 그 문장을 대조했다
    assert rc.status_code == 202 and after["status"] == "done"
    assert next(x for x in after["results"] if x["idx"] == 1)["category"] == "checked"
    # 유료 호출 2번(첫 검수 1 + 수동 검수 1), 모두 익명 키 사본으로
    assert jev.calls == 2 and set(jev.users) == {key}
    assert res == [(fm.reservation_for(4), 1_000, True, True), (fm.reservation_for(1, triage=False), 1_000, False, True)]
    assert rows[key] == (1, 2_000, 0) and rows["global"] == (1, 2_000, 0)


def test_real_pipeline_jev_timeout_is_charged_at_bound(fc_pg):
    jev = FakeServiceJev(tokens=1000, delay=5)

    async def go(env):
        async with env.client() as c:
            r = await c.post("/api/factcheck", json={"corp_code": SAMSUNG, "source": "my_draft",
                                                     "text": "삼성전자는 2026년 상반기에 메모리 반도체를 생산했다."})
            done = await poll_done(c, r.json()["job_id"])
        return done, await quota_rows(env)

    done, rows = run(fc_pg, go, pipeline=real_pipeline(jev, jev_timeout_s=0.05), jev=jev)
    assert done["results"][0]["status"] == "unjudged" and done["results"][0]["reason"] == "timeout"
    assert jev.calls == 1
    used = rows["global"][1]
    assert used >= 1_000 and used <= fm.SLOT_TOKENS and rows["global"][2] == 0  # 모르는 사용량: 그 요청의 상한 × 시도


def test_real_pipeline_refuses_calls_beyond_reservation(fc_pg):
    """원장 예산(예약량)을 넘는 유료 호출은 나가지 않는다 — 파이프라인 안에서 호출이 늘어도 상한이 지켜진다."""
    jev = FakeServiceJev(tokens=1000)

    async def go(env):
        import app.routes.factcheck as fr

        orig = fm.reservation_for
        fr.metering.reservation_for = lambda n, triage=True: 0  # 예약을 0으로: 어떤 호출도 원장 예산을 넘는다
        try:
            async with env.client() as c:
                r = await c.post("/api/factcheck", json={"corp_code": SAMSUNG, "source": "my_draft",
                                                         "text": "삼성전자는 2026년 상반기에 메모리 반도체를 생산했다."})
                done = await poll_done(c, r.json()["job_id"])
        finally:
            fr.metering.reservation_for = orig
        return done, await quota_rows(env)

    done, rows = run(fc_pg, go, pipeline=real_pipeline(jev), jev=jev)
    assert jev.calls == 0
    assert done["results"][0]["status"] == "unjudged" and done["results"][0]["reason"] == "budget"
    assert rows["global"][1:] == (0, 0)


def test_entrypoint_builds_metered_real_pipeline(tmp_path):
    """진입점 조립: T1 저장소·XBRL 행·상장사명 사전 + MeteredJev(ServiceJevClient). 데이터 파일이 없어도 만든다."""
    import json

    from app import factcheck_main
    from app.lib.jev_service import ServiceJevClient

    (tmp_path / "xbrl_facts.json").write_text(json.dumps(load_facts()[:3], ensure_ascii=False))
    (tmp_path / "corp_names.json").write_text(json.dumps([{"corp_code": SAMSUNG, "corp_name": "삼성전자"}],
                                                         ensure_ascii=False))

    async def go(path):
        p = factcheck_main.build_pipeline(path)
        try:
            return p, isinstance(p.jev, fm.MeteredJev), isinstance(p.jev.inner, ServiceJevClient), len(p.facts)
        finally:
            await factcheck_main.close_pipeline(p)

    p, metered, service, n = asyncio.run(go(tmp_path))
    assert isinstance(p, FactcheckPipeline) and metered and service and n == 3
    p2, metered2, _, n2 = asyncio.run(go(tmp_path / "missing"))
    assert metered2 and n2 == 0  # 파일이 없으면 XBRL 대조 없이(문단 판정만) 뜬다
