# tests/factcheck/test_fc_readiness.py
"""진입점 준비 상태(배포 전 검수 A1·A4·B4·C3): 데이터·API 키·복구가 준비되지 않으면 검수를 받지 않고 /api/health에 드러낸다.

- A1: T2 pipeline.build_pipeline(경로를 주면 파일이 없을 때 예외)을 쓰고, XBRL 행·상장사명·문단이 하나라도 0이면 503
  (FACTCHECK_ALLOW_NO_DATA로만 허용). 시작 로그에 facts=·names=·passages=를 남긴다. 상대 경로는 저장소 루트 기준.
- A4: API 키를 읽을 수 없으면 파이프라인을 켜지 않는다(503 no_api_key).
- B4: 오래된 예약 복구가 실패하면 한도·익명 키를 공개하지 않는다(503 startup_failed). 복구 기준은 실행 마감 + 여유.
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

SAMSUNG = "00126380"
ORIGIN = {"origin": "http://t"}


class Store:
    """T1 FactcheckStore 모양의 가짜: exists·client.count·search·latest_period·aclose."""

    collection = "factcheck_passages"

    def __init__(self, passages=5, exists=True):
        self.n, self._exists, self.closed = passages, exists, False

        async def count(collection, exact=True):
            return SimpleNamespace(count=self.n)

        self.client = SimpleNamespace(count=count)

    async def exists(self):
        return self._exists

    async def search(self, corp_code, query, k=8, *, periods=None, report_types=None):
        return []

    async def latest_period(self, corp_code):
        return "2026H1"

    async def aclose(self):
        self.closed = True


def data_dir(tmp_path, *, facts=3, names=1):
    d = tmp_path / "data"
    d.mkdir()
    (d / "xbrl_facts.json").write_text(json.dumps(load_facts()[:facts], ensure_ascii=False))
    (d / "corp_names.json").write_text(json.dumps([{"corp_code": SAMSUNG, "corp_name": "삼성전자",
                                                    "stock_code": "005930", "norm": "삼성전자"}][:names],
                                                  ensure_ascii=False))
    return d


@pytest.fixture
def boot(fc_pg, monkeypatch):
    """prepare()를 테스트 DB로 돌리고 (readiness, health 응답, POST 응답, 출력)을 돌려주는 함수."""
    monkeypatch.setattr(settings, "DATABASE_URL", fc_pg)
    monkeypatch.setattr(settings, "RUN_MIGRATIONS_ON_STARTUP", False)
    monkeypatch.setattr(fm_main, "load_api_key", lambda: "test-key")

    def no_network(request):
        raise AssertionError("테스트에서 외부 JEV 호출이 나갔다")

    monkeypatch.setattr(fm_main, "make_jev",
                        lambda: metering.service_client(api_key="k", transport=httpx.MockTransport(no_network)))

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
    assert health.json()["checks"]["data"] == {"facts": 3, "names": 1, "passages": 5}
    assert isinstance(pipeline.jev, metering.MeteredJev)  # 실제 조립: MeteredJev(ServiceJevClient)
    assert post.status_code == 202
    assert "facts=3 names=1 passages=5" in capsys.readouterr().out


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
