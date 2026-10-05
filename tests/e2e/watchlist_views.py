"""브라우저 E2E: 관심종목 패널·별 버튼·연결 버튼(모듈 D spec 3절, 결정 6-2·7-1~7-3·8절, 수용 기준 10·11).

기업 지표 화면(#company-dashboard) 맨 위 관심종목 패널의 화면 상태(불러오는 중·비어 있음·목록·불러오기 실패·로그인 안 됨),
별 버튼(추가·삭제·메모 확인·상한·이미 있음·실패), 줄의 연결 버튼 (a) 지표 보기 (b) 근거 모드로 질문 (c) 판단 기록 개수,
메모 편집·삭제, 고지 문구, 이름·메모의 <script>가 글자로만 보이는지, 밖으로 나가는 요청이 없는지 본다.
근거 모드·일지 각각 404면 (b)·(c) 버튼이 DOM에 없어야 하고, corp_code가 null인 줄도 (b)·(c)가 없다.
앱 서버·DB 없이 돈다. public/을 정적 서버로 띄우고 /api/** 는 가짜 응답(company_views.CompanyApi + 관심종목)을 준다.
외부 CDN 요청은 끊는다. pytest 수집 대상이 아니다(파일명이 test_* 가 아님).
    pip install playwright        # 브라우저가 없으면 playwright install chromium
    python tests/e2e/watchlist_views.py
종료 코드 0 = 모든 확인 통과, 1 = 실패 있음.
"""
import asyncio
import datetime
import json
import os
import re
import sys
from urllib.parse import parse_qs

from playwright.async_api import async_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import company_views as cv  # noqa: E402  지표 가짜 응답(FUND)
import evidence_views as ev  # noqa: E402  정적 서버·Checks·브라우저 경로
import journal_views as jv  # noqa: E402  일지 가짜 API(JournalApi) — 일지 fixtures: e1 00126380(다시 볼 때 됨), e2 00164779

WAIT_MS = 10_000
NOTICE = "관심종목은 내가 고른 목록입니다. 이 서비스는 종목을 추천하지 않습니다."
EMPTY = "관심종목이 없습니다. 종목을 검색해 ☆를 누르면 여기에 모입니다."
LOADING = "관심종목을 불러오는 중…"
FAILED = "관심종목을 불러오지 못했습니다"
CORP = {"005930.KS": "00126380", "000660.KS": "00164779", "035420.KS": "00266961"}
XSS_NAME = "<script>window.__wlx=1</script>악성이름"
XSS_NOTE = "<img src=x onerror=\"window.__wly=1\">메모"
SECRET_NOTE = "비밀메모-보유 300주"
SEARCH = [{"symbol": "AAPL", "name": "Apple Inc.", "exchange": "NMS", "type": "EQUITY"}]


def item(iid, symbol, name, *, corp=None, market=None, note=None, created="2026-10-05T05:31:00+00:00"):
    return {"id": iid, "symbol": symbol, "name": name, "corp_code": corp, "market": market, "note": note,
            "created_at": created}


def rows_default():
    return [item("w1", "005930.KS", "삼성전자", corp="00126380", market="KOSPI", note=SECRET_NOTE),
            item("w2", "000660.KS", "SK하이닉스", corp="00164779", market="KOSPI"),
            item("w3", "035420.KS", "NAVER", corp="00266961", market="KOSPI"),       # 일지 기록 없음
            item("w4", "AAPL", "Apple Inc.", market="NASDAQ"),                         # corp_code null
            item("w5", "005930.KQ", "같은회사 다른줄", corp="00126380", market="KOSDAQ")]  # 같은 corp_code


class WatchlistApi(cv.CompanyApi):
    """지표·일지·근거 모드 가짜 API에 /api/watchlist와 /api/stocks/search를 더한다."""

    def __init__(self, *, items=None, list_status=200, post=None, delete_status=None, patch_status=None,
                 count_status=200, limit=100, **kw):
        super().__init__(**kw)
        self.items = [dict(i) for i in (items if items is not None else [])]
        self.list_status = list_status  # 200 | 401 | 500 | "bad"(모양이 다른 200)
        self.post = post                # 고정 응답 (status, payload) 또는 None
        self.delete_status, self.patch_status = delete_status, patch_status
        self.count_status = count_status
        self.limit = limit
        self.wl_calls: list[tuple[str, str, dict | None]] = []
        self.count_queries: list[dict] = []
        self.count_inflight = self.count_peak = 0  # 관심종목 개수 요청(offset 없는 corp_code 조회) 동시 수·최대

    def respond(self, method, path, query, body):
        if path == "/api/stocks/search":
            self.calls.append((method, path, body))
            return 200, {"results": SEARCH}
        if path == "/api/journal" and method == "GET" and "corp_code=" in (query or ""):
            self.count_queries.append({k: v[0] for k, v in parse_qs(query).items()})
            if self.count_status != 200:
                self.calls.append((method, path, body))
                return self.count_status, {"detail": "일시 오류"}
        if not path.startswith("/api/watchlist"):
            return super().respond(method, path, query, body)
        self.calls.append((method, path, body))
        self.wl_calls.append((method, path, body))
        if path == "/api/watchlist" and method == "GET":
            if self.list_status == "bad":
                return 200, {}
            if self.list_status != 200:
                return self.list_status, {"detail": "오류"}
            return 200, {"items": self.items, "limit": self.limit}
        if path == "/api/watchlist" and method == "POST":
            if self.post:
                return self.post
            sym = body["symbol"].strip().upper()
            hit = next((i for i in self.items if i["symbol"] == sym), None)
            if hit:
                return 409, {"detail": "이미 관심종목에 있습니다.", "code": "duplicate", "item_id": hit["id"]}
            if len(self.items) >= self.limit:
                return 409, {"detail": f"관심종목은 {self.limit}개까지 담을 수 있습니다.", "code": "limit",
                             "limit": self.limit}
            market = "KOSPI" if sym.endswith(".KS") else "KOSDAQ" if sym.endswith(".KQ") else (
                (body.get("exchange") or "")[:16] or None)
            new = item(f"n{len(self.wl_calls)}", sym, body.get("name"), corp=CORP.get(sym), market=market,
                       note=body.get("note") or None,
                       created=datetime.datetime.now(datetime.timezone.utc).isoformat())
            self.items.append(new)
            return 201, new
        m = re.fullmatch(r"/api/watchlist/([^/]+)", path)
        if m:
            hit = next((i for i in self.items if i["id"] == m.group(1)), None)
            if method == "DELETE":
                if self.delete_status:
                    return self.delete_status, {"detail": "오류"}
                if not hit:
                    return 404, {"detail": "관심종목을 찾을 수 없습니다."}
                self.items.remove(hit)
                return 204, None
            if method == "PATCH":
                if self.patch_status:
                    return self.patch_status, {"detail": "오류"}
                if not hit:
                    return 404, {"detail": "관심종목을 찾을 수 없습니다."}
                hit["note"] = body.get("note") or None
                return 200, hit
        return 404, {"detail": "Not Found"}


async def open_wl(browser, base, fake: WatchlistApi, *, hash_="company-dashboard", width=1280, height=900,
                  hold_list: asyncio.Event | None = None, post_delay_s=0.0, hold_counts: asyncio.Event | None = None):
    """jv.open_app과 같은 방식에 204 응답·목록 붙잡기(불러오는 중 상태)·추가 지연·개수 요청 붙잡기를 더한다."""
    ctx = await browser.new_context(viewport={"width": width, "height": height})
    ctx.set_default_timeout(WAIT_MS)
    page = await ctx.new_page()
    page.errors, page.external, page.dialogs = [], [], []
    page.on("pageerror", lambda e: page.errors.append(str(e)))

    async def handle(route):
        url = route.request.url
        if not url.startswith(base):
            page.external.append(url)
            return await route.abort()
        path, _, query = url[len(base):].partition("?")
        if not path.startswith("/api/"):
            return await route.continue_()
        body = None
        if route.request.post_data:
            try:
                body = json.loads(route.request.post_data)
            except ValueError:
                body = None
        method = route.request.method
        if path == "/api/watchlist" and method == "GET" and hold_list is not None:
            await hold_list.wait()
        if path == "/api/watchlist" and method == "POST" and post_delay_s:
            await asyncio.sleep(post_delay_s)
        is_count = path == "/api/journal" and method == "GET" and "corp_code=" in query and "offset=" not in query
        if is_count:
            fake.count_inflight += 1
            fake.count_peak = max(fake.count_peak, fake.count_inflight)
        try:
            if is_count:
                if hold_counts is not None:
                    await hold_counts.wait()
                await asyncio.sleep(0.02)  # 겹침이 보이게 조금씩 붙잡는다
            status, payload = fake.respond(method, path, query, body)
            if status == 204:
                return await route.fulfill(status=204, body="")
            await route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))
        finally:
            if is_count:
                fake.count_inflight -= 1

    await page.route("**/*", handle)
    page.fake = fake
    await page.goto(f"{base}/app.html#{hash_}")
    await page.wait_for_function("!!document.body?.dataset.jrProbe")
    return ctx, page


async def wl_text(page) -> str:
    return await page.evaluate("document.getElementById('wl-card')?.innerText || ''")


async def wait_rows(page, n: int):
    await page.wait_for_function("(n) => document.querySelectorAll('#wl-list .wl-row').length === n", arg=n)


async def wait_links(page):
    """연결 버튼이 확정될 때까지(근거 모드·일지 확인과 개수 요청이 끝나 패널에 표시가 붙는다)."""
    await page.wait_for_function("document.getElementById('wl-card')?.dataset.links === 'ready'")


async def row(page, symbol: str):
    return page.locator(f'#wl-list .wl-row[data-symbol="{symbol}"]')


async def star_text(page) -> str:
    return (await page.locator("#wl-star").inner_text()).strip()


async def wait_star(page, text: str):
    await page.wait_for_function("(t) => (document.getElementById('wl-star')?.innerText || '').trim() === t", arg=text)


async def toast(page) -> str:
    return await page.evaluate("document.getElementById('_toast_el')?.innerText || ''")


async def eventually(page, js: str, arg=None, ms: int = 3000) -> bool:
    """조건이 ms 안에 참이 되면 True(기다림 실패를 예외 대신 확인 실패로 남긴다)."""
    try:
        await page.wait_for_function(js, arg=arg, timeout=ms)
        return True
    except Exception:  # noqa: BLE001
        return False


async def until(cond, ms: int = 3000) -> bool:
    """파이썬 쪽 조건(가짜 API 상태)이 ms 안에 참이 되면 True."""
    for _ in range(ms // 20):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


# ── 시나리오 ─────────────────────────────────────────────────────────────────

async def s_states(browser, base, ck: ev.Checks):
    print("[watchlist] 불러오는 중 → 비어 있음, 고지")
    hold = asyncio.Event()
    fake = WatchlistApi(items=[])
    ctx, page = await open_wl(browser, base, fake, hold_list=hold)
    await page.wait_for_function("(t) => (document.getElementById('wl-card')?.innerText || '').includes(t)",
                                 arg=LOADING)
    ck.ok(LOADING in await wl_text(page), "불러오는 중 문구")
    hold.set()
    await page.wait_for_function("(t) => (document.getElementById('wl-card')?.innerText || '').includes(t)",
                                 arg=EMPTY)
    ck.ok(EMPTY in await wl_text(page) and LOADING not in await wl_text(page), "0건: 비어 있음 안내")
    ck.ok(await page.is_visible("#wl-notice") and (await page.inner_text("#wl-notice")).strip() == NOTICE,
          "관심종목 고지 한 줄이 접히지 않고 보인다")
    # 패널은 종목 선택 카드 위, 별 버튼은 지표 개요 위(결정 7-1)
    order = await page.evaluate("""() => {
      const pos = id => document.getElementById(id).getBoundingClientRect().top;
      return [pos('wl-card'), pos('company-tabs'), pos('wl-star'), pos('co-overview')];
    }""")
    ck.ok(order[0] < order[1] < order[2] <= order[3], f"패널 → 종목 선택 → 별 → 지표 개요 순서 {order}")
    await page.wait_for_function("(document.getElementById('co-overview')?.innerText || '').includes('현재가')")
    ck.ok(await page.is_visible("#wl-star") and await star_text(page) == "☆ 관심종목", "목록에 없는 종목: ☆ 관심종목")
    ck.ok(not page.errors, f"페이지 오류 없음 {page.errors[:2]}")
    await ctx.close()

    print("[watchlist] 불러오기 실패 → 다시 시도")
    fake = WatchlistApi(items=rows_default()[:1], list_status=500)
    ctx, page = await open_wl(browser, base, fake)
    await page.wait_for_function("(t) => (document.getElementById('wl-card')?.innerText || '').includes(t)",
                                 arg=FAILED)
    ck.ok(await page.is_visible("#wl-retry"), "실패: 다시 시도 버튼")
    await page.wait_for_function("(document.getElementById('co-overview')?.innerText || '').includes('현재가')")
    ck.ok(True, "실패해도 지표 화면 나머지는 그대로 동작")
    ck.ok(not await page.is_visible("#wl-star"), "실패: 목록을 모르니 별 버튼을 숨긴다")
    fake.list_status = 200
    await page.click("#wl-retry")
    await wait_rows(page, 1)
    ck.ok(FAILED not in await wl_text(page) and not await page.is_visible("#wl-retry"), "다시 시도 → 목록")
    ck.ok(await star_text(page) == "★ 관심종목", "다시 시도 뒤 현재 종목(005930.KS)이 목록에 있으면 ★")
    await ctx.close()

    print("[watchlist] 모양이 다른 200 → 실패 상태(오류 없음)")
    fake = WatchlistApi(list_status="bad")
    ctx, page = await open_wl(browser, base, fake)
    await page.wait_for_function("(t) => (document.getElementById('wl-card')?.innerText || '').includes(t)",
                                 arg=FAILED)
    ck.ok(not page.errors, f"모양 불일치: 페이지 오류 없음 {page.errors[:2]}")
    await ctx.close()

    print("[watchlist] 로그인 안 됨(401) → 패널·별 숨김")
    fake = WatchlistApi(list_status=401)
    ctx, page = await open_wl(browser, base, fake)
    await page.wait_for_function("document.getElementById('wl-card')?.dataset.state === 'unauthorized'")
    ck.ok(not await page.is_visible("#wl-card") and not await page.is_visible("#wl-star"), "401: 패널·별 숨김")
    await page.wait_for_function("(document.getElementById('co-overview')?.innerText || '').includes('현재가')")
    ck.ok(True, "401: 지표는 그대로 보인다")
    await ctx.close()


async def s_list_links(browser, base, ck: ev.Checks):
    print("[watchlist] 목록 줄·연결 버튼·일지 개수")
    rows = rows_default() + [item("w6", "XSS1", XSS_NAME, market="NYSE", note=XSS_NOTE)]
    fake = WatchlistApi(items=rows, acked=True)
    ctx, page = await open_wl(browser, base, fake)
    ext0 = len(page.external)
    await wait_rows(page, 6)
    await wait_links(page)
    r1 = await (await row(page, "005930.KS")).inner_text()
    ck.ok(all(s in r1 for s in ("삼성전자", "005930.KS", "KOSPI", SECRET_NOTE)), f"줄: 이름·심볼·시장·메모 {r1!r}")
    order = await page.evaluate("[...document.querySelectorAll('#wl-list .wl-row')].map(r => r.dataset.symbol)")
    ck.ok(order == ["005930.KS", "000660.KS", "035420.KS", "AAPL", "005930.KQ", "XSS1"], f"추가한 순서 {order}")

    async def count(symbol, sel):
        return await (await row(page, symbol)).locator(sel).count()

    ck.ok(all([await count(s, ".wl-view") == 1 for s in ("005930.KS", "AAPL", "XSS1")]), "(a) 지표 보기는 항상")
    ck.ok(await count("005930.KS", ".wl-ask") == 1 and await count("000660.KS", ".wl-ask") == 1,
          "(b) 근거 모드 켜짐·corp_code 있음 → 보인다")
    ck.ok(await count("AAPL", ".wl-ask") == 0 and await count("AAPL", ".wl-journal") == 0,
          "corp_code null → (b)·(c) 버튼이 DOM에 없다")
    labels = {s: (await (await row(page, s)).locator(".wl-journal").inner_text()).strip()
              for s in ("005930.KS", "000660.KS", "035420.KS", "005930.KQ")}
    ck.ok(labels == {"005930.KS": "판단 기록 1 · 다시 볼 때 1", "000660.KS": "판단 기록 1 · 다시 볼 때 0",
                     "035420.KS": "판단 기록 0", "005930.KQ": "판단 기록 1 · 다시 볼 때 1"},
          f"(c) 숫자 = corp_code별 total 두 값 {labels}")
    corp_q = [(q.get("corp_code"), q.get("due")) for q in fake.count_queries]
    ck.ok(sorted(corp_q, key=str) == sorted([("00126380", None), ("00126380", "true"), ("00164779", None),
                                            ("00164779", "true"), ("00266961", None), ("00266961", "true")],
                                           key=str),
          f"(c) 같은 corp_code는 한 번만, 기록 수·다시 볼 때 두 번 묻는다 {corp_q}")
    ck.ok(all(q.get("limit") == "1" for q in fake.count_queries), "(c) 개수 요청은 limit=1")

    # 이름·메모의 <script>·onerror는 글자로만 보인다
    x = await (await row(page, "XSS1")).inner_text()
    ck.ok(XSS_NAME in x and XSS_NOTE in x, f"이름·메모가 글자 그대로 보인다 {x!r}")
    ck.ok(await page.evaluate("window.__wlx === undefined && window.__wly === undefined"
                              " && !document.querySelector('#wl-list script, #wl-list img')"),
          "이름·메모의 HTML이 실행·삽입되지 않는다")

    # (c) 누르면 일지 탭이 이 회사로 걸러진다
    jq0 = len(fake.queries)
    await (await row(page, "000660.KS")).locator(".wl-journal").click()
    await page.wait_for_function("document.querySelector('.view.active')?.dataset.view === 'journal'")
    await page.wait_for_function("(document.getElementById('jr-list')?.innerText || '').includes('SK하이닉스')")
    ck.ok(await page.input_value("#jr-f-company") == "00164779", "(c) 일지 회사 거르기 = 이 corp_code")
    last = [q for q in fake.queries[jq0:] if "offset" in q]
    ck.ok(bool(last) and last[-1].get("corp_code") == "00164779" and "due" not in last[-1],
          f"(c) 일지 목록 요청이 corp_code로 걸러진다 {last[-1:] }")
    jr_rows = await page.evaluate("document.querySelectorAll('#jr-list .jr-row').length")
    ck.ok(jr_rows == 1, f"(c) 걸러진 목록 1건 {jr_rows}")

    # (b) 누르면 채팅으로 가서 회사만 고르고 보내지 않는다(메모는 채우지 않는다)
    await page.evaluate("location.hash = 'company-dashboard'")
    await wait_rows(page, 6)
    await wait_links(page)
    await page.evaluate("document.getElementById('chat-input').value = '이전에 쓰던 질문'")  # 채팅 화면은 숨어 있다
    await (await row(page, "005930.KS")).locator(".wl-ask").click()
    await page.wait_for_function("document.querySelector('.view.active')?.dataset.view === 'agent-chat'")
    await page.wait_for_function("document.getElementById('ev-mode')?.checked === true")
    ck.ok(await page.input_value("#ev-company") == "삼성전자", "(b) 근거 모드 회사가 미리 선택된다")
    ck.ok(await page.input_value("#chat-input") == "", "(b) 질문 칸은 비어 있다(이전 글은 지운다)")
    ck.ok(SECRET_NOTE not in await page.input_value("#chat-input"), "(b) 메모를 질문 칸에 채우지 않는다")
    await page.wait_for_timeout(300)
    ck.ok(fake.n("POST", "/api/evidence/chat") == 0 and fake.n("POST", "/api/chat") == 0, "(b) 채팅 전송 요청이 없다")

    # (a) 지표 보기 → 같은 화면에서 그 종목을 고른다
    await page.evaluate("location.hash = 'company-dashboard'")
    await wait_rows(page, 6)
    await (await row(page, "AAPL")).locator(".wl-view").click()
    await page.wait_for_function("(document.getElementById('co-overview')?.innerText || '').includes('Apple Inc.')")
    ck.ok(fake.fund_symbols[-1] == "AAPL", f"(a) 지표 보기 → AAPL 지표 {fake.fund_symbols[-3:]}")
    await wait_star(page, "★ 관심종목")
    ck.ok(True, "(a) 고른 종목이 목록에 있으면 ★")

    ck.ok(page.external[ext0:] == [], f"관심종목 화면에서 밖으로 나가는 요청 없음 {page.external[ext0:][:3]}")
    sent = json.dumps([b for _, _, b in fake.calls if b], ensure_ascii=False)
    ck.ok(SECRET_NOTE not in sent, "메모가 어떤 요청 본문에도 실리지 않는다(관심종목 경로 밖)")
    ck.ok(not page.errors, f"페이지 오류 없음 {page.errors[:2]}")
    await ctx.close()


async def s_flags_off(browser, base, ck: ev.Checks):
    for label, kw, absent, present in (
        ("근거 모드 꺼짐(404)", {"flag": 404}, ".wl-ask", ".wl-journal"),
        ("일지 꺼짐(404)", {"journal": 404}, ".wl-journal", ".wl-ask"),
        ("근거 모드 확인 오류(500)", {"notice_status": 500}, ".wl-ask", ".wl-journal"),
    ):
        print(f"[watchlist] {label}")
        fake = WatchlistApi(items=rows_default()[:2], **kw)
        ctx, page = await open_wl(browser, base, fake)
        await wait_rows(page, 2)
        await wait_links(page)
        n_absent = await page.locator(f"#wl-list {absent}").count()
        n_present = await page.locator(f"#wl-list {present}").count()
        ck.ok(n_absent == 0, f"{label}: {absent} 버튼이 DOM에 없다 ({n_absent})")
        ck.ok(n_present == 2, f"{label}: 다른 기능 버튼은 그대로 ({present} {n_present})")
        if kw.get("journal") == 404:
            ck.ok(not fake.count_queries, "일지 꺼짐: 개수 요청을 하지 않는다")
        ck.ok(not page.errors, f"{label}: 페이지 오류 없음 {page.errors[:2]}")
        await ctx.close()

    print("[watchlist] 개수 조회 실패 → 판단 기록 보기")
    fake = WatchlistApi(items=rows_default()[:1], count_status=500)
    ctx, page = await open_wl(browser, base, fake)
    await wait_rows(page, 1)
    await wait_links(page)
    lab = (await (await row(page, "005930.KS")).locator(".wl-journal").inner_text()).strip()
    ck.ok(lab == "판단 기록 보기", f"개수 조회 실패: 숫자 없이 '판단 기록 보기' {lab!r}")
    await ctx.close()


async def s_star(browser, base, ck: ev.Checks):
    print("[watchlist] 별 버튼: 추가·삭제(목업 탭은 exchange 없이)")
    fake = WatchlistApi(items=[])
    ctx, page = await open_wl(browser, base, fake, post_delay_s=0.4)
    await page.wait_for_function("(t) => (document.getElementById('wl-card')?.innerText || '').includes(t)",
                                 arg=EMPTY)
    await wait_star(page, "☆ 관심종목")
    await page.click("#wl-star")
    ck.ok(await page.is_disabled("#wl-star"), "요청 중에는 별 버튼이 비활성")
    await wait_star(page, "★ 관심종목")
    posts = [b for m, p, b in fake.wl_calls if m == "POST"]
    ck.ok(posts == [{"symbol": "005930.KS", "name": "삼성전자"}], f"추가 본문: 목업 탭은 exchange 없이 {posts}")
    await wait_rows(page, 1)
    ck.ok(await page.get_attribute("#wl-star", "aria-pressed") == "true", "★ aria-pressed=true")
    await page.click("#wl-star")  # 메모 없음 → 확인 없이 삭제
    await wait_star(page, "☆ 관심종목")
    await wait_rows(page, 0)
    ck.ok([m for m, _, _ in fake.wl_calls].count("DELETE") == 1 and not page.dialogs, "★ 누름(메모 없음) → 확인 없이 삭제")

    print("[watchlist] 별 버튼: 검색 결과 종목은 exchange를 함께 보낸다")
    await page.click("#company-search-btn")
    await page.fill("#ssm-query", "apple")
    await page.wait_for_selector('#ssm-results .ssm-item[data-symbol="AAPL"]')
    await page.click('#ssm-results .ssm-item[data-symbol="AAPL"]')
    await page.wait_for_function("(document.getElementById('co-overview')?.innerText || '').includes('Apple Inc.')")
    await wait_star(page, "☆ 관심종목")
    await page.click("#wl-star")
    await wait_star(page, "★ 관심종목")
    posts = [b for m, p, b in fake.wl_calls if m == "POST"]
    ck.ok(posts[-1] == {"symbol": "AAPL", "name": "Apple Inc.", "exchange": "NMS"}, f"검색 종목 본문 {posts[-1]}")
    r = await (await row(page, "AAPL")).inner_text()
    ck.ok("NMS" in r, f"새 줄에 시장(거래소) 표시 {r!r}")
    ck.ok(not page.errors, f"페이지 오류 없음 {page.errors[:2]}")
    await ctx.close()

    print("[watchlist] 별 버튼: 메모 있는 항목 삭제는 확인")
    fake = WatchlistApi(items=rows_default()[:1])
    ctx, page = await open_wl(browser, base, fake)
    await wait_rows(page, 1)
    await wait_star(page, "★ 관심종목")
    page.once("dialog", lambda d: (page.dialogs.append(d.message), asyncio.ensure_future(d.dismiss())))
    await page.click("#wl-star")
    await page.wait_for_timeout(300)
    ck.ok(page.dialogs and "메모도 함께 지워집니다" in page.dialogs[-1], f"확인 문구 {page.dialogs}")
    ck.ok(not [1 for m, _, _ in fake.wl_calls if m == "DELETE"] and await star_text(page) == "★ 관심종목",
          "확인 취소 → 지우지 않는다")
    page.once("dialog", lambda d: (page.dialogs.append(d.message), asyncio.ensure_future(d.accept())))
    await page.click("#wl-star")
    await wait_star(page, "☆ 관심종목")
    await wait_rows(page, 0)
    ck.ok(len(page.dialogs) == 2, "확인 → 지운다")
    await ctx.close()

    print("[watchlist] 별 버튼: 상한·이미 있음·실패")
    fake = WatchlistApi(items=[item("w9", "MSFT", "Microsoft")], limit=1)
    ctx, page = await open_wl(browser, base, fake)
    await wait_rows(page, 1)
    await wait_star(page, "☆ 관심종목")
    ck.ok((await page.inner_text("#wl-count")).strip() == "1 / 1", f"개수 표시는 서버 limit을 쓴다 "
          f"{(await page.inner_text('#wl-count')).strip()!r}")
    await page.click("#wl-star")
    await page.wait_for_function("(document.getElementById('_toast_el')?.innerText || '').includes('개까지')")
    ck.ok(await toast(page) == "관심종목은 1개까지 담을 수 있습니다", f"상한 토스트는 서버 limit {await toast(page)!r}")
    ck.ok(await star_text(page) == "☆ 관심종목" and not await page.is_disabled("#wl-star"), "상한: ☆로 돌아온다")
    # 다른 탭에서 먼저 추가한 경우: 409 duplicate → 조용히 ★
    fake.limit = 100
    fake.items.append(item("w10", "005930.KS", "삼성전자", corp="00126380", market="KOSPI"))
    await page.evaluate("document.getElementById('_toast_el')?.remove()")
    await page.click("#wl-star")
    await wait_star(page, "★ 관심종목")
    await wait_rows(page, 2)
    ck.ok(await toast(page) == "", f"이미 있음: 토스트 없이 ★ {await toast(page)!r}")
    # 실패: 원래 상태로 돌리고 토스트
    fake.post = (500, {"detail": "관심종목을 저장하지 못했습니다."})
    await page.evaluate("selectCompany('AAPL')")
    await page.wait_for_function("(document.getElementById('co-overview')?.innerText || '').includes('Apple Inc.')")
    await wait_star(page, "☆ 관심종목")
    await page.click("#wl-star")
    await page.wait_for_function("!!document.getElementById('_toast_el')")
    ck.ok(await star_text(page) == "☆ 관심종목", "추가 실패: ☆로 돌아온다")
    fake.delete_status = 500
    await page.evaluate("selectCompany('005930.KS')")
    await wait_star(page, "★ 관심종목")
    await page.evaluate("document.getElementById('_toast_el')?.remove()")
    await page.click("#wl-star")
    await page.wait_for_function("!!document.getElementById('_toast_el')")
    ck.ok(await star_text(page) == "★ 관심종목", "삭제 실패: ★로 돌아온다")
    await wait_rows(page, 2)
    ck.ok(not page.errors, f"페이지 오류 없음 {page.errors[:2]}")
    await ctx.close()


async def s_memo(browser, base, ck: ev.Checks):
    print("[watchlist] 메모 편집·삭제 버튼")
    fake = WatchlistApi(items=rows_default()[:2])
    ctx, page = await open_wl(browser, base, fake)
    await wait_rows(page, 2)
    r = await row(page, "005930.KS")
    await r.locator(".wl-note-edit").click()
    ck.ok(await r.locator(".wl-note-input").input_value() == SECRET_NOTE, "메모 편집: 지금 메모가 채워져 있다")
    ck.ok(await r.locator(".wl-note-input").get_attribute("maxlength") == "200", "메모 입력은 200자까지")
    await r.locator(".wl-note-input").fill("새 메모 <b>굵게</b>")
    await r.locator(".wl-note-save").click()
    await page.wait_for_function(
        "(document.querySelector('#wl-list .wl-row[data-symbol=\"005930.KS\"]')?.innerText || '').includes('새 메모')")
    patches = [(p, b) for m, p, b in fake.wl_calls if m == "PATCH"]
    ck.ok(patches == [("/api/watchlist/w1", {"note": "새 메모 <b>굵게</b>"})], f"PATCH 본문 {patches}")
    ck.ok("새 메모 <b>굵게</b>" in await r.inner_text(), "고친 메모가 글자 그대로 보인다")
    ck.ok(await r.locator(".wl-note-input").count() == 0, "저장 뒤 편집 칸이 닫힌다")
    await r.locator(".wl-note-edit").click()
    await r.locator(".wl-note-cancel").click()
    ck.ok(await r.locator(".wl-note-input").count() == 0 and len(patches) == 1, "취소 → 요청 없음")
    fake.patch_status = 500
    await r.locator(".wl-note-edit").click()
    await r.locator(".wl-note-input").fill("실패할 메모")
    await r.locator(".wl-note-save").click()
    await page.wait_for_function("!!document.getElementById('_toast_el')")
    ck.ok(await r.locator(".wl-note-input").input_value() == "실패할 메모", "메모 저장 실패: 입력은 그대로 있다")
    await (await row(page, "000660.KS")).locator(".wl-delete").click()  # 메모 없음 → 확인 없이
    await wait_rows(page, 1)
    ck.ok([p for m, p, _ in fake.wl_calls if m == "DELETE"] == ["/api/watchlist/w2"], "삭제 버튼 → DELETE")
    ck.ok(not page.errors, f"페이지 오류 없음 {page.errors[:2]}")
    await ctx.close()


async def s_count_dedupe_focus(browser, base, ck: ev.Checks):
    print("[watchlist] 개수 요청 중 별 추가: corp_code별 중복 없음·전체 동시 4개·편집 중 메모 칸 유지")
    hold = asyncio.Event()
    fake = WatchlistApi(items=[item("w2", "000660.KS", "SK하이닉스", corp="00164779", market="KOSPI"),
                               item("w3", "035420.KS", "NAVER", corp="00266961", market="KOSPI")], acked=True)
    ctx, page = await open_wl(browser, base, fake, hold_counts=hold)
    await wait_rows(page, 2)
    ck.ok(await until(lambda: fake.count_inflight == 4), f"첫 개수 요청 4개가 붙잡혀 있다 ({fake.count_inflight})")
    await wait_star(page, "☆ 관심종목")
    await page.click("#wl-star")  # 현재 종목 005930.KS 추가 → 개수를 다시 채운다
    await wait_rows(page, 3)
    r = await row(page, "000660.KS")
    await r.locator(".wl-note-edit").click()
    await r.locator(".wl-note-input").fill("편집 중 메모")
    await page.evaluate("""() => {
      const i = document.querySelector('#wl-list .wl-row[data-symbol="000660.KS"] .wl-note-input');
      i.focus(); i.setSelectionRange(3, 3); window.__wlInput = i;
    }""")
    hold.set()
    ok = await eventually(page, "(document.querySelector('#wl-list .wl-row[data-symbol=\"005930.KS\"] .wl-journal')"
                                "?.innerText || '').trim() === '판단 기록 1 · 다시 볼 때 1'")
    await wait_links(page)
    await page.wait_for_timeout(200)
    ck.ok(ok, "추가한 줄의 개수도 채워진다")
    per = {}
    for q in fake.count_queries:
        if "offset" not in q:
            per[q["corp_code"]] = per.get(q["corp_code"], 0) + 1
    ck.ok(per and all(n <= 2 for n in per.values()), f"corp_code당 개수 요청은 2회를 넘지 않는다 {per}")
    ck.ok(fake.count_peak <= 4, f"개수 요청 동시 실행은 전체에서 4개까지 ({fake.count_peak})")
    focus = await page.evaluate("""() => {
      const a = document.activeElement;
      return { same: a === window.__wlInput, connected: !!window.__wlInput?.isConnected,
               value: a?.value ?? null, caret: a?.selectionStart ?? null };
    }""")
    ck.ok(focus == {"same": True, "connected": True, "value": "편집 중 메모", "caret": 3},
          f"개수 도착 뒤에도 같은 메모 입력 칸에 포커스·커서가 남는다 {focus}")
    ck.ok(not page.errors, f"페이지 오류 없음 {page.errors[:2]}")
    await ctx.close()

    print("[watchlist] 개수 요청 401 → 로그인 안 됨")
    fake = WatchlistApi(items=rows_default()[:1], count_status=401)
    ctx, page = await open_wl(browser, base, fake)
    ok = await eventually(page, "document.getElementById('wl-card')?.dataset.state === 'unauthorized'")
    ck.ok(ok and not await page.is_visible("#wl-card") and not await page.is_visible("#wl-star"),
          "개수 401: 패널·별을 숨긴다(다른 경로와 같다)")
    ck.ok(not page.errors, f"페이지 오류 없음 {page.errors[:2]}")
    await ctx.close()

    print("[watchlist] 409 duplicate 뒤 목록 재조회 실패 → 그래도 ★")
    fake = WatchlistApi(items=[item("w2", "000660.KS", "SK하이닉스", corp="00164779", market="KOSPI")])
    ctx, page = await open_wl(browser, base, fake)
    await wait_rows(page, 1)
    await wait_star(page, "☆ 관심종목")
    fake.items.append(item("w10", "005930.KS", "삼성전자", corp="00126380", market="KOSPI", note=SECRET_NOTE))
    fake.list_status = 500
    await page.click("#wl-star")
    ok = await eventually(page, "(document.getElementById('wl-star')?.innerText || '').trim() === '★ 관심종목'")
    ck.ok(ok and await toast(page) == "", f"재조회 실패해도 토스트 없이 ★ {await star_text(page)!r}")
    ck.ok(await (await row(page, "005930.KS")).count() == 1, "응답 item_id로 줄이 생긴다")
    # 메모를 모르는 줄: 지울 때 확인한다(서버에 메모가 있을 수 있다)
    page.once("dialog", lambda d: (page.dialogs.append(d.message), asyncio.ensure_future(d.dismiss())))
    await page.click("#wl-star")
    await page.wait_for_timeout(300)
    ck.ok(bool(page.dialogs) and not [1 for m, _, _ in fake.wl_calls if m == "DELETE"],
          f"메모를 모르는 줄 삭제는 확인을 받는다 {page.dialogs}")
    page.once("dialog", lambda d: (page.dialogs.append(d.message), asyncio.ensure_future(d.accept())))
    await page.click("#wl-star")
    await wait_star(page, "☆ 관심종목")
    ck.ok([p for m, p, _ in fake.wl_calls if m == "DELETE"] == ["/api/watchlist/w10"], "확인 → item_id로 DELETE")
    ck.ok(not page.errors, f"페이지 오류 없음 {page.errors[:2]}")
    await ctx.close()


async def s_journal_company_name(browser, base, ck: ev.Checks):
    print("[watchlist] (c) 일지 회사 거르기 이름: 일지가 아는 이름 · 기록 0건 회사는 남기지 않는다")
    rows = [item("w1", "005930.KS", "Samsung Electronics", corp="00126380", market="KOSPI"),
            item("w3", "035420.KS", "NAVER", corp="00266961", market="KOSPI")]
    fake = WatchlistApi(items=rows, acked=True)
    ctx, page = await open_wl(browser, base, fake)
    await wait_rows(page, 2)
    await wait_links(page)
    await (await row(page, "005930.KS")).locator(".wl-journal").click()
    await page.wait_for_function("document.querySelector('.view.active')?.dataset.view === 'journal'")
    await page.wait_for_function("document.querySelectorAll('#jr-list .jr-row').length === 1")
    sel = await page.evaluate("(() => { const s = document.getElementById('jr-f-company');"
                              " return [s.value, s.selectedOptions[0]?.textContent]; })()")
    ck.ok(sel == ["00126380", "삼성전자"], f"일지 목록이 아는 회사 이름을 쓴다(관심종목 이름 아님) {sel}")
    await page.evaluate("location.hash = 'company-dashboard'")
    await wait_rows(page, 2)
    await wait_links(page)
    await (await row(page, "035420.KS")).locator(".wl-journal").click()
    await page.wait_for_function("document.querySelector('.view.active')?.dataset.view === 'journal'")
    await page.wait_for_function("document.getElementById('jr-f-company')?.value === '00266961'")
    await page.wait_for_timeout(300)
    ck.ok(await page.evaluate("document.getElementById('jr-f-company').selectedOptions[0]?.textContent") == "NAVER",
          "기록 0건 회사도 거르는 동안은 선택 상자에 보인다")
    await page.select_option("#jr-f-company", "")
    await page.wait_for_function("document.querySelectorAll('#jr-list .jr-row').length >= 2")
    opts = await page.evaluate("[...document.querySelectorAll('#jr-f-company option')].map(o => o.value)")
    ck.ok("00266961" not in opts and "00126380" in opts, f"거르기를 풀면 기록 0건 회사는 상자에서 빠진다 {opts}")
    ck.ok(not page.errors, f"페이지 오류 없음 {page.errors[:2]}")
    await ctx.close()


async def s_map_limit(browser, base, ck: ev.Checks):
    print("[watchlist] 동시 요청 4개 제한(createLimiter)")
    fake = WatchlistApi(items=[])
    ctx, page = await open_wl(browser, base, fake)
    got = await page.evaluate("""async () => {
      const { createLimiter } = await import('/js/watchlist.js');
      const run = createLimiter(4);
      let now = 0, peak = 0;
      const job = async x => {
        now++; peak = Math.max(peak, now);
        await new Promise(r => setTimeout(r, 20 + (x % 3) * 10));
        now--;
        if (x === 5) throw new Error("x");
        return x * 2;
      };
      // 두 묶음을 따로 넣어도 상한은 하나다
      const a = Promise.allSettled([...Array(6).keys()].map(x => run(() => job(x))));
      const b = Promise.allSettled([6, 7, 8, 9, 10].map(x => run(() => job(x))));
      const out = [...await a, ...await b].map(r => r.status === "fulfilled" ? r.value : "err");
      return { peak, out };
    }""")
    ck.ok(got["peak"] == 4, f"createLimiter: 두 묶음 합쳐 동시 실행 최대 4 ({got['peak']})")
    ck.ok(got["out"] == [0, 2, 4, 6, 8, "err", 12, 14, 16, 18, 20], f"createLimiter: 결과·실패 전달 {got['out']}")
    exported = await page.evaluate("import('/js/company.js').then(m => 'getSelectedCompany' in m)")
    ck.ok(exported is False, "company.js: 쓰이지 않는 getSelectedCompany를 내보내지 않는다")
    await ctx.close()


async def main() -> int:
    srv, base = ev._serve()
    ck = ev.Checks()
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=ev._chromium())
        for scenario in (s_states, s_list_links, s_flags_off, s_star, s_memo, s_count_dedupe_focus,
                         s_journal_company_name, s_map_limit):
            try:
                await scenario(browser, base, ck)
            except Exception as exc:  # noqa: BLE001 — 한 시나리오가 죽어도 나머지를 본다
                ck.ok(False, f"{scenario.__name__}: 예외 {type(exc).__name__}: {str(exc)[:300]}")
        await browser.close()
    srv.shutdown()
    print(f"checks passed={ck.passed} failed={len(ck.failures)}")
    return 1 if ck.failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
