# tests/evidence/test_watchlist_mapping.py
"""관심종목 corp_code 매핑·시장 계산 순수 함수(모듈 D spec 결정 4-3, 5-2). DB·외부 호출 없음."""
import asyncio
import json
import logging

import pytest

from app.services import watchlist as wl

LOADED = [
    {"corp_code": "00126380", "corp_name": "삼성전자", "stock_code": "005930", "rcept_no": "1", "passages": 3},
    {"corp_code": "00164779", "corp_name": "SK하이닉스", "stock_code": "000660", "rcept_no": "2", "passages": 2},
    {"corp_code": "00877059", "corp_name": "에코프로비엠", "stock_code": "247540", "rcept_no": "3", "passages": 1},
]


@pytest.mark.parametrize("symbol,code", [("005930.KS", "005930"), ("247540.KQ", "247540"),
                                         ("AAPL", None), ("^KS11", None), ("005930", None),
                                         ("05930.KS", None), ("0059301.KS", None), ("005930.KX", None),
                                         ("BRK-B", None), ("ABCDEF.KS", None)])
def test_stock_code_only_for_six_digit_ks_kq(symbol, code):
    assert wl.stock_code_of(symbol) == code


def test_corp_code_for_loaded_ks_and_kq():
    assert wl.corp_code_for("005930", LOADED) == "00126380"
    assert wl.corp_code_for("247540", LOADED) == "00877059"


def test_corp_code_not_loaded_is_none():
    assert wl.corp_code_for("000000", LOADED) is None
    assert wl.corp_code_for("005935", LOADED) is None  # 우선주는 stock_code가 달라 null(지어내지 않는다)


def test_corp_code_ambiguous_is_none():
    dup = LOADED + [{"corp_code": "99999999", "corp_name": "다른회사", "stock_code": "005930", "rcept_no": "9",
                     "passages": 1}]
    assert wl.corp_code_for("005930", dup) is None


def test_corp_code_none_inputs():
    assert wl.corp_code_for(None, LOADED) is None
    assert wl.corp_code_for("005930", None) is None
    assert wl.corp_code_for("005930", []) is None


@pytest.mark.parametrize("symbol,exchange,market", [
    ("005930.KS", None, "KOSPI"), ("005930.KS", "KSC", "KOSPI"), ("247540.KQ", "KOE", "KOSDAQ"),
    ("AAPL", "NASDAQ", "NASDAQ"), ("AAPL", None, None), ("AAPL", "", None), ("AAPL", "  ", None),
    ("X", "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "ABCDEFGHIJKLMNOP"),
])
def test_market_of(symbol, exchange, market):
    assert wl.market_of(symbol, exchange) == market


def test_load_companies_store_none_is_none():
    assert asyncio.run(wl.load_companies(None)) is None


def test_load_companies_returns_list():
    calls = []

    async def companies():
        calls.append(1)
        return LOADED

    assert asyncio.run(wl.load_companies(companies)) == LOADED and calls == [1]


def test_load_companies_exception_is_none_and_logs_only_type(caplog):
    caplog.set_level(logging.DEBUG)

    async def companies():
        raise RuntimeError("qdrant down 비밀")

    assert asyncio.run(wl.load_companies(companies)) is None
    logged = [json.loads(r.getMessage()) for r in caplog.records if r.name.startswith("app.watchlist")]
    assert logged == [{"event": "watchlist_company_list_failed", "error": "RuntimeError"}]
