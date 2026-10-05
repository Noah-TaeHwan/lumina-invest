"""브라우저 E2E: 기업 지표 대시보드(#company-dashboard)가 종목 통화에 맞는 단위를 붙이는지 확인한다.

KRW 종목은 기존 표기(원·조원·(억원)), USD 종목은 "달러·조 달러·(억 달러)"로 "원"이 보이지 않아야 한다.
가격 통화와 재무 통화가 다르면 카드(주당 값·시가총액)와 표(재무 금액)가 각자 통화를 따른다.
Yahoo가 통화를 주지 않으면(null) 단위를 지어내지 않고 "통화 미확인"을 보여 준다.
지표 아래 출처 줄은 fetched_at(UTC)을 KST "YYYY-MM-DD HH:mm"으로 보이고(UTC 자정 근처는 날짜가 넘어간다),
fetched_at이 없으면 "조회 시각 확인 불가"를 보인다. 투자 권유 아님 고지는 접히지 않고 보인다.
분기 표 머리 칸 툴팁은 분기 끝 날짜다. 브라우저 캐시의 fetched_at이 6시간 지났거나 없으면 뷰를 다시 열 때
지표를 다시 요청하고, 6시간 안이면 요청하지 않는다(모듈 D spec 결정 6-2·수용 기준 9).
앱 서버·DB 없이 돈다. public/을 정적 서버로 띄우고 /api/** 는 가짜 응답(journal_views.JournalApi)을 준다.
외부 CDN 요청은 끊는다. pytest 수집 대상이 아니다(파일명이 test_* 가 아님).
    pip install playwright        # 브라우저가 없으면 playwright install chromium
    python tests/e2e/company_views.py
종료 코드 0 = 모든 확인 통과, 1 = 실패 있음.
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

from playwright.async_api import async_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evidence_views as ev  # noqa: E402  정적 서버·Checks·브라우저 경로를 그대로 쓴다
import journal_views as jv  # noqa: E402  일지 기능 확인까지 상태로 기다리는 open_app을 쓴다


def fundamentals(symbol, name, currency, financial_currency, *, price, cap, eps, bps, div, rev, assets,
                 fetched_at="2026-10-04T15:30:00+00:00"):
    """/api/stocks/fundamentals 응답 모양(금액은 ÷1e8 스케일). fetched_at=None이면 칸을 뺀다(옛 형식)."""
    d = {"symbol": symbol, "name": name, "currency": currency, "financial_currency": financial_currency,
            "price": price, "chg": 1.23, "cap": cap, "per": 30.1, "pbr": 4.5, "eps": eps, "bps": bps,
            "roe": 15.0, "roa": 8.0, "debt": 45.6, "div": div, "divYield": 0.5, "opMargin": 30.0,
            "revenue": [rev, rev + 10], "op": [rev // 4, rev // 4], "net": [rev // 5, rev // 5],
            "quarters": ["26Q1", "26Q2"], "quarter_ends": ["2026-03-31", "2026-06-30"],
            "assets": assets, "equity": assets // 5,
            "liabilities": assets - assets // 5, "cash": assets // 10, "source": "Yahoo Finance"}
    if fetched_at is not None:
        d["fetched_at"] = fetched_at  # 문자열, 또는 응답할 때 부르는 함수(_ago)
    return d


def _ago(hours):
    """응답할 때마다 '지금으로부터 hours 시간 전' UTC ISO 문자열을 만든다(브라우저와 같은 시계)."""
    return lambda: (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


FUND = {
    "005930.KS": fundamentals("005930.KS", "삼성전자", "KRW", "KRW", price=73400, cap=4378000, eps=4532,
                              bps=50600, div=1416, rev=302300, assets=4263000),  # UTC 15:30 → KST 다음 날 00:30
    "AAPL": fundamentals("AAPL", "Apple Inc.", "USD", "USD", price=189.5, cap=34000, eps=6.57, bps=4.2,
                         div=1.0, rev=950, assets=3500, fetched_at=_ago(1)),       # 6시간 안
    "PKX": fundamentals("PKX", "POSCO Holdings ADR", "USD", "KRW", price=61.25, cap=170, eps=1.1, bps=90.5,
                        div=1.8, rev=180000, assets=1000000, fetched_at=_ago(7)),  # 6시간 지남
    "7203.T": fundamentals("7203.T", "Toyota", "JPY", "JPY", price=2850.0, cap=450000, eps=350.2, bps=2900.0,
                           div=75.0, rev=120000, assets=900000,
                           fetched_at="2026-12-31T15:00:00Z"),                       # 해·날짜가 함께 넘어감
    "XYZ": fundamentals("XYZ", "통화 없는 종목", None, None, price=12.5, cap=500, eps=0.8, bps=5.0,
                        div=None, rev=40, assets=300, fetched_at=None),              # fetched_at 없음
}


class CompanyApi(jv.JournalApi):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.fund_symbols: list[str] = []  # 지표 요청 심볼(차례대로)

    def respond(self, method, path, query, body):
        if path == "/api/stocks/fundamentals":
            self.calls.append((method, path, body))
            sym = parse_qs(query or "").get("symbol", [""])[0]
            self.fund_symbols.append(sym)
            if sym not in FUND:
                return 502, {"detail": "데이터 없음"}
            d = dict(FUND[sym])
            if callable(d.get("fetched_at")):
                d["fetched_at"] = d["fetched_at"]()
            return 200, d
        return super().respond(method, path, query, body)


async def texts(page) -> dict:
    return await page.evaluate("""() => Object.fromEntries(
      ['co-overview', 'co-valuation', 'co-quarterly', 'co-balance', 'co-source', 'co-disclaimer'].map(id =>
        [id, document.getElementById(id)?.innerText || '']))""")


async def show(page, symbol: str, name: str) -> dict:
    if symbol != "005930.KS":
        await page.evaluate(f"selectCompany({symbol!r})")
    # 선택한 종목의 카드가 그려질 때까지 상태로 기다린다(시가총액 카드 sub = 종목명)
    await page.wait_for_function(
        "(n) => (document.getElementById('co-overview')?.innerText || '').includes(n)"
        " && (document.getElementById('co-overview')?.innerText || '').includes('현재가')", arg=name)
    return await texts(page)


async def reopen(page, name: str):
    """다른 뷰로 갔다가 기업 지표 뷰를 다시 연다(loadCompanyDashboard 재호출). 카드가 다시 그려질 때까지 기다린다."""
    await page.evaluate("location.hash = 'company-compare'")
    await page.wait_for_function("document.querySelector('.view.active')?.dataset.view === 'company-compare'")
    await page.evaluate("location.hash = 'company-dashboard'")
    await page.wait_for_function("document.querySelector('.view.active')?.dataset.view === 'company-dashboard'")
    await page.wait_for_function(
        "(n) => (document.getElementById('co-overview')?.innerText || '').includes(n)", arg=name)


async def quarter_titles(page) -> list:
    return await page.evaluate(
        "() => [...document.querySelectorAll('#co-quarterly thead th')].map(th => th.getAttribute('title'))")


async def s_company(browser, base, ck: ev.Checks):
    fake = CompanyApi()
    ctx, page = await jv.open_app(browser, base, fake, hash_="company-dashboard")
    page.fake = fake
    ext0 = len(page.external)  # 첫 로드의 CDN 요청(끊김)은 빼고 본다
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
    src, disc = t["co-source"], t["co-disclaimer"]
    print("[company] 출처 줄·고지")
    ck.ok(src == "출처: Yahoo Finance · 조회 2026-10-05 00:30(KST) · 최대 6시간 지난 값일 수 있음",
          f"출처 줄: UTC 2026-10-04 15:30 → KST 다음 날 00:30 {src!r}")
    ck.ok("지표는 외부 시세 제공처의 값을 그대로 보여 주며 투자 권유가 아닙니다." in disc
          and "투자 판단과 그 결과는 본인에게 있습니다." in disc, f"고지 문구 {disc!r}")
    ck.ok(await page.is_visible("#co-source") and await page.is_visible("#co-disclaimer"),
          "출처 줄·고지가 접히지 않고 보인다")
    ck.ok(await quarter_titles(page) == [None, "분기 끝 2026-03-31", "분기 끝 2026-06-30"],
          f"분기 표 머리 칸 툴팁 = 분기 끝 날짜 {await quarter_titles(page)}")

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
    ck.ok(t["co-source"].startswith("출처: Yahoo Finance · 조회 ") and t["co-source"].endswith(
        "(KST) · 최대 6시간 지난 값일 수 있음"), f"USD: 출처 줄 {t['co-source']!r}")

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
    ck.ok("조회 2027-01-01 00:00(KST)" in t["co-source"],
          f"출처 줄: UTC 2026-12-31 15:00 → KST 2027-01-01 00:00 {t['co-source']!r}")

    t = await show(page, "XYZ", "통화 없는 종목")
    ov, val, qt, bal = t["co-overview"], t["co-valuation"], t["co-quarterly"], t["co-balance"]
    print("[company] 통화 없음 XYZ")
    ck.ok("통화 미확인" in ov, f"null: 카드에 '통화 미확인' {ov[:160]!r}")
    ck.ok("통화 미확인" in qt and "통화 미확인" in bal, "null: 표 머리글에 '통화 미확인'")
    ck.ok("12.50" in ov and "0.05조" in ov, "null: 숫자·스케일은 그대로 보인다")
    ck.ok("원" not in ov + val + qt + bal and "달러" not in ov + val + qt + bal,
          "null: 통화 단위를 지어내지 않는다(원·달러 없음)")
    ck.ok(t["co-source"] == "출처: Yahoo Finance · 조회 시각 확인 불가",
          f"fetched_at 없음: '조회 시각 확인 불가' {t['co-source']!r}")
    ck.ok("투자 권유가 아닙니다" in t["co-disclaimer"], "fetched_at 없음: 고지는 그대로 보인다")

    # 브라우저 캐시: 뷰를 다시 열거나 종목을 다시 고를 때 fetched_at이 6시간 지났거나 없으면 다시 받는다
    print("[company] 브라우저 캐시 6시간")
    fake = page.fake
    for sym, name, expect, why in (
        ("XYZ", "통화 없는 종목", 1, "fetched_at 없음 → 다시 요청"),
        ("PKX", "POSCO Holdings ADR", 1, "7시간 전 → 다시 요청"),
        ("AAPL", "Apple Inc.", 0, "1시간 전 → 요청하지 않음"),
    ):
        if sym != "XYZ":
            await show(page, sym, name)  # 종목을 다시 고름(selectCompany)
        n0 = fake.fund_symbols.count(sym)
        await reopen(page, name)
        n1 = fake.fund_symbols.count(sym)
        ck.ok(n1 - n0 == expect, f"뷰 다시 열기: {sym} {why} (요청 {n1 - n0}회)")
    # 종목을 다시 고를 때도 같은 규칙: 6시간 안인 AAPL로 갔다가 7시간 전 PKX를 다시 고르면 PKX만 다시 받는다
    a0, p0 = fake.fund_symbols.count("AAPL"), fake.fund_symbols.count("PKX")
    await show(page, "PKX", "POSCO Holdings ADR")
    await show(page, "AAPL", "Apple Inc.")
    ck.ok(fake.fund_symbols.count("PKX") - p0 == 1 and fake.fund_symbols.count("AAPL") - a0 == 0,
          f"종목 다시 고르기: 지난 값만 다시 요청 PKX+{fake.fund_symbols.count('PKX') - p0} "
          f"AAPL+{fake.fund_symbols.count('AAPL') - a0}")

    ck.ok(page.external[ext0:] == [], f"지표 화면에서 밖으로 나가는 요청 없음 {page.external[ext0:][:3]}")
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
