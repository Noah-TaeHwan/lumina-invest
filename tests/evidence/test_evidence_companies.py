# tests/evidence/test_evidence_companies.py
"""회사 선택 후보 API(P3, spec 결정 3-1): 적재된 회사만, 검색어가 있으면 KRX 검색 결과와 교집합.

DB를 쓰지 않는다. 적재 목록·KRX 검색은 가짜 함수로 주입한다.
"""
import asyncio
import uuid

import pytest

from app.config import settings
from app.routes import evidence
from tests.evidence.p2_support import Who, client, make_app

LOADED = [
    {"corp_code": "00164779", "corp_name": "SK하이닉스", "stock_code": "000660", "rcept_no": "2", "passages": 90},
    {"corp_code": "00126380", "corp_name": "삼성전자", "stock_code": "005930", "rcept_no": "1", "passages": 120},
    {"corp_code": "00126371", "corp_name": "삼성SDI", "stock_code": "006400", "rcept_no": "3", "passages": 80},
]
USER = {"id": str(uuid.uuid4()), "roles": ["user"]}


def _list(rows=LOADED):
    async def companies():
        return [dict(r) for r in rows]
    return companies


def _krx(results, calls=None, exc=None):
    async def search(q, limit=10):
        if calls is not None:
            calls.append((q, limit))
        if exc:
            raise exc
        return results
    return search


def _get(app, path):
    async def go():
        async with client(app) as c:
            return await c.get(path)
    return asyncio.run(go())


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "EVIDENCE_CHAT_ENABLED", True)


def test_flag_off_is_404(monkeypatch):
    monkeypatch.setattr(settings, "EVIDENCE_CHAT_ENABLED", False)
    app = make_app(None, Who(None), companies=_list())
    assert _get(app, "/api/evidence/companies").status_code == 404


def test_login_required(enabled):
    app = make_app(None, Who(None), companies=_list())
    assert _get(app, "/api/evidence/companies").status_code == 401


def test_unwired_store_is_503(enabled):
    app = make_app(None, Who(USER), companies=None)
    r = _get(app, "/api/evidence/companies")
    assert r.status_code == 503
    assert r.json()["detail"] == "공시 문단 저장소가 아직 준비되지 않았습니다"


def test_lists_loaded_companies_in_store_order(enabled):
    """정렬은 저장소(store.companies, 이름순)가 한다. 라우트는 다시 정렬하지 않는다."""
    rows = list(reversed(LOADED))
    app = make_app(None, Who(USER), companies=_list(rows))
    r = _get(app, "/api/evidence/companies")
    assert r.status_code == 200
    assert r.json()["companies"] == rows


def test_query_intersects_krx_search_in_krx_order(enabled):
    calls = []
    krx = _krx([{"symbol": "005930.KS", "name": "삼성전자", "exchange": "KOSPI", "type": "주식"},
                {"symbol": "028260.KS", "name": "삼성물산", "exchange": "KOSPI", "type": "주식"},
                {"symbol": "006400.KS", "name": "삼성SDI", "exchange": "KOSPI", "type": "주식"}], calls)
    app = make_app(None, Who(USER), companies=_list(), krx_search=krx)
    r = _get(app, "/api/evidence/companies?q=삼성")
    assert [c["corp_code"] for c in r.json()["companies"]] == ["00126380", "00126371"]
    assert calls and calls[0][0] == "삼성"


def test_query_falls_back_to_loaded_names_when_krx_unavailable(enabled):
    app = make_app(None, Who(USER), companies=_list(), krx_search=_krx([], exc=RuntimeError("s3 down")))
    r = _get(app, "/api/evidence/companies?q=하이닉스")
    assert r.status_code == 200
    assert [c["corp_code"] for c in r.json()["companies"]] == ["00164779"]


def test_query_falls_back_when_krx_hits_miss_loaded(enabled):
    """KRX 상위 결과에 적재 회사가 없으면(상위 50 밖 등) 조용히 빈 목록이 아니라 적재 목록에서 찾는다."""
    krx = _krx([{"symbol": "000661.KS", "name": "SK하이닉스우", "exchange": "KOSPI", "type": "주식"}])
    app = make_app(None, Who(USER), companies=_list(), krx_search=krx)
    r = _get(app, "/api/evidence/companies?q=하이닉스")
    assert [c["corp_code"] for c in r.json()["companies"]] == ["00164779"]


def test_query_by_stock_code_prefix_when_krx_empty(enabled):
    app = make_app(None, Who(USER), companies=_list(), krx_search=_krx([]))
    r = _get(app, "/api/evidence/companies?q=0059")
    assert [c["corp_code"] for c in r.json()["companies"]] == ["00126380"]


def test_store_failure_is_503(enabled):
    async def broken():
        raise ConnectionError("qdrant down")
    app = make_app(None, Who(USER), companies=broken)
    assert _get(app, "/api/evidence/companies").status_code == 503


def test_set_passage_store_wires_search_and_companies():
    class Store:
        async def search(self, corp_code, question):
            return []

        async def companies(self):
            return []

    st = Store()
    try:
        evidence.set_passage_store(st)
        assert evidence.get_passage_search() == st.search
        assert evidence.get_company_list() == st.companies
    finally:
        evidence.set_passage_store(None)
    assert evidence.get_passage_search() is None and evidence.get_company_list() is None


def test_missing_collection_is_503_then_lists_after_load(enabled):
    """시작 때 컬렉션이 없던 실제 저장소: 503 → 적재 뒤 재시작 없이 200."""
    from qdrant_client import AsyncQdrantClient

    from app.services.evidence import store
    from app.services.evidence.dart import Corp
    from app.services.evidence.passages import Passage
    from tests.evidence.test_passage_store import FakeEmbed

    s = store.PassageStore(AsyncQdrantClient(location=":memory:"), FakeEmbed())
    app = make_app(None, Who(USER), companies=s.companies)
    corp = Corp("00126380", "삼성전자", "005930")

    async def go():
        async with client(app) as c:
            before = await c.get("/api/evidence/companies")
            await s.load(corp, [Passage("00126380-II-0000", corp.corp_code, "1", "II", 0, "본문")])
            return before, await c.get("/api/evidence/companies")

    before, after = asyncio.run(go())
    assert before.status_code == 503
    assert after.status_code == 200 and [c["corp_code"] for c in after.json()["companies"]] == ["00126380"]
