"""관심종목 corp_code 매핑과 시장 계산(모듈 D spec 결정 4-3, 5-2).

Yahoo 심볼 → stock_code → 근거 모드 문단 저장소의 적재 회사 목록에서 정확히 하나 맞는 corp_code.
지어내지 않는다: 이름 유사도·KRX 목록 추정·OpenDART 조회로 채우지 않는다. 매핑 실패는 None이다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Awaitable, Callable, Optional

log = logging.getLogger("app.watchlist.mapping")

KR_SYMBOL = re.compile(r"^(\d{6})\.(KS|KQ)$")
MARKETS = {".KS": "KOSPI", ".KQ": "KOSDAQ"}
MARKET_MAX = 16
# 저장소가 멈춰 있으면 기다리지 않고 매핑 실패(null)로 본다.
# 판단 일지 변화 비교(journal.changes.STORE_TIMEOUT_S)와 같은 값
COMPANIES_TIMEOUT_S = 5.0

CompanyList = Callable[[], Awaitable[list[dict]]]


def stock_code_of(symbol: str) -> Optional[str]:
    """국내 상장 심볼(`NNNNNN.KS`/`NNNNNN.KQ`)의 6자리 종목코드.

    @param symbol 대문자로 정규화한 Yahoo 심볼
    @returns 6자리 종목코드, 형식 밖(해외·지수·ETF 등)이면 None
    """
    m = KR_SYMBOL.match(symbol or "")
    return m.group(1) if m else None


def corp_code_for(stock_code: Optional[str], loaded: Optional[list[dict]]) -> Optional[str]:
    """적재 회사 목록에서 stock_code가 같은 회사의 corp_code. 정확히 하나 맞을 때만 돌려준다.

    @param stock_code 6자리 종목코드(None이면 매핑하지 않음)
    @param loaded 문단 저장소 companies() 결과(`corp_code`·`stock_code`를 가진 dict 목록), None이면 저장소 없음
    @returns DART 고유번호, 0개·2개 이상 맞으면 None
    """
    if not stock_code or not loaded:
        return None
    hits = {c["corp_code"] for c in loaded if c.get("stock_code") == stock_code and c.get("corp_code")}
    return hits.pop() if len(hits) == 1 else None


def market_of(symbol: str, exchange: Optional[str]) -> Optional[str]:
    """표시용 시장 이름. `.KS` → KOSPI, `.KQ` → KOSDAQ, 그 밖에는 검색 결과 거래소 문자열을 16자로 자른 값.

    @param symbol 대문자로 정규화한 Yahoo 심볼
    @param exchange 종목 검색 결과의 거래소 문자열(없을 수 있다)
    @returns 시장 이름, 정할 수 없으면 None
    """
    for suffix, market in MARKETS.items():
        if symbol.endswith(suffix):
            return market
    exchange = (exchange or "").strip()
    return exchange[:MARKET_MAX] or None


async def load_companies(companies: Optional[CompanyList]) -> Optional[list[dict]]:
    """근거 모드 적재 회사 목록을 한 번 불러온다. 저장소가 없거나 실패하면 None(추가·목록을 막지 않는다).

    @param companies 문단 저장소의 companies(근거 모드가 꺼져 연결이 없으면 None)
    @returns 적재 회사 목록, 저장소 없음·예외·시간 초과(COMPANIES_TIMEOUT_S)면 None. 로그에는 예외 이름만 남긴다
    """
    if companies is None:
        return None
    try:
        return await asyncio.wait_for(companies(), COMPANIES_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        log.warning(json.dumps({"event": "watchlist_company_list_failed", "error": type(exc).__name__}))
        return None
