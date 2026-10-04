"""브라우저 E2E: 공시 근거 모드 화면(A-2 P4, spec 3.2·3.3·3.4·8절)을 가짜 API 응답으로 상태별 확인한다.

앱 서버·DB·Redis·Ollama·JEV 없이 돈다. public/을 정적 서버로 띄우고 /api/** 는 page.route로 가짜 응답을 준다.
외부 CDN(Tailwind·폰트·아이콘) 요청은 끊는다(외부 호출 없음). 실제 스타일로 보려면 EV_ALLOW_CDN=1.
pytest 수집 대상이 아니다(파일명이 test_* 가 아님). smoke_views.py와 같은 수동 실행 스크립트다.
    pip install playwright        # 브라우저가 없으면 playwright install chromium
    python tests/e2e/evidence_views.py
    # 브라우저를 직접 지정: CHROMIUM_PATH=/opt/pw-browsers/chromium-1194/chrome-linux/chrome
종료 코드 0 = 모든 확인 통과, 1 = 실패 있음.
"""
import asyncio
import datetime
import functools
import glob
import json
import os
import re
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from playwright.async_api import async_playwright

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
PUBLIC = os.path.join(ROOT, "public")

CID = "c0000000-0000-0000-0000-000000000001"
# 이모지(아스트랄 문자)로 시작한다: 서버 오프셋은 파이썬 코드포인트라 JS UTF-16 길이와 1 어긋난다
ANSWER = "📈 삼성전자의 주요 제품은 메모리 반도체입니다. 2025년 DX 부문 매출은 174조 8,877억원입니다.\n- 본사는 부산에 있습니다.\n문단에서 확인할 수 없습니다. 영업이익은 32조원입니다."
# 서버(claim_spans)가 나눈 오프셋과 같다(파이썬 str 인덱스 = 코드포인트). 세 번째 문장은 글머리표를 뺀 오프셋이다.
# claim_spans(ANSWER)로 확인한 값: (0,26) (27,59) (62,75) (76,92) (93,107)
_SENTS = ["📈 삼성전자의 주요 제품은 메모리 반도체입니다.", "2025년 DX 부문 매출은 174조 8,877억원입니다.",
          "본사는 부산에 있습니다.", "문단에서 확인할 수 없습니다.", "영업이익은 32조원입니다."]
PASSAGES = [{"passage_id": f"p{i}", "section": "II. 사업의 내용", "idx": 10 + i, "sha256": "x" * 8,
             "text": f"문단 {i} 본문 <b>태그</b> & 기호."} for i in range(8)]
PASSAGES[1]["text"] = "[표: 부문별 매출, 단위 억원] DX 부문 2025년 매출 174조 8,877억원, 전년 대비 3.1% 증가."
PASSAGES[2]["text"] = "본점 소재지는 경기도 수원시이다."


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _spans():
    out, cur = [], 0
    for i, t in enumerate(_SENTS):
        s = ANSWER.index(t, cur)
        out.append({"idx": i, "text": t, "start": s, "end": s + len(t)})
        cur = s + len(t)
    return out


def claims_with(statuses: list[str], extra: dict | None = None) -> list[dict]:
    out = []
    for sp, st in zip(_spans(), statuses):
        c = {**sp, "status": st, "route": None, "reason": None, "source_idx": None, "s": None, "c": None,
             "lex": None, "number_ok": None, "confidence": None, "cached": False, "attempts": 0, "latency_ms": 0.0}
        c.update((extra or {}).get(sp["idx"], {}))
        out.append(c)
    return out


PENDING = ["pending", "pending", "pending", "not_claim", "pending"]
DONE = {
    "statuses": ["supported", "supported", "contradicted", "not_claim", "no_evidence"],
    "extra": {
        0: {"source_idx": 0, "s": [0.97] + [0.1] * 7, "c": [0.01] * 8, "number_ok": [True] * 8, "confidence": "높음"},
        1: {"source_idx": 1, "s": [0.2, 0.78] + [0.1] * 6, "c": [0.01] * 8, "number_ok": [False, True] + [False] * 6,
            "confidence": "보통"},
        2: {"source_idx": 2, "s": [0.05] * 8, "c": [0.0, 0.0, 0.91] + [0.0] * 5, "number_ok": [True] * 8},
        4: {"s": [0.2] * 8, "c": [0.05] * 8, "number_ok": [False] * 8},
    },
}


def run(run_id: str, status: str, statuses=None, extra=None, *, retryable=False, chat_id="h1", error_code=None,
        poll_until_s=None, created_at=None, policy_version="a2-provisional", rejudgeable=False, trigger="auto"):
    cl = claims_with(statuses or PENDING, extra)
    counts = {k: 0 for k in ("supported", "contradicted", "no_evidence", "not_claim", "unjudged", "pending")}
    for c in cl:
        counts[c["status"]] += 1
    active = status in ("pending", "running")
    return {"id": run_id, "chat_id": chat_id, "conversation_id": CID, "status": status, "trigger": trigger,
            "error_code": error_code, "retryable": retryable, "rejudgeable": rejudgeable, "company": "삼성전자",
            "corp_code": "00126380", "rcept_no": "20260312000123", "passages": PASSAGES, "policy_version": policy_version,
            "jev_model": "jev-1.13.0", "generator_model": "llama3.1:8b", "calls": 3, "cache_hits": 0,
            "input_tokens": 1, "created_at": created_at or _now_iso(), "started_at": None, "finished_at": None,
            "counts": counts, "claims": cl, "poll_interval_ms": 500 if active else None,
            "poll_until_s": (poll_until_s or 12) if active else None}


def started(run_id: str, chat_id="h1", poll_until_s=12):
    cl = [{k: c[k] for k in ("idx", "text", "start", "end", "status")} for c in claims_with(PENDING)]
    return {"chat_id": chat_id, "conversation_id": CID, "run_id": run_id, "answer": ANSWER, "claims": cl,
            "poll_interval_ms": 500, "poll_until_s": poll_until_s}


COMPANIES = [{"corp_code": "00126380", "corp_name": "삼성전자", "stock_code": "005930", "rcept_no": "1", "passages": 120},
             {"corp_code": "00164779", "corp_name": "SK하이닉스", "stock_code": "000660", "rcept_no": "2", "passages": 90}]


class FakeApi:
    """시나리오별 가짜 API. runs[run_id]는 GET마다 하나씩 꺼내는 응답 목록(마지막은 계속 준다)."""

    def __init__(self, *, flag=200, acked=False, runs=None, chat=None, retry=None, active=None, conv=None,
                 timeline=None, agent_answer="일반 에이전트 답변입니다.", notice_status=200, chat_delay_s=0.0,
                 rejudge=None, agent_delay_s=0.0):
        self.flag, self.acked, self.runs = flag, acked, runs or {}
        self.chat, self.retry, self.active, self.conv, self.timeline = chat, retry, active, conv, timeline
        self.rejudge = rejudge
        self.agent_answer = agent_answer
        self.notice_status, self.chat_delay_s = notice_status, chat_delay_s
        self.agent_delay_s = agent_delay_s
        self.calls: list[tuple[str, str, dict | None]] = []

    def respond(self, method: str, path: str, query: str, body: dict | None):
        self.calls.append((method, path, body))
        if path == "/api/me":
            return 200, {"user": {"name": "테스터", "email": "t@example.com", "roles": ["user"]}, "state": {}}
        if path == "/api/stocks/market":
            return 200, {"indices": []}
        if path == "/api/evidence/companies":
            if self.flag != 200:
                return self.flag, {"detail": "공시 문단 저장소가 아직 준비되지 않았습니다" if self.flag == 503 else "Not Found"}
            q = re.search(r"q=([^&]*)", query or "")
            from urllib.parse import unquote
            q = unquote(q.group(1)) if q else ""
            return 200, {"companies": [c for c in COMPANIES if q in c["corp_name"]]}
        if path == "/api/evidence/notice":
            if self.flag == 404:
                return 404, {"detail": "Not Found"}
            if self.notice_status != 200:
                return self.notice_status, {"detail": "로그인이 필요합니다."}
            if method == "POST":
                self.acked = True
            return 200, {"acknowledged": self.acked, "version": "a2-notice-v1"}
        if path == "/api/evidence/chat":
            return 200, self.chat
        m = re.fullmatch(r"/api/evidence/runs/([^/]+)/retry", path)
        if m:
            return 201, self.retry
        m = re.fullmatch(r"/api/evidence/runs/([^/]+)/rejudge", path)
        if m:
            return (201, self.rejudge) if self.rejudge else (409, {"detail": "이 판정은 새 기준으로 재판정할 수 없습니다."})
        m = re.fullmatch(r"/api/evidence/runs/([^/]+)", path)
        if m:
            seq = self.runs.get(m.group(1)) or []
            return (200, seq.pop(0) if len(seq) > 1 else seq[0]) if seq else (404, {"detail": "없음"})
        if path == "/api/conversations/active":
            return 200, {"active_conversation": self.active}
        if path == f"/api/conversations/{CID}/evidence":
            return 200, self.timeline
        if path == f"/api/conversations/{CID}":
            return 200, self.conv
        if path == "/api/chat":
            return 200, {"answer": self.agent_answer, "steps": [], "citations": [], "conversation_id": CID}
        return 200, {}

    def n(self, method: str, path: str) -> int:
        return sum(1 for m, p, _ in self.calls if m == method and p == path)

    def bodies(self, method: str, path: str) -> list:
        return [b for m, p, b in self.calls if m == method and p == path]


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def _serve() -> tuple[ThreadingHTTPServer, str]:
    handler = functools.partial(_Quiet, directory=PUBLIC)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _chromium() -> str | None:
    if os.environ.get("CHROMIUM_PATH"):
        return os.environ["CHROMIUM_PATH"]
    found = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))
    return found[-1] if found else None


class Checks:
    def __init__(self):
        self.failures: list[str] = []
        self.passed = 0

    def ok(self, cond, label: str):
        if cond:
            self.passed += 1
        else:
            self.failures.append(label)
            print("  FAIL", label)


async def open_app(browser, base: str, fake: FakeApi, *, width=1280, height=900, init_script=None):
    ctx = await browser.new_context(viewport={"width": width, "height": height})
    ctx.set_default_timeout(5000)
    page = await ctx.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))

    async def handle(route):
        url = route.request.url
        if not url.startswith(base):
            if os.environ.get("EV_ALLOW_CDN") == "1" and not re.match(r"https?://(127\.0\.0\.1|localhost)", url):
                return await route.continue_()
            return await route.abort()
        path_q = url[len(base):]
        path, _, query = path_q.partition("?")
        if not path.startswith("/api/"):
            return await route.continue_()
        body = None
        if route.request.post_data:
            try:
                body = json.loads(route.request.post_data)
            except ValueError:
                body = None
        if path == "/api/evidence/chat" and fake.chat_delay_s:
            await asyncio.sleep(fake.chat_delay_s)
        if path == "/api/chat" and fake.agent_delay_s:
            await asyncio.sleep(fake.agent_delay_s)
        status, payload = fake.respond(route.request.method, path, query, body)
        await route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))

    await page.route("**/*", handle)
    if init_script:
        await page.add_init_script(init_script)
    await page.goto(f"{base}/app.html#agent-chat")
    await page.wait_for_timeout(600)
    return ctx, page


async def turn_on(page, *, confirm=True):
    # #ev-bar는 기능 확인(probe) 응답 뒤에 보인다. open_app의 고정 대기(600ms)만으로는 간헐적으로 모자랐다
    await page.wait_for_selector("#ev-bar:not(.hidden)", timeout=10000)
    await page.click("#ev-mode")
    await page.wait_for_timeout(200)
    if confirm and await page.is_visible("#ev-notice"):
        await page.click("#ev-notice-ok")
        await page.wait_for_timeout(200)


async def pick_company(page, text="삼성"):
    await page.fill("#ev-company", text)
    await page.wait_for_timeout(450)
    await page.press("#ev-company", "ArrowDown")
    await page.press("#ev-company", "Enter")
    await page.wait_for_timeout(100)


async def ask(page, q="주요 제품과 매출 비중은?"):
    await page.fill("#chat-input", q)
    await page.click("#chat-send")


async def summary(page) -> str:
    return (await page.locator(".ev-msg").last.locator(".ev-summary").inner_text()).strip()


async def badges(page) -> list[str]:
    return [t.strip() for t in await page.locator(".ev-msg").last.locator(".ev-badge").all_inner_texts()]


# ── 시나리오 ────────────────────────────────────────────────────────────────

async def s_pure(browser, base, ck: Checks):
    print("[pure] 순수 함수")
    ctx, page = await open_app(browser, base, FakeApi())
    res = await page.evaluate("""async (answer) => {
      const m = await import('/js/evidence.js');
      const claims = [{idx:1,start:10,end:15,status:'supported'},{idx:0,start:0,end:5,status:'pending'},
                      {idx:2,start:12,end:20,status:'no_evidence'},{idx:3,start:50,end:99,status:'no_evidence'}];
      const segs = m.segmentsFromClaims(answer, claims);
      return {
        joined: segs.map(s => s.text).join('') === answer,
        wrapped: segs.filter(s => s.claim).map(s => s.claim.idx),
        k_pending_running: m.badgeKind('running', 'pending'),
        k_skipped: m.badgeKind('skipped', 'supported'),
        k_failed: m.badgeKind('failed', 'no_evidence'),
        k_limited: m.badgeKind('limited', 'unjudged'),
        k_notclaim: m.badgeKind('done', 'not_claim'),
        k_partial_unj: m.badgeKind('partial', 'unjudged'),
        k_done_left_pending: m.badgeKind('done', 'pending'),
        poll_cont: m.pollDecision('running', 4000, 12),
        poll_to: m.pollDecision('pending', 12000, 12),
        poll_fallback: m.pollDecision('running', 15000, null),
        poll_done: m.pollDecision('done', 99999, 12),
        interval: [m.pollInterval(500), m.pollInterval(null), m.pollInterval(0)],
        hl: m.highlightNumbers("매출 1,234억원 & 5% <b>", "매출은 1,234억원", true),
        hl_off: m.highlightNumbers("매출 1,234억원", "매출은 1,234억원", false),
        hl_ent: m.highlightNumbers("it's 39", "39", true),
        s_failed: m.summaryText({status:'failed', passages:[]}),
        s_running: m.summaryText({status:'running', passages:[]}),
        s_prov2: m.summaryText({status:'done', policy_version:'a2-provisional-2', counts:{}, passages:[1]}),
        s_cap: m.summaryText({status:'done', policy_version:'a2-v1', counts:{supported:1}, passages:[1,2,3],
                              claims:[{status:'unjudged', reason:'claim_cap'}]}),
        s_unknown: m.summaryText({status:'done', policy_version:'a9-future', counts:{}, passages:[1]}),
        rejudge: [m.showRejudge({rejudgeable:true}), m.showRejudge({rejudgeable:false}), m.showRejudge({})],
        tip_jev: m.badgeTitle('supported', {status:'supported', route:'jev', confidence:'보통'}, {policy_version:'a2-v1'}),
        tip_lex: m.badgeTitle('supported', {status:'supported', route:'lex_high', confidence:null}, {policy_version:'a2-v1'}),
        tip_prov: m.badgeTitle('supported', {status:'supported', route:'jev', confidence:'높음'}, {policy_version:'a2-provisional-2'}),
        tip_pending: m.badgeTitle('pending', {status:'pending'}, {policy_version:'a2-v1'}),
        tip_low: m.judgeTooltip({route:'lex_low'}),
        tip_proto: m.judgeTooltip({route:'toString'}),
        emoji: (() => {
          const a = "📈 매출이 늘었습니다. 영업이익은 3조원입니다.";  // 서버 claim_spans: (0,12) (13,26), 코드포인트 26
          const segs = m.segmentsFromClaims(a, [{idx:0,start:0,end:12,status:'supported'},
                                                {idx:1,start:13,end:26,status:'supported'}]);
          return {joined: segs.map(s => s.text).join('') === a,
                  texts: segs.filter(s => s.claim).map(s => s.text)};
        })(),
        emoji_range: m.segmentsFromClaims("📈 가나다라", [{idx:0,start:2,end:7,status:'supported'}])
                       .filter(s => s.claim).length,
        elapsed_created: m.pollElapsedMs({created_at: new Date(Date.now() - 20000).toISOString()}, Date.now(), Date.now()),
        elapsed_fallback: m.pollElapsedMs({}, Date.now() - 3000, Date.now()),
        panel_nosrc: m.panelHtml({passages: [{section:'s', idx:1, text:'t'}], rcept_no:'1'},
                                 {status:'supported', source_idx:null, text:'x'}, 'supported'),
        panel_nosrc_w: m.panelHtml({passages: [], rcept_no:'1'}, {status:'contradicted', source_idx:9, text:'x'}, 'contradicted'),
        conf_server: m.confidenceText({status:'supported', confidence:'보통'}),
        conf_server_none: m.confidenceText({status:'supported', confidence:null}),
        exports: Object.keys(m).filter(k => ['NOTICE_TEXT','AFFILIATION_TEXT','POLICY_TAU_S'].includes(k)),
        retry: [m.showRetry({status:'failed', retryable:true}), m.showRetry({status:'partial', retryable:false}),
                m.showRetry({status:'limited', retryable:true}), m.showRetry({status:'done', retryable:true})],
      };
    }""", "0123456789abcdefghijklmn")
    ck.ok(res["joined"], "pure: 감싼 조각을 이으면 답변 원문과 같다")
    ck.ok(res["wrapped"] == [0, 1], f"pure: 오프셋 순서·겹침·범위 밖 제외 {res['wrapped']}")
    ck.ok(res["k_pending_running"] == "pending", "pure: 판정 중 pending → 회색 점")
    ck.ok(res["k_skipped"] is None, "pure: skipped → 배지 없음")
    ck.ok(res["k_failed"] == "unjudged" and res["k_limited"] == "unjudged", "pure: failed·limited → ⊘")
    ck.ok(res["k_notclaim"] is None, "pure: not_claim → 배지 없음")
    ck.ok(res["k_partial_unj"] == "unjudged" and res["k_done_left_pending"] == "unjudged", "pure: 끝난 실행의 미판정 → ⊘")
    ck.ok((res["poll_cont"], res["poll_to"], res["poll_fallback"], res["poll_done"])
          == ("continue", "timeout", "timeout", "done"), "pure: 폴링 상한(서버 값, 없으면 15초)")
    ck.ok(res["interval"] == [500, 500, 500], "pure: 폴링 간격 500ms")
    ck.ok(res["emoji"]["joined"] and res["emoji"]["texts"] == ["📈 매출이 늘었습니다.", "영업이익은 3조원입니다."],
          f"pure: 서버 코드포인트 오프셋으로 감싼다(이모지) {res['emoji']}")
    ck.ok(res["emoji_range"] == 0, "pure: 범위 검사는 코드포인트 길이(UTF-16 길이 아님)")
    ck.ok(19000 <= res["elapsed_created"] <= 21000, f"pure: 폴링 경과는 실행 created_at 기준 {res['elapsed_created']}")
    ck.ok(2900 <= res["elapsed_fallback"] <= 3100, "pure: created_at이 없으면 폴링 시작 시각 기준")
    ck.ok("근거 문단 정보 없음" in res["panel_nosrc"] and "확인하지 못했습니다" not in res["panel_nosrc"]
          and "근거 문단 정보 없음" in res["panel_nosrc_w"], "pure: ✅/⚠️인데 근거 문단이 없으면 '근거 문단 정보 없음'")
    ck.ok(res["conf_server"] == "보통" and res["conf_server_none"] is None, "pure: 확신도는 서버 confidence 값을 쓴다")
    ck.ok(res["exports"] == [], f"pure: 안 쓰는·복제 상수 export 없음 {res['exports']}")
    ck.ok(res["hl"] == "매출 <strong>1,234</strong>억원 &amp; 5% &lt;b&gt;", f"pure: 숫자 굵게·이스케이프 {res['hl']}")
    ck.ok("<strong>" not in res["hl_off"], "pure: 숫자 확인 미통과면 굵게 없음")
    ck.ok(res["hl_ent"] == "it&#39;s <strong>39</strong>", f"pure: 이스케이프 엔티티 숫자는 건드리지 않음 {res['hl_ent']}")
    ck.ok(res["s_failed"].startswith("AI 판정: 근거 판정을 하지 못했습니다(일시적 오류)"), "pure: failed 문구")
    ck.ok(res["s_running"] == "AI 판정 중…", "pure: 판정 중 문구")
    ck.ok(res["s_cap"].startswith("AI 판정: ✅ 1 · ⚠️ 0 · ❔ 0 — 검색된 2025.12 사업보고서 문단 3개 기준 (시험 기준)")
          and "긴 답변의 뒷부분은 판정하지 않았습니다" in res["s_cap"],
          f"pure: 요약줄·주장 상한 문구, a2-v1도 시험 기준(정밀도 목표 미확인) {res['s_cap']}")
    ck.ok("시험 기준" not in res["s_unknown"], f"pure: 목록에 없는 정책은 시험 기준을 붙이지 않는다 {res['s_unknown']}")
    ck.ok(res["rejudge"] == [True, False, False], "pure: 재판정 버튼은 서버 rejudgeable일 때만")
    note = "정밀도 목표를 확인하지 못한 시험 운영"
    ck.ok(note in res["tip_jev"] and "AI 판정(JEV 모델)" in res["tip_jev"] and "보수적" not in res["tip_jev"],
          f"pure: a2-v1 배지 툴팁에 사실대로 쓴 시험 운영 문구 {res['tip_jev']}")
    ck.ok(note in res["tip_lex"] and "규칙 판정" in res["tip_lex"] and "JEV 모델" not in res["tip_lex"],
          f"pure: lex_high ✅ 툴팁은 규칙 판정(JEV 모델이라 쓰지 않는다) {res['tip_lex']}")
    ck.ok("규칙 판정" in res["tip_low"] and "거의 같은" not in res["tip_low"], f"pure: lex_low 툴팁 {res['tip_low']}")
    ck.ok(res["tip_proto"].startswith("AI 판정(JEV 모델)"), f"pure: 모르는 경로는 JEV 문구 {res['tip_proto']}")
    ck.ok(note not in res["tip_prov"] and note not in res["tip_pending"],
          f"pure: 잠정 정책·판정 중 배지에는 덧붙이지 않는다 {res['tip_prov']} / {res['tip_pending']}")
    ck.ok("(시험 기준)" in res["s_prov2"], f"pure: 비주장 규칙 보강 뒤 잠정 정책도 시험 기준 {res['s_prov2']}")
    ck.ok(res["retry"] == [True, False, False, False], "pure: 다시 판정은 failed·partial이면서 서버 retryable일 때만")
    ck.ok(not page.errors, f"pure: JS 오류 없음 {page.errors}")
    await ctx.close()


async def s_flag_off(browser, base, ck: Checks):
    print("[flag-off] 404면 토글 숨김")
    fake = FakeApi(flag=404)
    ctx, page = await open_app(browser, base, fake)
    ck.ok(fake.n("GET", "/api/evidence/notice") == 1, "flag-off: 기능 확인은 가벼운 /api/evidence/notice로")
    ck.ok(fake.n("GET", "/api/evidence/companies") == 0, "flag-off: 회사 전체 목록을 기능 확인에 쓰지 않는다")
    ck.ok(not await page.is_visible("#ev-bar"), "flag-off: 근거 모드 토글이 보이지 않는다")
    await ask(page, "안녕")
    await page.wait_for_timeout(300)
    ck.ok(fake.n("POST", "/api/chat") == 1 and fake.n("POST", "/api/evidence/chat") == 0, "flag-off: 기존 채팅 경로")
    label = await page.locator("#chat-messages .ai-label").all_inner_texts()
    ck.ok(label == ["AI 생성 답변"], f"flag-off: 일반 답변에도 AI 생성 답변 라벨 {label}")
    ck.ok(not page.errors, f"flag-off: JS 오류 없음 {page.errors}")
    await ctx.close()

    fake = FakeApi(notice_status=401)
    ctx, page = await open_app(browser, base, fake)
    ck.ok(not await page.is_visible("#ev-bar"), "flag-off: 401이어도 토글을 숨긴다")
    await ctx.close()


async def s_agent_thread(browser, base, ck: Checks):
    print("[agent-thread] 일반 채팅도 현재 스레드 id를 보내고, 초기화하면 비운다")
    fake = FakeApi(flag=404)
    ctx, page = await open_app(browser, base, fake)
    labels = "#chat-messages .ai-label"
    await ask(page, "첫 질문")
    await page.wait_for_function(f"document.querySelectorAll('{labels}').length === 1")
    await ask(page, "두 번째 질문")
    await page.wait_for_function(f"document.querySelectorAll('{labels}').length === 2")
    await page.click("#clear-chat")
    await ask(page, "초기화 뒤 질문")
    await page.wait_for_function(f"document.querySelectorAll('{labels}').length === 1")
    sent = [b.get("conversation_id") for b in fake.bodies("POST", "/api/chat")]
    ck.ok(sent == [None, CID, None], f"agent-thread: 첫 질문은 id 없이, 다음은 응답 스레드 id로, 초기화 뒤 다시 없이 {sent}")
    ck.ok(not page.errors, f"agent-thread: JS 오류 없음 {page.errors}")
    await ctx.close()


async def s_clear_race(browser, base, ck: Checks):
    print("[clear-race] 응답을 기다리는 중 초기화하면 늦게 온 응답이 예전 스레드 id·말풍선을 되살리지 않는다")
    fake = FakeApi(flag=404, agent_delay_s=1.0)
    ctx, page = await open_app(browser, base, fake)
    await ask(page, "느린 질문")
    await page.wait_for_timeout(200)
    await page.click("#clear-chat")
    await page.wait_for_timeout(1300)  # 늦은 응답(conversation_id=CID)이 도착한다
    ck.ok(await page.locator("#chat-messages .ai-label").count() == 0, "clear-race: 초기화한 화면에 늦은 답변을 그리지 않는다")
    fake.agent_delay_s = 0.0
    await ask(page, "초기화 뒤 질문")
    await page.wait_for_function("document.querySelectorAll('#chat-messages .ai-label').length === 1")
    bodies = fake.bodies("POST", "/api/chat")
    ck.ok(bodies[-1].get("conversation_id") is None, f"clear-race: 일반 채팅 다음 질문은 스레드 id 없이 {bodies[-1]}")
    ck.ok(bodies[-1].get("history") == [], f"clear-race: 늦은 답변을 대화 이력에 넣지 않는다 {bodies[-1].get('history')}")
    ck.ok(not page.errors, f"clear-race: JS 오류 없음 {page.errors}")
    await ctx.close()

    fake = FakeApi(acked=True, chat=started("r1"), chat_delay_s=1.0,
                   runs={"r1": [run("r1", "done", DONE["statuses"], DONE["extra"])]})
    ctx, page = await open_app(browser, base, fake)
    await turn_on(page)
    await pick_company(page)
    await ask(page)
    await page.wait_for_timeout(200)
    await page.click("#clear-chat")
    await page.wait_for_timeout(1300)
    ck.ok(await page.locator(".ev-msg").count() == 0, "clear-race: 초기화한 화면에 늦은 근거 답변을 그리지 않는다")
    fake.chat_delay_s = 0.0
    await ask(page)
    await page.wait_for_selector(".ev-msg", timeout=5000)
    sent = fake.bodies("POST", "/api/evidence/chat")[-1]
    ck.ok(sent.get("conversation_id") is None, f"clear-race: 근거 모드 다음 질문도 스레드 id 없이 {sent}")
    ck.ok(not page.errors, f"clear-race: JS 오류 없음 {page.errors}")
    await ctx.close()


async def s_notice(browser, base, ck: Checks):
    print("[notice] 처음 켤 때 고지")
    fake = FakeApi()
    ctx, page = await open_app(browser, base, fake)
    ck.ok(await page.is_visible("#ev-bar"), "notice: 토글이 보인다")
    help_txt = await page.locator("#ev-help").inner_text()
    ck.ok("TypeSafe와 제휴 관계가 아닙니다" in help_txt and "질문 원문은 보내지 않습니다" in help_txt,
          "notice: 도움말에 비제휴·외부 전송 문구(상시)")
    await page.click("#ev-mode")
    await page.wait_for_timeout(200)
    ck.ok(await page.is_visible("#ev-notice"), "notice: 처음 켜면 고지 대화상자")
    txt = await page.locator("#ev-notice").inner_text()
    ck.ok("AI 답변 문장과 공시 문단을 외부 판정 서비스(TypeSafe JEV)로 보냅니다" in txt
          and "개인정보를 질문에 넣지 마세요" in txt, "notice: spec 8절 문구")
    await page.click("#ev-notice-cancel")
    await page.wait_for_timeout(150)
    ck.ok(not await page.is_checked("#ev-mode") and not await page.is_visible("#ev-company"),
          "notice: 취소하면 모드가 켜지지 않는다")
    ck.ok(fake.n("POST", "/api/evidence/notice") == 0, "notice: 취소는 기록하지 않는다")
    await turn_on(page)
    ck.ok(await page.is_checked("#ev-mode") and await page.is_visible("#ev-company"), "notice: 확인하면 켜지고 회사 선택")
    ck.ok(fake.n("POST", "/api/evidence/notice") == 1, "notice: 확인 사실을 서버(user_state)에 기록")
    await page.click("#ev-mode")
    await page.click("#ev-mode")
    await page.wait_for_timeout(150)
    ck.ok(not await page.is_visible("#ev-notice") and await page.is_checked("#ev-mode"), "notice: 두 번째부터는 묻지 않는다")
    await ctx.close()

    fake2 = FakeApi(acked=True)
    ctx, page = await open_app(browser, base, fake2)
    await page.click("#ev-mode")
    await page.wait_for_timeout(200)
    ck.ok(not await page.is_visible("#ev-notice") and await page.is_checked("#ev-mode"),
          "notice: 이미 확인한 사용자는 바로 켜진다")
    await ask(page)
    await page.wait_for_timeout(200)
    ck.ok(fake2.n("POST", "/api/evidence/chat") == 0, "notice: 회사를 고르지 않으면 보내지 않는다")
    await ctx.close()


async def s_done(browser, base, ck: Checks):
    print("[done] 판정 중 → 확정, 근거 펼치기, conversation_id")
    fake = FakeApi(acked=True, chat=started("r1"),
                   runs={"r1": [run("r1", "running"), run("r1", "done", DONE["statuses"], DONE["extra"])]})
    ctx, page = await open_app(browser, base, fake)
    await turn_on(page)
    await pick_company(page)
    ck.ok(await page.input_value("#ev-company") == "삼성전자", "done: 키보드로 회사 선택")
    await ask(page)
    await page.wait_for_selector(".ev-msg")
    b0 = await badges(page)
    ck.ok(b0 == ["⋯"] * 4, f"done: 처음엔 회색 점 4개(비주장 제외) {b0}")
    ck.ok(await summary(page) == "AI 판정 중…", "done: 판정 중 요약줄")
    body = fake.bodies("POST", "/api/evidence/chat")[0]
    ck.ok(body["corp_code"] == "00126380" and body["company"] == "삼성전자" and body["question"], "done: 요청 본문")
    await page.wait_for_function("document.querySelector('.ev-msg .ev-summary')?.innerText.startsWith('AI 판정: ✅')",
                                 timeout=5000)
    b1 = await badges(page)
    ck.ok(b1 == ["✅", "✅", "⚠️", "❔"], f"done: 확정 배지 {b1}")
    s = await summary(page)
    ck.ok(s.startswith("AI 판정: ✅ 2 · ⚠️ 1 · ❔ 1 — 검색된 2025.12 사업보고서 문단 8개 기준") and "(시험 기준)" in s,
          f"done: 요약줄 {s}")
    ck.ok(not await page.locator(".ev-msg .ev-retry").count(), "done: 다시 판정 버튼 없음")
    text = await page.locator(".ev-msg .ev-answer").evaluate(
        "el => Array.from(el.childNodes).filter(n => !(n.classList && n.classList.contains('ev-badge')))"
        ".map(n => n.textContent).join('')")
    ck.ok(text == ANSWER, "done: 프런트가 문장을 다시 나누지 않는다(오프셋으로 감싼 원문이 그대로)")
    spans = [t for t in await page.locator(".ev-msg .ev-claim").all_inner_texts()]
    ck.ok(spans == _SENTS, f"done: 서버 코드포인트 오프셋대로 문장을 감싼다(이모지 앞) {spans}")
    ck.ok(await page.locator(".ev-msg .ev-claim.ev-muted").count() == 1, "done: 비주장은 옅은 글씨·배지 없음")
    ck.ok(await page.locator(".ev-msg .ai-label").inner_text() == "AI 생성 답변", "done: AI 생성 답변 라벨")
    title = await page.locator(".ev-msg .ev-badge").first.get_attribute("title")
    ck.ok("AI 판정(JEV 모델) · 검색된 공시 문단 기준이며 사실 여부를 보증하지 않습니다" in title and "확신도: 높음" in title,
          f"done: 배지 툴팁 {title}")
    ck.ok(not re.search(r"0\.\d", title), "done: 확률 숫자를 보이지 않는다")

    # 키보드로 ✅(두 번째 문장) 펼치기
    badge = page.locator(".ev-msg .ev-badge").nth(1)
    ck.ok(await badge.evaluate("el => el.tagName") == "BUTTON", "done: 배지는 button")
    ck.ok(await badge.get_attribute("aria-expanded") == "false", "done: aria-expanded=false")
    await badge.focus()
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(100)
    ck.ok(await badge.get_attribute("aria-expanded") == "true", "done: Enter로 펼침")
    panel = page.locator("#" + await badge.get_attribute("aria-controls"))
    ptxt = await panel.inner_text()
    ck.ok("근거 문단 · 2025.12 사업보고서(접수번호 20260312000123) · II. 사업의 내용 · 문단 11" in ptxt,
          f"done: 근거 머리줄 {ptxt[:80]}")
    ck.ok("확신도 보통" in ptxt and not re.search(r"0\.\d", ptxt), "done: 확신도 보통, 확률 숫자 없음")
    strong = await panel.locator("strong").all_inner_texts()
    ck.ok(strong == ["2025", "174", "8,877"], f"done: 숫자 확인 통과 값만 굵게 {strong}")
    await badge.press("Enter")
    ck.ok(await badge.get_attribute("aria-expanded") == "false" and not await panel.is_visible(), "done: 다시 누르면 접힘")

    # ❔ 펼치기: 검색 문단 목록(접힘)
    q = page.locator(".ev-msg .ev-badge").nth(3)
    await q.click()
    qp = page.locator("#" + await q.get_attribute("aria-controls"))
    qt = await qp.inner_text()
    ck.ok("검색된 문단 8개에서 이 문장을 확인하지 못했습니다" in qt, "no_evidence: 문구")
    ck.ok(await qp.locator("details").count() == 1 and not await qp.locator("details").get_attribute("open"),
          "no_evidence: 문단 목록은 접혀 있다")
    await qp.locator("summary").click()
    ck.ok(await qp.locator("details li").count() == 8, "no_evidence: 펼치면 문단 8개")
    ck.ok("<b>태그</b>" in await qp.inner_text(), "no_evidence: 문단 본문은 이스케이프해 그대로")

    # ⚠️ 펼치기
    w = page.locator(".ev-msg .ev-badge").nth(2)
    await w.click()
    wt = await page.locator("#" + await w.get_attribute("aria-controls")).inner_text()
    ck.ok("근거 문단 ·" in wt and "본점 소재지는 경기도 수원시이다." in wt, "contradicted: 근거 문단")

    # 두 번째 질문은 첫 응답의 conversation_id를 보낸다
    fake.chat = started("r2", chat_id="h2")
    fake.runs["r2"] = [run("r2", "done", DONE["statuses"], DONE["extra"], chat_id="h2")]
    await ask(page, "두 번째 질문")
    await page.wait_for_function("document.querySelectorAll('.ev-msg').length === 2")
    b2 = fake.bodies("POST", "/api/evidence/chat")[1]
    ck.ok(b2.get("conversation_id") == CID, f"done: 후속 질문에 conversation_id {b2}")
    # 초기화하면 스레드 id도 비운다
    await page.click("#clear-chat")
    fake.chat = started("r3", chat_id="h3")
    fake.runs["r3"] = [run("r3", "done", DONE["statuses"], DONE["extra"], chat_id="h3")]
    await ask(page, "초기화 뒤 질문")
    await page.wait_for_selector(".ev-msg")
    b3 = fake.bodies("POST", "/api/evidence/chat")[2]
    ck.ok("conversation_id" not in b3, f"done: 초기화 뒤에는 conversation_id를 보내지 않는다 {b3}")
    ck.ok(not page.errors, f"done: JS 오류 없음 {page.errors}")
    await ctx.close()


async def s_double_send(browser, base, ck: Checks):
    print("[double-send] 요청 중 중복 전송 막기")
    fake = FakeApi(acked=True, chat=started("r1"), chat_delay_s=1.0,
                   runs={"r1": [run("r1", "done", DONE["statuses"], DONE["extra"])]})
    ctx, page = await open_app(browser, base, fake)
    await turn_on(page)
    await pick_company(page)
    await page.fill("#chat-input", "주요 제품은?")
    await page.press("#chat-input", "Enter")
    await page.wait_for_timeout(100)
    ck.ok(await page.is_disabled("#chat-send"), "double-send: 요청 중 전송 버튼 비활성")
    await page.press("#chat-input", "Enter")
    await page.press("#chat-input", "Enter")
    await page.click("#chat-send", force=True)
    await page.wait_for_selector(".ev-msg", timeout=5000)
    await page.wait_for_timeout(300)
    ck.ok(fake.n("POST", "/api/evidence/chat") == 1, f"double-send: 한 번만 보낸다 ({fake.n('POST', '/api/evidence/chat')})")
    ck.ok(await page.locator(".ev-msg").count() == 1, "double-send: 말풍선 하나")
    ck.ok(not await page.is_disabled("#chat-send"), "double-send: 끝나면 다시 보낼 수 있다")
    ck.ok(not page.errors, f"double-send: JS 오류 없음 {page.errors}")
    await ctx.close()


async def _final_state(browser, base, ck: Checks, name, final, expect_badges, expect_summary, expect_retry):
    print(f"[{name}]")
    fake = FakeApi(acked=True, chat=started("r1"), runs={"r1": [final]})
    ctx, page = await open_app(browser, base, fake)
    await turn_on(page)
    await pick_company(page)
    await ask(page)
    await page.wait_for_selector(".ev-msg")
    await page.wait_for_function("document.querySelector('.ev-msg .ev-summary')?.innerText.startsWith('AI 판정 중') === false",
                                 timeout=5000)
    b = await badges(page)
    ck.ok(b == expect_badges, f"{name}: 배지 {b}")
    s = await summary(page)
    ck.ok(s.startswith(expect_summary), f"{name}: 요약줄 {s}")
    ck.ok(await page.locator(".ev-msg .ev-retry").count() == (1 if expect_retry else 0), f"{name}: 다시 판정 버튼")
    ck.ok(not page.errors, f"{name}: JS 오류 없음 {page.errors}")
    return ctx, page, fake


async def s_states(browser, base, ck: Checks):
    partial = run("r1", "partial", ["supported", "unjudged", "no_evidence", "not_claim", "unjudged"],
                  {0: {"source_idx": 0, "s": [0.9] + [0.1] * 7, "number_ok": [True] * 8},
                   1: {"reason": "deadline"}, 4: {"reason": "deadline"}}, retryable=True, error_code="deadline")
    ctx, page, fake = await _final_state(browser, base, ck, "partial", partial, ["✅", "⊘", "❔", "⊘"],
                                         "AI 판정: ✅ 1 · ⚠️ 0 · ❔ 1 — 검색된 2025.12 사업보고서 문단 8개 기준", True)
    ck.ok("일부 문장은 판정하지 못했습니다" in await summary(page), "partial: 일부 미판정 문구")
    title = await page.locator(".ev-msg .ev-badge").nth(1).get_attribute("title")
    ck.ok(title.startswith("판정 불가"), f"partial: ⊘ 툴팁 {title}")
    # 다시 판정 → 새 실행을 폴링해 배지를 바꾼다
    fake.retry = started("r9")
    fake.runs["r9"] = [run("r9", "running"), run("r9", "done", DONE["statuses"], DONE["extra"])]
    await page.click(".ev-msg .ev-retry")
    await page.wait_for_function("document.querySelector('.ev-msg .ev-summary')?.innerText.startsWith('AI 판정: ✅ 2')",
                                 timeout=5000)
    ck.ok(fake.n("POST", "/api/evidence/runs/r1/retry") == 1, "partial: 최신 실행 id로 다시 판정 요청")
    ck.ok(await badges(page) == ["✅", "✅", "⚠️", "❔"], "partial: 다시 판정 결과로 배지 갱신")
    ck.ok(await page.locator(".ev-msg").count() == 1, "partial: 같은 말풍선에서 갱신")
    await ctx.close()

    failed = run("r1", "failed", ["unjudged", "unjudged", "unjudged", "not_claim", "unjudged"], retryable=True,
                 error_code="timeout")
    ctx, *_ = await _final_state(browser, base, ck, "failed", failed, ["⊘"] * 4,
                                 "AI 판정: 근거 판정을 하지 못했습니다(일시적 오류). 답변은 판정 없이 표시됩니다.", True)
    await ctx.close()

    failed_key = run("r1", "failed", ["unjudged", "unjudged", "unjudged", "not_claim", "unjudged"], retryable=False,
                     error_code="http_4xx")
    ctx, *_ = await _final_state(browser, base, ck, "failed-no-retry", failed_key, ["⊘"] * 4, "AI 판정: 근거 판정을", False)
    await ctx.close()

    limited = run("r1", "limited", ["unjudged", "unjudged", "unjudged", "not_claim", "unjudged"], retryable=False,
                  error_code="cap_user")
    ctx, *_ = await _final_state(browser, base, ck, "limited", limited, ["⊘"] * 4,
                                 "AI 판정: 오늘 판정 한도에 도달해 판정을 생략했습니다.", False)
    await ctx.close()

    skipped = run("r1", "skipped", ["unjudged", "unjudged", "unjudged", "not_claim", "unjudged"], error_code="pii")
    ctx, *_ = await _final_state(browser, base, ck, "skipped", skipped, [],
                                 "AI 판정: 개인정보로 보이는 내용이 있어 외부 판정을 생략했습니다.", False)
    await ctx.close()


async def s_timeout(browser, base, ck: Checks):
    print("[timeout] 폴링 상한 초과")
    fake = FakeApi(acked=True, chat=started("r1", poll_until_s=1),
                   runs={"r1": [run("r1", "running", poll_until_s=1)]})
    ctx, page = await open_app(browser, base, fake)
    await turn_on(page)
    await pick_company(page)
    await ask(page)
    await page.wait_for_selector(".ev-msg")
    await page.wait_for_function("document.querySelector('.ev-msg .ev-summary')?.innerText.includes('판정이 지연되고 있습니다')",
                                 timeout=5000)
    n = fake.n("GET", "/api/evidence/runs/r1")
    await page.wait_for_timeout(1200)
    ck.ok(fake.n("GET", "/api/evidence/runs/r1") == n, "timeout: 상한을 넘으면 폴링을 멈춘다")
    ck.ok(n <= 3, f"timeout: 500ms 간격(1초 상한에 {n}회)")
    fake.runs["r1"] = [run("r1", "done", DONE["statuses"], DONE["extra"])]
    await page.click(".ev-msg .ev-refresh")
    await page.wait_for_function("document.querySelector('.ev-msg .ev-summary')?.innerText.startsWith('AI 판정: ✅')",
                                 timeout=3000)
    ck.ok(await badges(page) == ["✅", "✅", "⚠️", "❔"], "timeout: 새로고침으로 결과 반영")
    ck.ok(not page.errors, f"timeout: JS 오류 없음 {page.errors}")
    await ctx.close()


async def s_restore(browser, base, ck: Checks):
    print("[restore] 대화를 다시 열면 배지 복원")
    done = run("r1", "done", DONE["statuses"], DONE["extra"], chat_id="h1")
    msgs = [
        {"id": "h0", "question": "일반 질문", "answer": "일반 답변", "steps": [], "citations": [],
         "latest_evidence_run": None},
        {"id": "h1", "question": "주요 제품은?", "answer": ANSWER, "steps": [], "citations": [],
         "latest_evidence_run": {"id": "r1", "status": "done", "counts": done["counts"]}},
    ]
    fake = FakeApi(active={"id": CID, "title": "t"}, conv={"id": CID, "messages": msgs, "msg_total": 2},
                   timeline={"conversation_id": CID, "runs": [done]})
    ctx, page = await open_app(browser, base, fake)
    await page.wait_for_selector(".ev-msg")
    ck.ok(await badges(page) == ["✅", "✅", "⚠️", "❔"], "restore: 저장된 판정으로 배지 복원")
    ck.ok((await summary(page)).startswith("AI 판정: ✅ 2"), "restore: 요약줄 복원")
    ck.ok(await page.locator("#chat-messages .ai-label").count() == 2, "restore: 두 답변 모두 AI 생성 답변 라벨")
    ck.ok(fake.n("GET", f"/api/conversations/{CID}/evidence") == 1, "restore: 판정 기록 한 번 조회")
    # 복원한 스레드에 이어 묻는다
    await turn_on(page)
    await pick_company(page)
    fake.chat = started("r5", chat_id="h5")
    fake.runs["r5"] = [run("r5", "done", DONE["statuses"], DONE["extra"], chat_id="h5")]
    await ask(page)
    await page.wait_for_function("document.querySelectorAll('.ev-msg').length === 2")
    ck.ok(fake.bodies("POST", "/api/evidence/chat")[0].get("conversation_id") == CID, "restore: 복원한 스레드 id로 질문")
    ck.ok(not page.errors, f"restore: JS 오류 없음 {page.errors}")
    await ctx.close()

    print("[restore-steps] 복원한 일반 답변들의 추론 패널 id가 겹치지 않는다")
    steps = [{"action": "search", "thought": "생각", "observation": "관찰"}]
    plain = [{"id": f"g{i}", "question": f"질문{i}", "answer": f"답변{i}", "steps": steps, "citations": [],
              "latest_evidence_run": None} for i in range(3)]
    fake = FakeApi(flag=404, active={"id": CID, "title": "t"}, conv={"id": CID, "messages": plain, "msg_total": 3})
    # 같은 밀리초에 연달아 그려도 겹치지 않아야 한다: Date.now를 고정한다
    ctx, page = await open_app(browser, base, fake, init_script="Date.now = () => 1700000000000;")
    await page.wait_for_selector(".steps-btn")
    targets = await page.eval_on_selector_all(".steps-btn", "els => els.map(e => e.dataset.target)")
    ck.ok(len(targets) == 3 and len(set(targets)) == 3, f"restore-steps: 패널 id가 모두 다르다 {targets}")
    await page.locator(".steps-btn").nth(2).click()
    vis = await page.eval_on_selector_all(".steps-btn", "els => els.map(e => !document.getElementById(e.dataset.target)"
                                          ".classList.contains('hidden'))")
    ck.ok(vis == [False, False, True], f"restore-steps: 세 번째 버튼은 세 번째 패널만 연다 {vis}")
    # 복원한 스레드에 일반 채팅으로 이어 묻는다: Redis 활성 값이 아니라 화면의 스레드 id로 보낸다
    await ask(page, "이어서")
    await page.wait_for_function("document.querySelectorAll('#chat-messages .ai-label').length === 4")
    ck.ok(fake.bodies("POST", "/api/chat")[0].get("conversation_id") == CID, "restore-steps: 일반 채팅도 복원한 스레드 id로 질문")
    await ctx.close()

    print("[restore-active] 진행 중 실행 복원 시 폴링 재개")
    fake = FakeApi(active={"id": CID, "title": "t"},
                   conv={"id": CID, "messages": [msgs[1]], "msg_total": 1},
                   timeline={"conversation_id": CID, "runs": [run("r1", "running")]},
                   runs={"r1": [run("r1", "done", DONE["statuses"], DONE["extra"])]})
    ctx, page = await open_app(browser, base, fake)
    await page.wait_for_function("document.querySelector('.ev-msg .ev-summary')?.innerText.startsWith('AI 판정: ✅')",
                                 timeout=5000)
    ck.ok(fake.n("GET", "/api/evidence/runs/r1") >= 1, "restore-active: 진행 중이면 폴링")
    await ctx.close()


# 1차 필터 상단 구간(lex_high, JEV 없음) ✅와 JEV ✅·❔를 함께 가진 a2-v1 결과
V1 = {
    "statuses": ["supported", "supported", "no_evidence", "not_claim", "no_evidence"],
    "extra": {
        0: {"route": "jev", "source_idx": 0, "s": [0.9] + [0.1] * 7, "c": [0.01] * 8, "number_ok": [True] * 8,
            "confidence": "보통"},
        1: {"route": "lex_high", "source_idx": 1, "lex": 1.0, "number_ok": [False, True] + [False] * 6},
        2: {"route": "jev", "s": [0.05] * 8, "c": [0.0] * 8, "number_ok": [True] * 8},
        4: {"route": "jev", "s": [0.8] * 8, "c": [0.05] * 8, "number_ok": [False] * 8},
    },
}


async def s_policy_v1(browser, base, ck: Checks):
    print("[policy-v1] 이전 정책 실행 재판정(연 스레드), lex_high ✅, 시험 운영 문구")
    old = run("r1", "done", DONE["statuses"], DONE["extra"], policy_version="a2-provisional-2", rejudgeable=True)
    new = run("r2", "done", V1["statuses"], V1["extra"], policy_version="a2-v1", trigger="rejudge")
    msgs = [{"id": "h1", "question": "주요 제품은?", "answer": ANSWER, "steps": [], "citations": [],
             "latest_evidence_run": {"id": "r1", "status": "done", "counts": old["counts"]}}]
    fake = FakeApi(active={"id": CID, "title": "t"}, conv={"id": CID, "messages": msgs, "msg_total": 1},
                   timeline={"conversation_id": CID, "runs": [old]}, rejudge=new)
    ctx, page = await open_app(browser, base, fake)
    await page.wait_for_selector(".ev-msg .ev-rejudge")
    btn = page.locator(".ev-msg .ev-rejudge")
    ck.ok(await btn.inner_text() == "새 기준으로 재판정", "policy-v1: 연 스레드의 이전 정책 실행에 재판정 버튼")
    ck.ok("(시험 기준)" in await summary(page), "policy-v1: 이전 정책 요약줄은 시험 기준")
    old_tip = await page.locator(".ev-msg .ev-badge").first.get_attribute("title")
    ck.ok("정밀도 목표를 확인하지 못한" not in old_tip, f"policy-v1: 잠정 정책 배지에는 a2-v1 문구 없음 {old_tip}")
    await page.locator(".ev-msg .ev-badge").first.click()  # 펼친 칸은 재판정 뒤 닫는다
    await btn.click()
    await page.wait_for_function("document.querySelector('.ev-msg .ev-summary')?.innerText.startsWith('AI 판정: ✅ 2 · ⚠️ 0 · ❔ 2')",
                                 timeout=5000)
    ck.ok(fake.n("POST", "/api/evidence/runs/r1/rejudge") == 1, "policy-v1: 옛 실행 id로 재판정 요청")
    ck.ok(fake.n("GET", "/api/evidence/runs/r2") == 0, "policy-v1: 재판정은 폴링하지 않는다(응답이 종결 실행)")
    ck.ok(await badges(page) == ["✅", "✅", "❔", "❔"], f"policy-v1: 재판정 결과 배지 {await badges(page)}")
    s = await summary(page)
    ck.ok(s.endswith("문단 8개 기준 (시험 기준)") and "새 기준으로 재판정" not in s and "다시 판정" not in s,
          f"policy-v1: a2-v1 요약줄은 시험 기준 유지, 재판정 버튼 사라짐 {s}")
    ck.ok(not await page.locator(".ev-msg .ev-panel").count(), "policy-v1: 재판정하면 펼친 칸을 닫는다")
    ck.ok(await page.locator(".ev-msg").count() == 1, "policy-v1: 같은 말풍선에서 갱신")
    jev_tip = await page.locator(".ev-msg .ev-badge").nth(0).get_attribute("title")
    ck.ok("AI 판정(JEV 모델)" in jev_tip and "정밀도 목표를 확인하지 못한 시험 운영" in jev_tip and "확신도: 보통" in jev_tip,
          f"policy-v1: JEV ✅ 툴팁 {jev_tip}")
    lex = page.locator(".ev-msg .ev-badge").nth(1)
    lex_tip = await lex.get_attribute("title")
    ck.ok(lex_tip.startswith("검색된 공시 문단에서 확인됨") and "규칙 판정" in lex_tip and "JEV 모델" not in lex_tip
          and "확신도" not in lex_tip and "정밀도 목표를 확인하지 못한 시험 운영" in lex_tip,
          f"policy-v1: lex_high ✅ 툴팁(규칙 판정, 확신도 없음) {lex_tip}")
    await lex.click()
    lp = await page.locator("#" + await lex.get_attribute("aria-controls")).inner_text()
    ck.ok("근거 문단 · 2025.12 사업보고서(접수번호 20260312000123) · II. 사업의 내용 · 문단 11" in lp
          and "174조 8,877억원" in lp and "확신도" not in lp and "규칙 판정" in lp,
          f"policy-v1: lex_high ✅ 근거 문단 펼치기 {lp[:120]}")
    ck.ok(not page.errors, f"policy-v1: JS 오류 없음 {page.errors}")
    await ctx.close()

    print("[policy-v1-live] 새 질문(a2-v1)은 재판정 버튼이 없다")
    fake = FakeApi(acked=True, chat=started("r3"),
                   runs={"r3": [run("r3", "running"), run("r3", "done", V1["statuses"], V1["extra"],
                                                         policy_version="a2-v1")]})
    ctx, page = await open_app(browser, base, fake)
    await turn_on(page)
    await pick_company(page)
    await ask(page)
    await page.wait_for_function("document.querySelector('.ev-msg .ev-summary')?.innerText.startsWith('AI 판정: ✅')",
                                 timeout=5000)
    ck.ok(await badges(page) == ["✅", "✅", "❔", "❔"], "policy-v1-live: lex_high 포함 배지")
    ck.ok(not await page.locator(".ev-msg .ev-rejudge").count(), "policy-v1-live: 현재 정책 실행에는 재판정 버튼 없음")
    ck.ok((await summary(page)).endswith("(시험 기준)"), "policy-v1-live: 요약줄 시험 기준")
    ck.ok(fake.n("POST", "/api/evidence/runs/r3/rejudge") == 0, "policy-v1-live: 자동 재판정 없음")
    ck.ok(not page.errors, f"policy-v1-live: JS 오류 없음 {page.errors}")
    await ctx.close()

    print("[policy-v1-refused] 재판정 409면 알림, 버튼 다시 사용 가능")
    fake = FakeApi(active={"id": CID, "title": "t"}, conv={"id": CID, "messages": msgs, "msg_total": 1},
                   timeline={"conversation_id": CID, "runs": [old]}, rejudge=None)
    ctx, page = await open_app(browser, base, fake)
    await page.wait_for_selector(".ev-msg .ev-rejudge")
    await page.click(".ev-msg .ev-rejudge")
    await page.wait_for_timeout(300)
    ck.ok(await page.locator(".ev-msg .ev-rejudge").is_enabled(), "policy-v1-refused: 실패하면 버튼을 다시 켠다")
    ck.ok(await badges(page) == ["✅", "✅", "⚠️", "❔"], "policy-v1-refused: 배지는 그대로")
    await ctx.close()


async def s_mobile(browser, base, ck: Checks):
    print("[mobile] 375px 폭")
    fake = FakeApi(acked=True, chat=started("r1"),
                   runs={"r1": [run("r1", "done", DONE["statuses"], DONE["extra"])]})
    ctx, page = await open_app(browser, base, fake, width=375, height=740)
    ck.ok(await page.is_visible("#ev-bar"), "mobile: 토글이 보인다")
    await turn_on(page)
    await pick_company(page)
    await ask(page)
    await page.wait_for_function("document.querySelector('.ev-msg .ev-summary')?.innerText.startsWith('AI 판정: ✅')",
                                 timeout=5000)
    await page.locator(".ev-msg .ev-badge").nth(3).click()
    await page.locator(".ev-msg details summary").click()
    over = await page.evaluate("""() => {
      const out = [];
      for (const sel of ['#ev-bar', '.ev-msg', '.ev-panel', '#chat-input', '#chat-send']) {
        document.querySelectorAll(sel).forEach(el => {
          const r = el.getBoundingClientRect();
          if (r.width && (r.right > window.innerWidth + 1 || el.scrollWidth > el.clientWidth + 1)) out.push(sel);
        });
      }
      return out;
    }""")
    ck.ok(not over, f"mobile: 가로로 넘치는 요소 없음 {over}")
    if os.environ.get("EV_SCREENSHOT"):  # 화면을 눈으로 보려면 저장 경로를 준다
        await page.screenshot(path=os.environ["EV_SCREENSHOT"], full_page=True)
    await ctx.close()


async def main() -> int:
    srv, base = _serve()
    ck = Checks()
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=_chromium())
        for scenario in (s_pure, s_flag_off, s_agent_thread, s_clear_race, s_notice, s_done, s_double_send, s_states, s_timeout, s_restore,
                         s_policy_v1, s_mobile):
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
