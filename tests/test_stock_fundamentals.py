"""get_fundamentals 통화 필드: Yahoo quoteSummary를 고정 픽스처로 흉내 내 외부 호출 없이 확인한다.

가격·주당 값 통화(currency)와 재무제표 금액 통화(financial_currency)를 따로 돌려주고,
Yahoo가 통화를 주지 않으면 지어내지 않고 None으로 둔다. 금액 스케일(÷1e8)은 그대로다.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.services import stock


def _v(x):
    return {"raw": x, "fmt": str(x)}


def quote_summary(*, currency=None, financial_currency=None, price=100.0, cap=3.4e12, revenue=1.2e10) -> dict:
    price_mod = {"longName": "테스트 기업", "regularMarketPrice": _v(price),
                 "regularMarketChangePercent": _v(0.0123), "marketCap": _v(cap)}
    if currency is not None:
        price_mod["currency"] = currency
    fin = {"returnOnEquity": _v(0.15), "debtToEquity": _v(45.67)}
    if financial_currency is not None:
        fin["financialCurrency"] = financial_currency
    return {
        "price": price_mod,
        "summaryDetail": {"trailingPE": _v(30.1), "dividendRate": _v(1.0)},
        "defaultKeyStatistics": {"trailingEps": _v(6.57), "bookValue": _v(4.2), "priceToBook": _v(45.0)},
        "financialData": fin,
        "incomeStatementHistoryQuarterly": {"incomeStatementHistory": [
            {"endDate": {"fmt": "2026-06-30"}, "totalRevenue": _v(revenue),
             "operatingIncome": _v(3e9), "netIncome": _v(2.5e9)},
        ]},
        "balanceSheetHistory": {"balanceSheetStatements": [
            {"totalAssets": _v(3.5e11), "totalStockholderEquity": _v(7e10), "totalLiab": _v(2.8e11), "cash": _v(3e10)},
        ]},
    }


@pytest.fixture
def yahoo(monkeypatch):
    """Yahoo·캐시를 가짜로 바꾼다. state["summary"]를 응답으로 주고, state["cache"]를 캐시로 쓴다."""
    state: dict = {"summary": None, "cache": {}, "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(str(request.url))
        return httpx.Response(200, json={"quoteSummary": {"result": [state["summary"]], "error": None}})

    real_client = httpx.AsyncClient

    def fake_client(*args, **kw):
        kw.pop("transport", None)
        return real_client(*args, transport=httpx.MockTransport(handler), **kw)

    async def no_crumb():
        return None

    async def fake_cache_get(key, max_age_hours=24):
        return state["cache"].get(key)

    async def fake_cache_set(key, data):
        state["cache"][key] = data

    monkeypatch.setattr(stock.httpx, "AsyncClient", fake_client)
    monkeypatch.setattr(stock, "_get_yahoo_crumb", no_crumb)
    monkeypatch.setattr(stock, "cache_get", fake_cache_get)
    monkeypatch.setattr(stock, "cache_set", fake_cache_set)
    return state


def fetch(symbol: str) -> dict:
    return asyncio.run(stock.get_fundamentals(symbol))


def test_usd_stock_returns_usd_currencies_with_same_scale(yahoo):
    yahoo["summary"] = quote_summary(currency="USD", financial_currency="USD")
    d = fetch("AAPL")
    assert d["currency"] == "USD"
    assert d["financial_currency"] == "USD"
    # 금액 스케일은 지금과 같다(÷1e8): 1.2e10 → 120, 3.4e12 → 34000
    assert d["revenue"] == [120.0]
    assert d["cap"] == 34000.0
    assert d["assets"] == 3500.0
    assert d["price"] == 100.0
    assert d["eps"] == 6.57


def test_krw_stock_keeps_values_and_adds_krw(yahoo):
    yahoo["summary"] = quote_summary(currency="KRW", financial_currency="KRW", price=73400.0,
                                     cap=4.378e14, revenue=3.023e13)
    d = fetch("005930.KS")
    assert d["currency"] == "KRW"
    assert d["financial_currency"] == "KRW"
    assert d["price"] == 73400.0
    assert d["cap"] == 4378000.0
    assert d["revenue"] == [302300.0]


def test_price_and_financial_currency_returned_separately(yahoo):
    yahoo["summary"] = quote_summary(currency="USD", financial_currency="KRW")
    d = fetch("PKX")
    assert d["currency"] == "USD"
    assert d["financial_currency"] == "KRW"


def test_missing_currency_fields_are_none_not_invented(yahoo):
    yahoo["summary"] = quote_summary()
    d = fetch("XYZ")
    assert "currency" in d and d["currency"] is None
    assert "financial_currency" in d and d["financial_currency"] is None


def test_old_cache_entry_without_currency_is_not_served(yahoo, monkeypatch):
    """옛 형식(통화 필드 없음) 캐시가 남아 있어도 그대로 내보내지 않고 새로 받는다(키 이름과 무관하게)."""
    old = {"symbol": "AAPL", "name": "Apple", "price": 100.0, "cap": 34000.0, "revenue": [120.0]}

    async def old_cache_get(key, max_age_hours=24):
        return dict(old)

    monkeypatch.setattr(stock, "cache_get", old_cache_get)
    yahoo["summary"] = quote_summary(currency="USD", financial_currency="USD")
    d = fetch("AAPL")
    assert d["currency"] == "USD"
    assert d["financial_currency"] == "USD"
    assert yahoo["requests"], "옛 캐시를 쓰지 않고 Yahoo를 다시 불러야 한다"


def test_new_cache_entry_is_reused(yahoo):
    yahoo["summary"] = quote_summary(currency="USD", financial_currency="USD")
    first = fetch("AAPL")
    n = len(yahoo["requests"])
    second = fetch("AAPL")
    assert second == first
    assert len(yahoo["requests"]) == n


def test_cached_none_currency_is_reused(yahoo):
    """통화 미확인(None)도 새 형식이다 — 키가 있으면 캐시를 쓴다."""
    yahoo["summary"] = quote_summary()
    fetch("XYZ")
    n = len(yahoo["requests"])
    d = fetch("XYZ")
    assert d["currency"] is None
    assert len(yahoo["requests"]) == n
