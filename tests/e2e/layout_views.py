"""브라우저 E2E: 상단 GNB가 모바일에서 문서 폭을 넘기지 않는지(가로 스크롤 없음) 확인한다.

375·768·1280px × 상담 화면(#agent-chat)·판단 일지(#journal)·기업 지표(#company-dashboard)에서
documentElement.scrollWidth == clientWidth. 375px에서는 GNB 탭 줄이 보이면서(폭 > 0) 가로로 스크롤되는지,
더보기 패널이 화면 안에 열리는지, 관심종목 줄의 연결 버튼이 줄바꿈되어 화면 안에 보이는지도 본다(모듈 D P2).
앱 서버·DB 없이 돈다. public/을 정적 서버로 띄우고 /api/** 는 가짜 응답(journal_views.JournalApi)을 준다.
외부 CDN 요청은 끊는다. 시세(/api/stocks/market)는 로컬처럼 지수 4개를 채워 우측 영역 폭을 실제와 맞춘다.
pytest 수집 대상이 아니다(파일명이 test_* 가 아님). evidence_views.py와 같은 수동 실행 스크립트다.
    pip install playwright        # 브라우저가 없으면 playwright install chromium
    python tests/e2e/layout_views.py
    # 375px 화면 저장: LAYOUT_SCREENSHOT_DIR=/tmp/shots python tests/e2e/layout_views.py
종료 코드 0 = 모든 확인 통과, 1 = 실패 있음.
"""
import asyncio
import os
import sys

from playwright.async_api import async_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evidence_views as ev  # noqa: E402  정적 서버·Checks·브라우저 경로를 그대로 쓴다
import journal_views as jv  # noqa: E402  일지 기능 확인까지 상태로 기다리는 open_app을 쓴다
import company_views as cv  # noqa: E402  지표 가짜 응답(FUND)

WIDTHS = (375, 768, 1280)
HASHES = ("agent-chat", "journal", "company-dashboard")
# 관심종목 줄: 긴 이름·메모와 corp_code가 있어 연결 버튼 다섯 개가 모두 그려진다(일지 fixtures e1과 같은 corp_code)
WATCHLIST = [{"id": "w1", "symbol": "005930.KS", "name": "삼성전자 보통주 아주 긴 표시 이름 테스트용 문자열",
              "corp_code": "00126380", "market": "KOSPI", "note": "HBM 고객 다변화 확인 — 다음 분기 실적 발표 뒤 다시 보기",
              "created_at": "2026-10-05T05:31:00+00:00"},
             {"id": "w2", "symbol": "AAPL", "name": "Apple Inc.", "corp_code": None, "market": "NASDAQ",
              "note": None, "created_at": "2026-10-05T05:40:12+00:00"}]
INDICES = [{"name": "KOSPI", "price": 2734.56, "change_pct": 0.42},
           {"name": "KOSDAQ", "price": 868.12, "change_pct": -0.37},
           {"name": "S&P 500", "price": 5612.34, "change_pct": 0.18},
           {"name": "NASDAQ", "price": 17890.45, "change_pct": -0.21}]


class LayoutApi(jv.JournalApi):
    def respond(self, method, path, query, body):
        if path == "/api/stocks/market":
            self.calls.append((method, path, body))
            return 200, {"indices": INDICES}
        if path == "/api/watchlist":
            self.calls.append((method, path, body))
            return 200, {"items": WATCHLIST, "limit": 100}
        if path == "/api/stocks/fundamentals":
            self.calls.append((method, path, body))
            return 200, cv.FUND["005930.KS"]
        return super().respond(method, path, query, body)


async def overflow(page) -> dict:
    return await page.evaluate("""() => {
      const de = document.documentElement, tabs = document.querySelector('.gnb-tabs');
      return {sw: de.scrollWidth, cw: de.clientWidth,
              tabs_cw: tabs.clientWidth, tabs_sw: tabs.scrollWidth};
    }""")


async def s_view(browser, base, ck: ev.Checks, width: int, hash_: str):
    tag = f"{width}px #{hash_}"
    print(f"[layout] {tag}")
    ctx, page = await jv.open_app(browser, base, LayoutApi(), hash_=hash_, width=width, height=740)
    # 시세가 그려진 뒤에 잰다(우측 영역이 가장 넓은 상태)
    await page.wait_for_function("document.querySelectorAll('#market-ticker .ticker-item').length > 0")
    if hash_ == "journal":
        await page.wait_for_function("document.querySelector('.view.active')?.dataset.view === 'journal'")
    if hash_ == "company-dashboard":
        # 관심종목 줄과 연결 버튼(일지 개수까지), 지표 카드가 그려진 뒤에 잰다
        await page.wait_for_function("document.getElementById('wl-card')?.dataset.links === 'ready'"
                                     " && document.querySelectorAll('#wl-list .wl-row').length === 2"
                                     " && (document.getElementById('co-overview')?.innerText || '').includes('현재가')")
        btns = await page.evaluate("""() => [...document.querySelectorAll('#wl-list .wl-row')[0]
            .querySelectorAll('.wl-links button')].map(b => { const r = b.getBoundingClientRect();
              return {t: b.innerText.trim(), left: r.left, right: r.right, top: Math.round(r.top), w: r.width}; })""")
        ck.ok([b["t"] for b in btns] == ["지표 보기", "근거 모드로 질문", "판단 기록 1 · 다시 볼 때 1", "메모", "삭제"],
              f"{tag}: 관심종목 줄 연결 버튼 다섯 개 {[b['t'] for b in btns]}")
        vw = await page.evaluate("window.innerWidth")
        ck.ok(all(b["w"] > 0 and b["left"] >= 0 and b["right"] <= vw for b in btns),
              f"{tag}: 연결 버튼이 화면 안에 보인다 {[(round(b['left']), round(b['right'])) for b in btns]} / {vw}")
        if width == 375:
            ck.ok(len({b["top"] for b in btns}) > 1, f"{tag}: 연결 버튼이 줄바꿈된다 tops={[b['top'] for b in btns]}")
    m = await overflow(page)
    ck.ok(m["sw"] == m["cw"], f"{tag}: 가로 스크롤 없음 scrollWidth={m['sw']} clientWidth={m['cw']}")
    if width == 375:
        ck.ok(m["tabs_cw"] >= 120, f"{tag}: GNB 탭 줄이 보인다(폭 {m['tabs_cw']}px)")
        ck.ok(m["tabs_sw"] > m["tabs_cw"], f"{tag}: GNB 탭 줄이 가로로 스크롤된다 {m['tabs_sw']}>{m['tabs_cw']}")
        before = await page.evaluate("document.querySelector('.gnb-tabs').scrollLeft")
        await page.evaluate("document.querySelector('.gnb-tabs').scrollLeft = 10000")
        after = await page.evaluate("document.querySelector('.gnb-tabs').scrollLeft")
        ck.ok(after > before, f"{tag}: 탭 줄을 끝까지 밀 수 있다 scrollLeft {before}→{after}")
        await page.evaluate("document.querySelector('.gnb-tabs').scrollLeft = 0")
        shot = os.environ.get("LAYOUT_SCREENSHOT_DIR")
        if shot:
            os.makedirs(shot, exist_ok=True)
            await page.screenshot(path=os.path.join(shot, f"375_{hash_}.png"))
        # 글자(.gnb-label)를 숨겨도 버튼의 접근 가능한 이름은 남아야 한다
        ck.ok(await page.get_by_role("button", name="로그아웃", exact=True).is_visible(),
              f"{tag}: 로그아웃 버튼의 접근 가능한 이름이 '로그아웃'이다")
        ck.ok(await page.is_visible("#gnb-more-btn"), f"{tag}: 더보기 버튼이 보인다")
        btn = await page.evaluate("(() => { const r = document.querySelector('#gnb-more-btn').getBoundingClientRect();"
                                  " return [r.left, r.right, window.innerWidth]; })()")
        ck.ok(btn[0] >= 0 and btn[1] <= btn[2], f"{tag}: 더보기 버튼이 화면 안에 있다 {btn}")
        await page.click("#gnb-more-btn")
        await page.wait_for_selector("#gnb-offcanvas.open")
        # right 전환(0.22s)이 끝날 때까지 상태로 기다린다
        await page.wait_for_function("document.querySelector('#gnb-offcanvas').getBoundingClientRect().right"
                                     " <= window.innerWidth + 0.5")
        r = await page.evaluate("(() => { const r = document.querySelector('#gnb-offcanvas').getBoundingClientRect();"
                                " return [r.left, r.right, window.innerWidth]; })()")
        ck.ok(r[0] >= 0 and r[1] <= r[2] + 0.5, f"{tag}: 더보기 패널이 화면 안에 열린다 {r}")
        m2 = await overflow(page)
        ck.ok(m2["sw"] == m2["cw"], f"{tag}: 더보기 연 뒤에도 가로 스크롤 없음 {m2['sw']}/{m2['cw']}")
        if shot:
            await page.screenshot(path=os.path.join(shot, f"375_{hash_}_more.png"))
    ck.ok(not page.errors, f"{tag}: 페이지 오류 없음 {page.errors[:2]}")
    await ctx.close()


async def main() -> int:
    srv, base = ev._serve()
    ck = ev.Checks()
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=ev._chromium())
        for width in WIDTHS:
            for hash_ in HASHES:
                try:
                    await s_view(browser, base, ck, width, hash_)
                except Exception as exc:  # noqa: BLE001 — 한 조합이 죽어도 나머지를 본다
                    ck.ok(False, f"{width}px #{hash_}: 예외 {type(exc).__name__}: {str(exc)[:300]}")
        await browser.close()
    srv.shutdown()
    print(f"checks passed={ck.passed} failed={len(ck.failures)}")
    return 1 if ck.failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
