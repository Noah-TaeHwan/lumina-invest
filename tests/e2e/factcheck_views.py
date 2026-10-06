"""브라우저 E2E: 공시 팩트체커 화면(public/factcheck.html, 설계 T3)을 가짜 API 응답으로 상태별 확인한다.

앱 서버·DB·Qdrant·JEV 없이 돈다. public/을 정적 서버로 띄우고 /api/** 는 page.route로 가짜 응답을 준다(evidence_views.py와
같은 방식). 외부 요청은 모두 끊고, 끊긴 요청이 있으면 실패로 센다(화면은 외부 CSS·폰트를 쓰지 않는다).
pytest 수집 대상이 아니다(파일명이 test_* 가 아님).
    pip install playwright        # 브라우저가 없으면 playwright install chromium
    python tests/e2e/factcheck_views.py
종료 코드 0 = 모든 확인 통과, 1 = 실패 있음. 실패 기록은 evidence_views.py와 같은 tests/e2e/_artifacts/(gitignore).

시나리오: 첫 검수(점진 표시·⚠️❔ 먼저·근거 펼치기·XBRL·textContent), 입력 초과 안내(2,000자·30문장), 한도 소진(3회·전체),
건너뛴 문장 펼치기·수동 검수, 만료(404), 모바일 375px 가로 넘침 없음.
"""
import asyncio
import json
import os
import re
import sys
import traceback

from playwright.async_api import async_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evidence_views as ev  # noqa: E402  정적 서버·Checks·브라우저 경로·실패 기록

WAIT_MS = ev.WAIT_MS
JOB = "J" * 43
XSS = "<img src=x onerror=\"window.__fcx=1\">태그는 글자로"
LONG = "아주긴단어" * 40  # 띄어쓰기 없는 긴 문자열(모바일 줄바꿈 확인)
COMPANIES = {"companies": [{"corp_code": "00126380", "corp_name": "삼성전자", "stock_code": "005930"},
                           {"corp_code": "00164779", "corp_name": "SK하이닉스", "stock_code": "000660"}],
             "scope": "데모 범위: 삼성전자·SK하이닉스 최근 3년 정기보고서(사업·반기·분기)·잠정실적·XBRL 재무 수치",
             "weaknesses": ["주어가 바뀐 문장(다른 부문·제품·고객사 이름)을 잘 못 거릅니다.",
                            "증감률·영업이익률 밖의 파생 지표와 명시하지 않은 기간은 검수하지 않거나 범위 밖으로 표시합니다.",
                            "✅는 검색된 공시와 일치한다는 뜻이고, 사실 보증·발행 승인이 아닙니다."],
             "max_chars": 2000, "max_targets": 30}
DRAFT = ("삼성전자의 2026년 상반기 매출은 153조원이다. 2025년 영업이익은 50조원이다. HBM 매출은 줄었다. "
         "앞으로도 좋을까? 2025년 연구개발비는 35조원이다.")
EVID = {"rcept_no": "20260814000123", "report_nm": "반기보고서 (2026.06)", "period": "2026H1",
        "section": "II. 사업의 내용", "text": "회사는 메모리 반도체를 생산한다."}


def res(idx, text, status, *, category="checked", evidence=None, xbrl=None, reason=None):
    return {"idx": idx, "text": text, "category": category, "status": status,
            "evidence": evidence if evidence is not None else ([] if status == "skipped" else [EVID]),
            "xbrl": xbrl, "reason": reason}


SENTS = ["삼성전자의 2026년 상반기 매출은 153조원이다.", "2025년 영업이익은 50조원이다.", "HBM 매출은 줄었다.",
         "앞으로도 좋을까?", "2025년 연구개발비는 35조원이다."]
R0 = res(0, SENTS[0], "supported", xbrl={"account_nm": "매출액", "period": "2026H1", "fs_div": "CFS",
                                          "amount": 153000000000000})
R1 = res(1, SENTS[1], "contradicted", reason="숫자는 XBRL과 다름",
         evidence=[{**EVID, "text": XSS, "report_nm": "사업보고서 (2025.12)", "period": "2025"}],
         xbrl={"account_nm": "영업이익", "period": "2025", "fs_div": "CFS", "amount": 43601100000000})
R2 = res(2, SENTS[2], "no_evidence")
R3 = res(3, SENTS[3], "skipped", category="opinion", reason="의견·전망 문장")
R4 = res(4, SENTS[4], "supported")
ALL = [R0, R1, R2, R3, R4]


def counts(rs):
    out = {k: 0 for k in ("supported", "contradicted", "no_evidence", "unjudged", "skipped")}
    for r in rs:
        out[r["status"]] += 1
    return out


def job(status, rs, *, poll_ms=400, error=None):
    return {"job_id": JOB, "status": status, "corp_code": "00126380", "source": "ai_answer", "total": 5,
            "targets": 4, "results": rs, "counts": counts(rs), "error": error,
            "poll_interval_ms": poll_ms if status == "running" else None, "expires_in_s": 900}


STARTED = (202, {"job_id": JOB, "status": "running", "total": 5, "targets": 4, "poll_interval_ms": 400,
                 "expires_in_s": 900})


class FakeFc:
    """시나리오별 가짜 팩트체커 API. polls는 GET마다 하나씩 꺼내는 응답(마지막은 계속 준다)."""

    def __init__(self, *, post=STARTED, polls=None, recheck=(202, {"job_id": JOB, "status": "running"}),
                 after_recheck=None):
        self.post, self.polls = post, list(polls or [(200, job("done", ALL))])
        self.recheck, self.after_recheck = recheck, after_recheck
        self.calls: list[tuple[str, str, dict | None]] = []

    def respond(self, method, path, query, body):
        self.calls.append((method, path, body))
        if path == "/api/factcheck/companies":
            return 200, COMPANIES
        if path == "/api/factcheck" and method == "POST":
            return self.post
        if re.fullmatch(r"/api/factcheck/[^/]+/recheck/\d+", path) and method == "POST":
            if self.after_recheck is not None:
                self.polls = list(self.after_recheck)
            return self.recheck
        if re.fullmatch(r"/api/factcheck/[^/]+", path) and method == "GET":
            return self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
        return 404, {"detail": "Not Found"}

    def posted(self, path="/api/factcheck"):
        return [b for m, p, b in self.calls if m == "POST" and p == path]


async def open_fc(browser, base, fake: FakeFc, *, width=1280, height=900):
    """factcheck.html을 열고 가짜 API를 붙인다(ev.open_app과 같은 실패 기록·외부 요청 차단)."""
    ctx = await browser.new_context(viewport={"width": width, "height": height})
    ctx.set_default_timeout(WAIT_MS)
    await ctx.tracing.start(screenshots=True, snapshots=True)
    page = await ctx.new_page()
    page.errors, page.console, page.failed, page.navs, page.external = [], [], [], [], []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.on("console", lambda m: page.console.append(f"{m.type}: {m.text}"))
    page.on("requestfailed", lambda r: page.failed.append(f"{r.method} {r.url} {r.failure}")
            if r.url.startswith(base) else None)
    page.on("framenavigated", lambda fr: page.navs.append(fr.url) if fr == page.main_frame else None)
    entry = {"ctx": ctx, "page": page, "fake": fake, "orig_close": ctx.close,
             "fails_at_open": len(ev._CK.failures) if ev._CK else 0}
    ev._OPEN.append(entry)

    async def close(**kw):
        if entry in ev._OPEN:
            ev._OPEN.remove(entry)
            if ev._CK and len(ev._CK.failures) > entry["fails_at_open"]:
                print("  기록:", await ev._save_artifacts(entry, "check-" + ev._CK.failures[-1].split(":")[0]))
        return await entry["orig_close"](**kw)

    ctx.close = close

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
        status, payload = fake.respond(route.request.method, path, query, body)
        await route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))

    await page.route("**/*", handle)
    await page.goto(f"{base}/factcheck.html")
    await page.wait_for_selector("body[data-fc-ready='1']")
    await page.wait_for_function("document.querySelectorAll('#fc-company option').length === 2")
    return ctx, page


async def submit(page, text=DRAFT, source="ai_answer", company="00126380"):
    await page.select_option("#fc-company", company)
    await page.check(f"input[name='source'][value='{source}']")
    await page.fill("#fc-text", text)
    await page.click("#fc-submit")


async def badges(page, sel="#fc-list"):
    return [t.strip() for t in await page.locator(f"{sel} > li .badge").all_inner_texts()]


def clean(ck, page, name):
    ck.ok(not page.errors, f"{name}: 페이지 오류 없음 {page.errors}")
    ck.ok(not page.external, f"{name}: 외부 요청 없음 {page.external}")


# ── 시나리오 ────────────────────────────────────────────────────────────────

async def s_first(browser, base, ck):
    print("[first] 첫 검수: 점진 표시·⚠️❔ 먼저·근거 펼치기")
    fake = FakeFc(polls=[(200, job("running", [])), (200, job("running", [R0, R1], poll_ms=900)),
                         (200, job("done", ALL))])
    ctx, page = await open_fc(browser, base, fake)
    lede = await page.inner_text("#fc-lede")
    ck.ok("✅는 검색된 공시 문단과 일치한다는 뜻이고, 사실 보증·발행 승인이 아닙니다" in lede, "first: 랜딩 문구")
    ck.ok("삼성전자·SK하이닉스" in await page.inner_text("#fc-scope"), "first: 검색 범위 상시 표시")
    ck.ok("주어가 바뀐 문장" in await page.inner_text("#fc-weak"), "first: 알려진 약점 상시 표시")
    ck.ok("TypeSafe(외부 API)로 전송" in await page.inner_text("#fc-privacy"), "first: 외부 전송 고지")
    await submit(page)
    await page.wait_for_function("document.getElementById('fc-progress').innerText.includes('검수 중… 2 / 5')")
    ck.ok(await badges(page) == ["⚠️", "✅"], f"first: 중간 결과가 먼저 보인다 {await badges(page)}")
    ck.ok(await page.is_disabled("#fc-submit"), "first: 실행 중에는 다시 보낼 수 없다")
    await page.wait_for_function("document.getElementById('fc-progress').innerText.startsWith('완료')")
    ck.ok(await page.inner_text("#fc-summary") == "✅ 2 · ⚠️ 1 · ❔ 1 · ⊘ 0 · 건너뜀 1", "first: 요약 개수")
    ck.ok(await badges(page) == ["⚠️", "❔", "✅", "✅"], f"first: ⚠️·❔ 먼저 {await badges(page)}")
    posted = fake.posted()
    ck.ok(posted == [{"corp_code": "00126380", "source": "ai_answer", "text": DRAFT}], f"first: 보낸 본문 {posted}")
    # 펼치기 전에는 근거가 안 보이고, 펼치면 보고서명·기간·XBRL·문단
    first = page.locator("#fc-list > li").first
    ck.ok(not await first.locator(".ev").first.is_visible(), "first: 근거는 접혀 있다")
    await first.locator("summary").click()
    body = await first.inner_text()
    ck.ok("사업보고서 (2025.12)" in body and "기간 2025" in body and "접수번호 20260814000123" in body,
          f"first: 보고서명·기간·접수번호 {body!r}")
    ck.ok("XBRL 재무 수치: 영업이익 · 기간 2025 · 연결 · 43,601,100,000,000원" in body, f"first: XBRL 값 {body!r}")
    ck.ok(XSS in body, "first: 근거 문단의 태그는 글자로 보인다")
    ck.ok(await page.evaluate("window.__fcx === undefined && !document.querySelector('#fc-list img')"),
          "first: 태그가 실행되지 않는다(textContent)")
    ck.ok("숫자는 XBRL과 다름" in body, "first: 이유 표시")
    ck.ok(not await page.is_disabled("#fc-submit"), "first: 끝나면 다시 보낼 수 있다")
    clean(ck, page, "first")
    await ctx.close()


async def s_over(browser, base, ck):
    print("[over] 입력 초과 안내")
    fake = FakeFc(post=(422, {"detail": {"code": "too_many_sentences", "limit": 30, "targets": 31,
                                         "message": "익명 검수는 한 번에 검수 대상 문장 30개까지입니다. 지금 31개입니다. 나눠서 붙여 넣어 주세요."}}))
    ctx, page = await open_fc(browser, base, fake)
    await page.fill("#fc-text", "가" * 2001)
    ck.ok(await page.inner_text("#fc-count") == "2,001 / 2,000자", "over: 글자 수 표시")
    ck.ok("over" in (await page.get_attribute("#fc-count", "class")), "over: 초과 강조")
    await page.click("#fc-submit")
    err = await page.inner_text("#fc-error")
    ck.ok("2,000자까지" in err and "2,001자" in err, f"over: 2,000자 안내 {err!r}")
    ck.ok(fake.posted() == [], "over: 2,000자 초과는 보내지 않는다")
    await submit(page, text="짧은 글이다. " * 3)
    await page.wait_for_function("document.getElementById('fc-error').innerText.includes('30개까지')")
    ck.ok("지금 31개" in await page.inner_text("#fc-error"), "over: 30문장 초과 서버 안내")
    ck.ok(await page.is_hidden("#fc-result"), "over: 결과 영역은 열리지 않는다")
    ck.ok(not await page.is_disabled("#fc-submit"), "over: 고쳐서 다시 보낼 수 있다")
    clean(ck, page, "over")
    await ctx.close()


async def s_quota(browser, base, ck):
    print("[quota] 한도 소진 안내")
    runs = "익명 검수는 하루 3회까지입니다. 오늘 3회를 모두 썼습니다. 내일(한국 시간 자정 이후) 다시 써 주세요."
    glob = "오늘 검수 한도에 도달했습니다. 내일(한국 시간 자정 이후) 다시 써 주세요."
    fake = FakeFc(post=(429, {"detail": {"code": "cap_runs", "message": runs}}))
    ctx, page = await open_fc(browser, base, fake)
    await submit(page)
    await page.wait_for_function("document.getElementById('fc-error').innerText.length > 0")
    ck.ok(await page.inner_text("#fc-error") == runs, "quota: 하루 3회 소진 안내")
    fake.post = (429, {"detail": {"code": "cap_global", "message": glob}})
    await page.click("#fc-submit")
    await page.wait_for_function("document.getElementById('fc-error').innerText.startsWith('오늘 검수 한도')")
    ck.ok(await page.inner_text("#fc-error") == glob, "quota: 전체 한도 안내")
    ck.ok(await page.is_hidden("#fc-result"), "quota: 결과 영역은 열리지 않는다")
    clean(ck, page, "quota")
    await ctx.close()


async def s_skipped(browser, base, ck):
    print("[skipped] 건너뛴 문장 펼치기·수동 검수")
    redone = {**R3, "status": "supported", "category": "checked", "reason": None, "evidence": [EVID]}
    fake = FakeFc(polls=[(200, job("done", ALL))],
                  after_recheck=[(200, job("running", ALL)), (200, job("done", [R0, R1, R2, redone, R4]))])
    ctx, page = await open_fc(browser, base, fake)
    await submit(page)
    await page.wait_for_function("document.getElementById('fc-progress').innerText.startsWith('완료')")
    ck.ok(await page.inner_text("#fc-skipped-title") == "건너뛴 문장 1개", "skipped: 접힌 제목")
    ck.ok(await page.evaluate("!document.getElementById('fc-skipped').open"), "skipped: 처음엔 접혀 있다")
    ck.ok(not await page.is_visible("#fc-skipped-list .fc-recheck"), "skipped: 접혀 있으면 버튼이 안 보인다")
    ck.ok(SENTS[3] not in await page.inner_text("#fc-list"), "skipped: 확인 목록에 섞이지 않는다")
    await page.click("#fc-skipped-title")
    item = await page.inner_text("#fc-skipped-list")
    ck.ok(SENTS[3] in item and "이유: 의견·전망 — 의견·전망 문장" in item, f"skipped: 문장과 이유 {item!r}")
    await page.click("#fc-skipped-list .fc-recheck")
    await page.wait_for_function("document.getElementById('fc-summary').innerText.endsWith('건너뜀 0')")
    ck.ok([p for m, p, _ in fake.calls if m == "POST" and "recheck" in p] == [f"/api/factcheck/{JOB}/recheck/3"],
          "skipped: 수동 검수 요청 경로")
    ck.ok(await page.is_hidden("#fc-skipped"), "skipped: 건너뛴 문장이 없으면 상자를 숨긴다")
    ck.ok(await badges(page) == ["⚠️", "❔", "✅", "✅", "✅"], f"skipped: 검수 결과로 옮겨진다 {await badges(page)}")
    clean(ck, page, "skipped")
    await ctx.close()


async def s_expired(browser, base, ck):
    print("[expired] 만료·다른 브라우저(404)")
    msg = "검수 결과를 찾을 수 없습니다. 15분이 지나 만료됐거나 다른 브라우저에서 시작한 검수입니다."
    fake = FakeFc(polls=[(404, {"detail": {"code": "not_found", "message": msg}})])
    ctx, page = await open_fc(browser, base, fake)
    await submit(page)
    await page.wait_for_function("document.getElementById('fc-job-error').innerText.length > 0")
    ck.ok(await page.inner_text("#fc-job-error") == msg, "expired: 404 안내")
    ck.ok(not await page.is_disabled("#fc-submit"), "expired: 다시 보낼 수 있다")
    clean(ck, page, "expired")
    await ctx.close()


async def s_mobile(browser, base, ck):
    print("[mobile] 375px 폭")
    long_ev = {**EVID, "text": LONG, "report_nm": LONG[:60]}
    rs = [res(0, LONG, "contradicted", evidence=[long_ev], reason=LONG,
              xbrl={"account_nm": "매출액", "period": "2026H1", "fs_div": "OFS", "amount": 999999999999999999}),
          R2, {**R3, "text": LONG}, R4]
    fake = FakeFc(polls=[(200, job("done", rs))])
    ctx, page = await open_fc(browser, base, fake, width=375, height=740)
    await submit(page, text=DRAFT + " " + LONG)
    await page.wait_for_function("document.getElementById('fc-progress').innerText.startsWith('완료')")
    for d in await page.locator("#fc-list details").all():
        await d.locator("summary").click()
    await page.click("#fc-skipped-title")
    over = await page.evaluate("""() => {
      const de = document.documentElement, out = [];
      if (de.scrollWidth > de.clientWidth + 1) out.push(`page ${de.scrollWidth}>${de.clientWidth}`);
      document.querySelectorAll('main *').forEach(el => {
        const r = el.getBoundingClientRect();
        if (r.width && r.right > window.innerWidth + 1) out.push(el.id || el.className || el.tagName);
      });
      return out;
    }""")
    ck.ok(not over, f"mobile: 가로로 넘치는 요소 없음 {over[:5]}")
    ck.ok(await page.is_visible("#fc-skipped-list .fc-recheck"), "mobile: 수동 검수 버튼이 보인다")
    if os.environ.get("FC_SCREENSHOT"):  # 화면을 눈으로 보려면 저장 경로를 준다
        await page.screenshot(path=os.environ["FC_SCREENSHOT"], full_page=True)
    clean(ck, page, "mobile")
    await ctx.close()


async def main() -> int:
    srv, base = ev._serve()
    ck = ev._CK = ev.Checks()
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=ev._chromium())
        for scenario in (s_first, s_over, s_quota, s_skipped, s_expired, s_mobile):
            try:
                await scenario(browser, base, ck)
            except Exception as exc:  # noqa: BLE001 — 한 시나리오가 죽어도 나머지를 본다
                err = f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}"
                where = await ev.save_open_contexts(scenario.__name__, err)
                ck.ok(False, f"{scenario.__name__}: 예외 {type(exc).__name__}: {str(exc)[:300]} (기록: {', '.join(where)})")
        await browser.close()
    srv.shutdown()
    print(f"checks passed={ck.passed} failed={len(ck.failures)}")
    return 1 if ck.failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
