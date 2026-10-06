# tests/factcheck/test_fc_readiness.py
"""진입점 준비 상태(배포 전 검수 A1·A4·B4·C3): 데이터·API 키·복구가 준비되지 않으면 검수를 받지 않고 /api/health에 드러낸다.

- A1: T2 pipeline.build_pipeline(경로를 주면 파일이 없을 때 예외)을 쓰고, XBRL 행·상장사명·문단이 하나라도 0이면 503
  (FACTCHECK_ALLOW_NO_DATA로만 허용). 시작 로그에 facts=·names=·passages=를 남긴다. 상대 경로는 저장소 루트 기준.
- A4: API 키를 읽을 수 없으면 파이프라인을 켜지 않는다(503 no_api_key).
- B4: 오래된 예약 복구가 실패하면 한도·익명 키를 공개하지 않는다(503 startup_failed). 복구 기준은 실행 마감 + 여유.
- T4-B1: 준비가 안 됐으면 일정 간격으로 다시 확인해 회복하면 연다. health는 'Qdrant 연결 실패'와 '문단 0'을 구분한다.
- T4-B2: 데모 두 회사 각각 XBRL 행·문단이 있어야 준비됨(없는 회사를 health에 표시).
- T4-B4: 401/403이 이어져 판정 키가 막히면 no_api_key로 닫고, 재시작 전까지 다시 열지 않는다.
lifespan 대신 prepare()/shutdown()을 직접 부른다(httpx ASGITransport는 lifespan을 돌리지 않는다). DB가 필요하다.
"""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app import factcheck_main as fm_main
from app.config import settings
from app.routes import factcheck
from app.services.factcheck import jobs as fj
from app.services.factcheck import metering
from app.services.factcheck import quota as fq
from tests.factcheck.conftest import load_facts

SAMSUNG, HYNIX = "00126380", "00164779"
ORIGIN = {"origin": "http://t"}


def _corp_of(count_filter):
    """qdrant Filter(must=[FieldCondition(key='corp_code', match=MatchValue(value=…))])에서 corp_code."""
    for cond in getattr(count_filter, "must", None) or []:
        if getattr(cond, "key", None) == "corp_code":
            return cond.match.value
    return None


class Store:
    """T1 FactcheckStore 모양의 가짜: exists·client.count(corp_code 필터)·search·latest_period·aclose.
    by_corp: 회사별 문단 수. fail_exists: exists()가 앞의 n번 연결 오류를 낸다(일시 장애)."""

    collection = "factcheck_passages"

    def __init__(self, passages=5, exists=True, by_corp=None, fail_exists=0):
        self.by_corp = by_corp if by_corp is not None else {SAMSUNG: passages, HYNIX: passages}
        self._exists, self.closed, self.fail_exists = exists, False, fail_exists

        async def count(collection, exact=True, count_filter=None):
            corp = _corp_of(count_filter)
            return SimpleNamespace(count=self.by_corp.get(corp, 0) if corp else sum(self.by_corp.values()))

        self.client = SimpleNamespace(count=count)

    async def exists(self):
        if self.fail_exists > 0:
            self.fail_exists -= 1
            raise ConnectionError("qdrant down")
        return self._exists

    async def search(self, corp_code, query, k=8, *, periods=None, report_types=None):
        return []

    async def latest_period(self, corp_code):
        return "2026H1"

    async def aclose(self):
        self.closed = True


NAMES = [{"corp_code": SAMSUNG, "corp_name": "삼성전자", "stock_code": "005930", "norm": "삼성전자"},
         {"corp_code": HYNIX, "corp_name": "SK하이닉스", "stock_code": "000660", "norm": "SK하이닉스"}]


def data_dir(tmp_path, *, facts=3, names=2, corps=("samsung", "skhynix")):
    """회사마다 XBRL 행 facts개(T1 고정 자료)와 상장사명 names개."""
    d = tmp_path / "data"
    d.mkdir(parents=True)
    rows = [r for c in corps for r in load_facts(c)[:facts]]
    (d / "xbrl_facts.json").write_text(json.dumps(rows, ensure_ascii=False))
    (d / "corp_names.json").write_text(json.dumps(NAMES[:names], ensure_ascii=False))
    return d


@pytest.fixture
def boot(fc_pg, monkeypatch):
    """prepare()를 테스트 DB로 돌리고 (readiness, health 응답, POST 응답, 출력)을 돌려주는 함수."""
    monkeypatch.setattr(settings, "DATABASE_URL", fc_pg)
    monkeypatch.setattr(settings, "RUN_MIGRATIONS_ON_STARTUP", False)
    monkeypatch.setattr(fm_main, "load_api_key", lambda: "test-key")

    def no_network(request):
        raise AssertionError("테스트에서 외부 JEV 호출이 나갔다")

    monkeypatch.setattr(fm_main, "make_jev", lambda on_auth_block=None: metering.service_client(
        api_key="k", transport=httpx.MockTransport(no_network), on_auth_block=on_auth_block))

    def go(*, store=None, **kw):
        monkeypatch.setattr(fm_main, "make_store", lambda: store or Store())

        async def run():
            ready = await fm_main.prepare(**kw)
            try:
                transport = httpx.ASGITransport(app=fm_main.app)
                async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=ORIGIN) as c:
                    health = await c.get("/api/health")
                    post = await c.post("/api/factcheck", json={"corp_code": SAMSUNG, "source": "my_draft",
                                                                "text": "2025년 매출은 300조원이다."})
                return ready, health, post, factcheck._pipeline
            finally:
                await fm_main.shutdown()

        return asyncio.run(run())

    return go


def test_ready_with_data_key_and_recovery(boot, tmp_path, capsys):
    ready, health, post, pipeline = boot(data_dir=data_dir(tmp_path))
    assert ready.ready and ready.code is None
    assert health.status_code == 200 and health.json()["status"] == "ok"
    data = health.json()["checks"]["data"]
    assert (data["facts"], data["names"], data["passages"], data["qdrant"]) == (6, 2, 10, "ok")
    assert data["by_corp"] == {SAMSUNG: {"facts": 3, "passages": 5}, HYNIX: {"facts": 3, "passages": 5}}
    assert data["missing"] == []
    assert isinstance(pipeline.jev, metering.MeteredJev)  # 실제 조립: MeteredJev(ServiceJevClient)
    assert post.status_code == 202
    assert "facts=6 names=2 passages=10" in capsys.readouterr().out


@pytest.mark.parametrize("case", ["missing_dir", "empty_facts", "no_passages", "no_collection"])
def test_missing_or_empty_data_is_503_and_visible_in_health(boot, tmp_path, case):
    store = Store()
    d = tmp_path / "missing"
    if case == "empty_facts":
        d = data_dir(tmp_path, facts=0)
    elif case in ("no_passages", "no_collection"):
        d = data_dir(tmp_path)
        store = Store(passages=0, exists=case == "no_passages")
    ready, health, post, _ = boot(data_dir=d, store=store)
    assert not ready.ready and ready.code == "data_unavailable"
    assert health.status_code == 503 and health.json()["code"] == "data_unavailable"
    assert post.status_code == 503 and post.json()["detail"]["code"] == "data_unavailable"


def test_allow_no_data_flag_is_the_only_way_to_run_without_data(boot, tmp_path):
    ready, health, post, pipeline = boot(data_dir=tmp_path / "missing", allow_no_data=True)
    assert ready.ready and health.status_code == 200 and post.status_code == 202
    assert pipeline.names.display(SAMSUNG) == "삼성전자"  # 사전이 없으면 데모 두 종목 이름으로
    assert pipeline.names.display(HYNIX) == "SK하이닉스"
    assert health.json()["checks"]["allow_no_data"] is True


def test_missing_api_key_disables_pipeline(boot, tmp_path, monkeypatch):
    def no_key():
        raise FileNotFoundError("~/.config/typesafe/api_key")

    monkeypatch.setattr(fm_main, "load_api_key", no_key)
    ready, health, post, pipeline = boot(data_dir=data_dir(tmp_path))
    assert not ready.ready and ready.code == "no_api_key" and pipeline is None
    assert health.status_code == 503 and health.json()["checks"]["api_key"] is False
    assert post.status_code == 503 and post.json()["detail"]["code"] == "no_api_key"
    assert "api_key" not in post.text.lower().replace("no_api_key", "")  # 경로·키 값은 응답에 없다


def test_recovery_failure_does_not_publish_quota(boot, tmp_path, monkeypatch):
    async def broken(self, older_than_s=0):
        raise ConnectionError("db")

    monkeypatch.setattr(fq.FactcheckQuota, "settle_stale", broken)
    ready, health, post, _ = boot(data_dir=data_dir(tmp_path))
    assert not ready.ready and ready.code == "startup_failed"
    assert factcheck._quota is None and factcheck._keyer is None
    assert health.status_code == 503 and health.json()["checks"]["recovery"] is False
    assert post.status_code == 503


def test_stale_threshold_is_job_deadline_plus_margin():
    assert fj.JOB_DEADLINE_S < fq.STALE_S <= fj.JOB_DEADLINE_S + 300  # 크래시 직후 예약이 30분 묶이지 않게
    assert factcheck.JOB_DEADLINE_S == fj.JOB_DEADLINE_S


def test_relative_data_dir_resolves_against_repo_root(monkeypatch):
    monkeypatch.setenv("FACTCHECK_DATA_DIR", "lab/data/factcheck")
    p = fm_main.data_dir_path()
    assert p.is_absolute() and p.parts[-3:] == ("lab", "data", "factcheck")
    assert p.parent.parent.parent.resolve() == fm_main.ROOT_PATH.resolve()


@pytest.mark.parametrize("empty", ["", "   ", "\n"])
def test_empty_api_key_is_not_ready(boot, tmp_path, monkeypatch, empty):
    """키 파일이 비어 있으면(load_api_key가 예외 없이 빈 문자열) 키 없음과 같다(#61 검수 3)."""
    monkeypatch.setattr(fm_main, "load_api_key", lambda: empty)
    ready, health, post, pipeline = boot(data_dir=data_dir(tmp_path))
    assert not ready.ready and ready.code == "no_api_key" and pipeline is None
    assert health.status_code == 503 and health.json()["checks"]["api_key"] is False
    assert post.status_code == 503 and post.json()["detail"]["code"] == "no_api_key"


def test_real_make_jev_is_audited_service_client():
    """운영 조립(make_jev)이 원시 usage 기록 훅을 단 ServiceJevClient인지(#61 검수 4). 퇴행하면 B5 검증이 사라진다.
    클라이언트만 만들고 요청은 보내지 않는다."""
    from app.lib.jev_service import ServiceJevClient

    mj = fm_main.make_jev()
    try:
        assert isinstance(mj, metering.MeteredJev) and mj.audited is True
        assert isinstance(mj.inner, ServiceJevClient)
        hooks = mj.inner._client.event_hooks
        assert metering._on_request in hooks["request"] and metering._on_response in hooks["response"]
    finally:
        asyncio.run(mj.inner.aclose())



# ── T4-B2: 회사별 데이터 검사 ───────────────────────────────────────────────

@pytest.mark.parametrize("case,missing", [
    ("hynix_facts", [HYNIX]),  # SK하이닉스 XBRL 행이 없다
    ("hynix_passages", [HYNIX]),  # SK하이닉스 문단이 없다
    ("samsung_both", [SAMSUNG]),
])
def test_one_company_without_data_is_not_ready_and_named(boot, tmp_path, case, missing):
    store, d = Store(), None
    if case == "hynix_facts":
        d = data_dir(tmp_path, corps=("samsung",))
    elif case == "hynix_passages":
        d, store = data_dir(tmp_path), Store(by_corp={SAMSUNG: 7, HYNIX: 0})
    else:
        d, store = data_dir(tmp_path, corps=("skhynix",)), Store(by_corp={SAMSUNG: 0, HYNIX: 4})
    ready, health, post, _ = boot(data_dir=d, store=store)
    assert not ready.ready and ready.code == "data_unavailable"
    assert health.status_code == 503 and health.json()["checks"]["data"]["missing"] == missing
    assert post.status_code == 503


# ── T4-B1: 준비 안 됨 → 주기적으로 다시 확인 ───────────────────────────────

def test_health_distinguishes_qdrant_down_from_empty_collection(boot, tmp_path):
    _, down, _, _ = boot(data_dir=data_dir(tmp_path), store=Store(fail_exists=10 ** 6))
    assert down.json()["checks"]["data"]["qdrant"] == "unreachable"
    _, empty, _, _ = boot(data_dir=data_dir(tmp_path / "b"), store=Store(exists=False))
    assert empty.json()["checks"]["data"]["qdrant"] == "no_collection"
    _, zero, _, _ = boot(data_dir=data_dir(tmp_path / "c"), store=Store(passages=0))
    assert zero.json()["checks"]["data"]["qdrant"] == "ok" and zero.json()["checks"]["data"]["passages"] == 0


def test_not_ready_rechecks_and_opens_after_transient_failure(fc_pg, monkeypatch, tmp_path):
    """시작 때 Qdrant가 잠깐 안 되면 503이지만, 다시 확인해 회복하면 연다(재시작 불필요)."""
    monkeypatch.setattr(settings, "DATABASE_URL", fc_pg)
    monkeypatch.setattr(settings, "RUN_MIGRATIONS_ON_STARTUP", False)
    monkeypatch.setattr(fm_main, "load_api_key", lambda: "test-key")
    monkeypatch.setattr(fm_main, "make_jev", lambda on_auth_block=None: metering.service_client(
        api_key="k", transport=httpx.MockTransport(lambda r: httpx.Response(500)), on_auth_block=on_auth_block))
    store = Store(fail_exists=1)
    monkeypatch.setattr(fm_main, "make_store", lambda: store)
    d = data_dir(tmp_path)

    async def run():
        first = await fm_main.prepare(data_dir=d, recheck_s=0.05)
        try:
            transport = httpx.ASGITransport(app=fm_main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=ORIGIN) as c:
                h1 = await c.get("/api/health")
                for _ in range(100):
                    h2 = await c.get("/api/health")
                    if h2.status_code == 200:
                        break
                    await asyncio.sleep(0.05)
                post = await c.post("/api/factcheck", json={"corp_code": SAMSUNG, "source": "my_draft",
                                                            "text": "2025년 매출은 300조원이다."})
            return first, h1, h2, post, factcheck._not_ready
        finally:
            await fm_main.shutdown()

    first, h1, h2, post, not_ready = asyncio.run(run())
    assert not first.ready and h1.status_code == 503 and h1.json()["checks"]["data"]["qdrant"] == "unreachable"
    assert h2.status_code == 200 and not_ready is None and post.status_code == 202
    assert fm_main._recheck_task is None  # 종료하면 재확인 작업도 멈춘다


# ── T4-B4: 판정 키 거부가 이어지면 닫는다 ───────────────────────────────────

def test_auth_rejection_trip_closes_and_stays_closed(fc_pg, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATABASE_URL", fc_pg)
    monkeypatch.setattr(settings, "RUN_MIGRATIONS_ON_STARTUP", False)
    monkeypatch.setattr(fm_main, "load_api_key", lambda: "test-key")
    monkeypatch.setattr(fm_main, "make_jev", lambda on_auth_block=None: metering.service_client(
        api_key="k", transport=httpx.MockTransport(lambda r: httpx.Response(401)), on_auth_block=on_auth_block))
    monkeypatch.setattr(fm_main, "make_store", lambda: Store())
    d = data_dir(tmp_path)

    async def run():
        ready = await fm_main.prepare(data_dir=d, recheck_s=0.05)
        try:
            factcheck._pipeline.jev.on_auth_block()  # 차단기가 열린 것과 같다
            await asyncio.sleep(0.3)  # 재확인 주기가 여러 번 돌아도
            transport = httpx.ASGITransport(app=fm_main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=ORIGIN) as c:
                health = await c.get("/api/health")
                post = await c.post("/api/factcheck", json={"corp_code": SAMSUNG, "source": "my_draft",
                                                            "text": "2025년 매출은 300조원이다."})
            return ready, health, post, factcheck._quota
        finally:
            await fm_main.shutdown()

    ready, health, post, quota = asyncio.run(run())
    assert ready.ready  # 처음엔 열렸다
    assert health.status_code == 503 and health.json()["code"] == "no_api_key"
    assert health.json()["checks"]["api_key_rejected"] is True
    assert post.status_code == 503 and post.json()["detail"]["code"] == "no_api_key"
    assert quota is not None  # 한도·익명 키는 그대로(키만 막는다)
