"""get_fundamentals 통화·출처·시점 필드: Yahoo quoteSummary를 고정 픽스처로 흉내 내 외부 호출 없이 확인한다.

가격·주당 값 통화(currency)와 재무제표 금액 통화(financial_currency)를 따로 돌려주고,
Yahoo가 통화를 주지 않으면 지어내지 않고 None으로 둔다. 금액 스케일(÷1e8)은 그대로다.
출처(source)·Yahoo에서 받은 시각(fetched_at, UTC)·분기 끝 날짜(quarter_ends)를 함께 돌려주고,
캐시 적중 때는 처음 받은 시각을 그대로 둔다(모듈 D spec 결정 6-1).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

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
        # Yahoo는 최근 분기부터 준다(get_fundamentals가 뒤집어 오래된 분기부터 담는다)
        "incomeStatementHistoryQuarterly": {"incomeStatementHistory": [
            {"endDate": {"fmt": "2026-06-30"}, "totalRevenue": _v(revenue),
             "operatingIncome": _v(3e9), "netIncome": _v(2.5e9)},
            {"endDate": {}, "totalRevenue": _v(revenue)},
            {"endDate": {"fmt": "2025-12-31"}, "totalRevenue": _v(revenue)},
        ]},
        "balanceSheetHistory": {"balanceSheetStatements": [
            {"totalAssets": _v(3.5e11), "totalStockholderEquity": _v(7e10), "totalLiab": _v(2.8e11), "cash": _v(3e10)},
        ]},
    }


@pytest.fixture
def yahoo(monkeypatch):
    """Yahoo·캐시를 가짜로 바꾼다. state["summary"]를 응답으로 주고, state["cache"]를 캐시로 쓴다."""
    state: dict = {"summary": None, "cache": {}, "requests": [], "status": 200,
                   "now": datetime(2026, 10, 5, 5, 32, 7, 481932, tzinfo=timezone.utc)}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(str(request.url))
        if state["status"] != 200:
            return httpx.Response(state["status"], json={"quoteSummary": {"result": None, "error": "x"}})
        result = [state["summary"]] if state["summary"] is not None else []
        return httpx.Response(200, json={"quoteSummary": {"result": result, "error": None}})

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
    monkeypatch.setattr(stock, "_utc_now", lambda: state["now"])
    return state


def fetch(symbol: str) -> dict:
    return asyncio.run(stock.get_fundamentals(symbol))


def test_usd_stock_returns_usd_currencies_with_same_scale(yahoo):
    yahoo["summary"] = quote_summary(currency="USD", financial_currency="USD")
    d = fetch("AAPL")
    assert d["currency"] == "USD"
    assert d["financial_currency"] == "USD"
    # 금액 스케일은 지금과 같다(÷1e8): 1.2e10 → 120, 3.4e12 → 34000
    assert d["revenue"][-1] == 120.0
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
    assert d["revenue"][-1] == 302300.0


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


# ── 출처·시점(모듈 D P3, spec 결정 6-1·수용 기준 8) ─────────────────────────


def test_source_and_fetched_at_added_with_currencies_kept(yahoo):
    yahoo["summary"] = quote_summary(currency="USD", financial_currency="KRW")
    d = fetch("PKX")
    assert d["source"] == "Yahoo Finance"
    assert d["fetched_at"] == "2026-10-05T05:32:07.481932+00:00"
    parsed = datetime.fromisoformat(d["fetched_at"])
    assert parsed.utcoffset() == timedelta(0), "fetched_at은 UTC ISO 8601"
    assert d["currency"] == "USD" and d["financial_currency"] == "KRW"


def test_quarter_ends_follow_quarters_order_and_length(yahoo):
    yahoo["summary"] = quote_summary(currency="KRW", financial_currency="KRW")
    d = fetch("005930.KS")
    assert d["quarters"] == ["25Q4", "", "26Q2"]
    assert d["quarter_ends"] == ["2025-12-31", "", "2026-06-30"]
    assert len(d["quarter_ends"]) == len(d["quarters"])


def test_cache_hit_keeps_first_fetched_at(yahoo):
    yahoo["summary"] = quote_summary(currency="USD", financial_currency="USD")
    first = fetch("AAPL")
    n = len(yahoo["requests"])
    yahoo["now"] = yahoo["now"] + timedelta(hours=3)
    second = fetch("AAPL")
    assert len(yahoo["requests"]) == n, "캐시 적중이면 Yahoo를 부르지 않는다"
    assert second["fetched_at"] == first["fetched_at"] == "2026-10-05T05:32:07.481932+00:00"


def test_old_v2_entry_without_fetched_at_is_refetched_and_overwritten_under_same_key(yahoo):
    """PR #39 형식(통화 있음, 출처·시점 없음) v2 행 → 미스 → 재요청 → 같은 v2 키에 새 형식으로 덮어쓴다.

    이 테스트가 통과하므로 캐시 키는 fundamentals:v2:{symbol}을 유지한다(spec 결정 6-1).
    """
    key = "fundamentals:v2:AAPL"
    yahoo["cache"][key] = {"symbol": "AAPL", "name": "Apple", "currency": "USD", "financial_currency": "USD",
                           "price": 1.0, "quarters": ["26Q2"], "revenue": [1.0]}
    yahoo["summary"] = quote_summary(currency="USD", financial_currency="USD")
    d = fetch("AAPL")
    assert yahoo["requests"], "fetched_at 없는 옛 v2 행을 쓰지 않고 Yahoo를 다시 불러야 한다"
    assert d["price"] == 100.0 and d["fetched_at"] == "2026-10-05T05:32:07.481932+00:00"
    assert set(yahoo["cache"]) == {key}, "새 키를 만들지 않고 v2 키를 쓴다"
    stored = yahoo["cache"][key]
    for k in ("currency", "financial_currency", "source", "fetched_at", "quarter_ends"):
        assert k in stored, k


@pytest.mark.parametrize("missing", ["currency", "financial_currency", "fetched_at", "quarter_ends"])
def test_cached_entry_missing_any_required_key_is_refetched(yahoo, missing):
    yahoo["summary"] = quote_summary(currency="USD", financial_currency="USD")
    fetch("AAPL")
    n = len(yahoo["requests"])
    yahoo["cache"]["fundamentals:v2:AAPL"].pop(missing)
    d = fetch("AAPL")
    assert len(yahoo["requests"]) == n + 1
    assert missing in d


@pytest.mark.parametrize("status,summary", [(200, None), (500, None)])
def test_error_response_has_no_source_fields_and_is_not_cached(yahoo, status, summary):
    yahoo["status"] = status
    yahoo["summary"] = summary
    d = fetch("NOPE")
    assert "error" in d
    for k in ("source", "fetched_at", "quarter_ends"):
        assert k not in d
    assert yahoo["cache"] == {}


def test_cache_ttl_is_the_single_constant(yahoo, monkeypatch):
    """서버 캐시 유효 시간은 상수 하나(FUNDAMENTALS_CACHE_HOURS = 6)이고 cache_get에 그 값을 준다."""
    seen = []

    async def spy_get(key, max_age_hours=24):
        seen.append(max_age_hours)
        return None

    monkeypatch.setattr(stock, "cache_get", spy_get)
    yahoo["summary"] = quote_summary(currency="USD", financial_currency="USD")
    fetch("AAPL")
    assert stock.FUNDAMENTALS_CACHE_HOURS == 6
    assert seen == [stock.FUNDAMENTALS_CACHE_HOURS]
