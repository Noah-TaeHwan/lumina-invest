"""브라우저 E2E: 기업 지표 대시보드(#company-dashboard)가 종목 통화에 맞는 단위를 붙이는지 확인한다.

KRW 종목은 기존 표기(원·조원·(억원)), USD 종목은 "달러·조 달러·(억 달러)"로 "원"이 보이지 않아야 한다.
가격 통화와 재무 통화가 다르면 카드(주당 값·시가총액)와 표(재무 금액)가 각자 통화를 따른다.
Yahoo가 통화를 주지 않으면(null) 단위를 지어내지 않고 "통화 미확인"을 보여 준다.
앱 서버·DB 없이 돈다. public/을 정적 서버로 띄우고 /api/** 는 가짜 응답(journal_views.JournalApi)을 준다.
외부 CDN 요청은 끊는다. pytest 수집 대상이 아니다(파일명이 test_* 가 아님).
    pip install playwright        # 브라우저가 없으면 playwright install chromium
    python tests/e2e/company_views.py
종료 코드 0 = 모든 확인 통과, 1 = 실패 있음.
"""
import asyncio
import os
import sys
from urllib.parse import parse_qs

from playwright.async_api import async_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evidence_views as ev  # noqa: E402  정적 서버·Checks·브라우저 경로를 그대로 쓴다
import journal_views as jv  # noqa: E402  일지 기능 확인까지 상태로 기다리는 open_app을 쓴다


def fundamentals(symbol, name, currency, financial_currency, *, price, cap, eps, bps, div, rev, assets):
    """/api/stocks/fundamentals 응답 모양(금액은 ÷1e8 스케일)."""
    return {"symbol": symbol, "name": name, "currency": currency, "financial_currency": financial_currency,
            "price": price, "chg": 1.23, "cap": cap, "per": 30.1, "pbr": 4.5, "eps": eps, "bps": bps,
            "roe": 15.0, "roa": 8.0, "debt": 45.6, "div": div, "divYield": 0.5, "opMargin": 30.0,
            "revenue": [rev, rev + 10], "op": [rev // 4, rev // 4], "net": [rev // 5, rev // 5],
            "quarters": ["26Q1", "26Q2"], "assets": assets, "equity": assets // 5,
            "liabilities": assets - assets // 5, "cash": assets // 10}


FUND = {
    "005930.KS": fundamentals("005930.KS", "삼성전자", "KRW", "KRW", price=73400, cap=4378000, eps=4532,
                              bps=50600, div=1416, rev=302300, assets=4263000),
    "AAPL": fundamentals("AAPL", "Apple Inc.", "USD", "USD", price=189.5, cap=34000, eps=6.57, bps=4.2,
                         div=1.0, rev=950, assets=3500),
    "PKX": fundamentals("PKX", "POSCO Holdings ADR", "USD", "KRW", price=61.25, cap=170, eps=1.1, bps=90.5,
                        div=1.8, rev=180000, assets=1000000),
    "7203.T": fundamentals("7203.T", "Toyota", "JPY", "JPY", price=2850.0, cap=450000, eps=350.2, bps=2900.0,
                           div=75.0, rev=120000, assets=900000),
    "XYZ": fundamentals("XYZ", "통화 없는 종목", None, None, price=12.5, cap=500, eps=0.8, bps=5.0,
                        div=None, rev=40, assets=300),
}


class CompanyApi(jv.JournalApi):
    def respond(self, method, path, query, body):
        if path == "/api/stocks/fundamentals":
            self.calls.append((method, path, body))
            sym = parse_qs(query or "").get("symbol", [""])[0]
            return (200, FUND[sym]) if sym in FUND else (502, {"detail": "데이터 없음"})
        return super().respond(method, path, query, body)


async def texts(page) -> dict:
    return await page.evaluate("""() => Object.fromEntries(
      ['co-overview', 'co-valuation', 'co-quarterly', 'co-balance'].map(id =>
        [id, document.getElementById(id)?.innerText || '']))""")


async def show(page, symbol: str, name: str) -> dict:
    if symbol != "005930.KS":
        await page.evaluate(f"selectCompany({symbol!r})")
    # 선택한 종목의 카드가 그려질 때까지 상태로 기다린다(시가총액 카드 sub = 종목명)
    await page.wait_for_function(
        "(n) => (document.getElementById('co-overview')?.innerText || '').includes(n)"
        " && (document.getElementById('co-overview')?.innerText || '').includes('현재가')", arg=name)
    return await texts(page)


async def s_company(browser, base, ck: ev.Checks):
    ctx, page = await jv.open_app(browser, base, CompanyApi(), hash_="company-dashboard")
    await page.wait_for_function("document.querySelector('.view.active')?.dataset.view === 'company-dashboard'")

    t = await show(page, "005930.KS", "삼성전자")
    ov, val, qt, bal = t["co-overview"], t["co-valuation"], t["co-quarterly"], t["co-balance"]
    print("[company] KRW 005930.KS")
    ck.ok("73,400원" in ov, f"KRW: 현재가 '73,400원' {ov[:120]!r}")
    ck.ok("438조원" in ov, f"KRW: 시가총액 '438조원'")
    ck.ok("4,532원" in ov and "50,600원" in ov, "KRW: EPS·BPS 원 단위")
    ck.ok("1,416원" in val, f"KRW: DPS '1,416원' {val!r}")
    ck.ok("매출액 (억원)" in qt and "순이익 (억원)" in qt, f"KRW: 분기 실적 머리글 (억원) {qt[:80]!r}")
    ck.ok("금액 (억원)" in bal, "KRW: 재무상태 머리글 금액 (억원)")
    ck.ok("달러" not in ov + val + qt + bal and "통화 미확인" not in ov + qt, "KRW: 달러·통화 미확인이 보이지 않는다")

    t = await show(page, "AAPL", "Apple Inc.")
    ov, val, qt, bal = t["co-overview"], t["co-valuation"], t["co-quarterly"], t["co-balance"]
    print("[company] USD AAPL")
    ck.ok("원" not in ov + val + qt + bal, f"USD: '원'이 어디에도 없다 {(ov + qt + bal)[:160]!r}")
    ck.ok("189.50 달러" in ov, f"USD: 현재가 '189.50 달러' {ov[:120]!r}")
    ck.ok("3.40조 달러" in ov, "USD: 시가총액 '3.40조 달러'")
    ck.ok("6.57 달러" in ov and "4.20 달러" in ov, "USD: EPS·BPS 센트까지 달러")
    ck.ok("1.00 달러" in val, f"USD: DPS '1.00 달러' {val!r}")
    ck.ok("매출액 (억 달러)" in qt and "영업이익 (억 달러)" in qt, f"USD: 분기 실적 머리글 (억 달러) {qt[:80]!r}")
    ck.ok("금액 (억 달러)" in bal, "USD: 재무상태 머리글 금액 (억 달러)")

    t = await show(page, "PKX", "POSCO Holdings ADR")
    ov, val, qt, bal = t["co-overview"], t["co-valuation"], t["co-quarterly"], t["co-balance"]
    print("[company] 가격 USD · 재무 KRW PKX")
    ck.ok("61.25 달러" in ov and "원" not in ov, f"혼합: 카드는 가격 통화(달러) {ov[:120]!r}")
    ck.ok("1.80 달러" in val, "혼합: DPS는 가격 통화(달러)")
    ck.ok("매출액 (억원)" in qt and "달러" not in qt, f"혼합: 분기 실적은 재무 통화(억원) {qt[:80]!r}")
    ck.ok("금액 (억원)" in bal and "달러" not in bal, "혼합: 재무상태는 재무 통화(억원)")

    t = await show(page, "7203.T", "Toyota")
    ov, val, qt, bal = t["co-overview"], t["co-valuation"], t["co-quarterly"], t["co-balance"]
    print("[company] JPY 7203.T")
    ck.ok("2,850.00 JPY" in ov and "45.00조 JPY" in ov, f"JPY: ISO 코드 단위 {ov[:120]!r}")
    ck.ok("매출액 (억 JPY)" in qt and "금액 (억 JPY)" in bal, "JPY: 표 머리글 (억 JPY)")
    ck.ok("원" not in ov + val + qt + bal and "달러" not in ov + qt, "JPY: 원·달러가 보이지 않는다")

    t = await show(page, "XYZ", "통화 없는 종목")
    ov, val, qt, bal = t["co-overview"], t["co-valuation"], t["co-quarterly"], t["co-balance"]
    print("[company] 통화 없음 XYZ")
    ck.ok("통화 미확인" in ov, f"null: 카드에 '통화 미확인' {ov[:160]!r}")
    ck.ok("통화 미확인" in qt and "통화 미확인" in bal, "null: 표 머리글에 '통화 미확인'")
    ck.ok("12.50" in ov and "0.05조" in ov, "null: 숫자·스케일은 그대로 보인다")
    ck.ok("원" not in ov + val + qt + bal and "달러" not in ov + val + qt + bal,
          "null: 통화 단위를 지어내지 않는다(원·달러 없음)")

    ck.ok(not page.errors, f"페이지 오류 없음 {page.errors[:2]}")
    await ctx.close()


async def main() -> int:
    srv, base = ev._serve()
    ck = ev.Checks()
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=ev._chromium())
        try:
            await s_company(browser, base, ck)
        except Exception as exc:  # noqa: BLE001
            ck.ok(False, f"company: 예외 {type(exc).__name__}: {str(exc)[:300]}")
        await browser.close()
    srv.shutdown()
    print(f"checks passed={ck.passed} failed={len(ck.failures)}")
    return 1 if ck.failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
