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


def test_lists_loaded_companies_sorted_by_name(enabled):
    app = make_app(None, Who(USER), companies=_list())
    r = _get(app, "/api/evidence/companies")
    assert r.status_code == 200
    assert [c["corp_name"] for c in r.json()["companies"]] == ["SK하이닉스", "삼성SDI", "삼성전자"]
    assert r.json()["companies"][0] == LOADED[0]


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


def test_query_by_stock_code_prefix_when_krx_empty(enabled):
    app = make_app(None, Who(USER), companies=_list(), krx_search=_krx([]))
    r = _get(app, "/api/evidence/companies?q=0059")
    assert [c["corp_code"] for c in r.json()["companies"]] == ["00126380"]


def test_store_failure_is_503(enabled):
    async def broken():
        raise ConnectionError("qdrant down")
    app = make_app(None, Who(USER), companies=broken)
    assert _get(app, "/api/evidence/companies").status_code == 503


def test_set_company_list_roundtrip():
    try:
        fn = _list()
        evidence.set_company_list(fn)
        assert evidence.get_company_list() is fn
    finally:
        evidence.set_company_list(None)
