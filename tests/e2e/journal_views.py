"""브라우저 E2E: 투자 판단 일지 화면(모듈 C P3, spec 3.2 화면 상태·4절 고지·8.1의 8)을 가짜 API 응답으로 상태별 확인한다.

evidence_views.py와 같은 방식이다. 앱 서버·DB·Redis·Ollama·JEV 없이 public/을 정적 서버로 띄우고 /api/** 는 page.route로
가짜 응답을 준다. 외부 CDN 요청은 끊고, 일지 화면을 다루는 동안 밖으로 나가는 요청이 없는지도 센다.
변화 칸(GET /api/journal/{id}/changes)은 P2가 병행 구현 중이라 아래 CHANGES_* 모양(가정한 계약)으로 가짜 응답을 준다.
pytest 수집 대상이 아니다(파일명이 test_* 가 아님).
    pip install playwright        # 브라우저가 없으면 playwright install chromium
    python tests/e2e/journal_views.py
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
import evidence_views as ev  # noqa: E402  같은 폴더의 가짜 판정 응답·정적 서버를 그대로 쓴다

WAIT_MS = 10_000  # 상태 기반 대기의 상한(evidence_views.py와 같다)
KST = datetime.timezone(datetime.timedelta(hours=9))

# spec 결정 4-2 고정 고지(app/routes/journal.py NOTICE와 같은 글자)
NOTICE = ("판단 일지는 내가 쓴 기록입니다. 답변과 배지는 AI가 만든 것으로, 배지는 검색된 공시 문단 기준 AI 판정"
          "(TypeSafe의 JEV 모델)이며 사실 여부를 보증하지 않습니다. 이 서비스는 투자 권유나 수익 예측을 하지 않으며, "
          "투자 판단과 그 결과는 본인에게 있습니다. 이 프로젝트는 TypeSafe와 제휴 관계가 아닙니다.")
Q1 = "HBM 매출 비중과 주요 고객은?"
Q2 = "SK하이닉스의 2025년 사업부문별 매출 구성과 주요 고객사, 그리고 설비투자 계획은 무엇인가요?"  # 40자 넘음
CLAIM_KEYS = ("idx", "text", "start", "end", "status", "route", "reason", "source_idx", "number_ok", "confidence")
# 가격·수익률·성과 표시가 없어야 한다(결정 4-1·4-3)
FORBIDDEN_WORDS = ("수익률", "적중", "주가", "근거 점수", "신뢰도 합계")
# 스냅샷 문단마다 다른 본문 해시(변화 응답의 sha256을 스냅샷과 맞춰 본다)
JR_PASSAGES = [{**p, "sha256": f"{i + 1:064x}"} for i, p in enumerate(ev.PASSAGES)]
TIP = "검색된 공시 문단 기준이며 사실 여부를 보증하지 않습니다"  # spec 4-1 그대로


def today_kst() -> datetime.date:
    return datetime.datetime.now(KST).date()


def snapshot(status="done", statuses=None, extra=None, *, policy="a2-v1", company="삼성전자", corp="00126380",
             question=Q1, created="2026-10-03T16:30:00+00:00"):
    """형식 c1 스냅샷(spec 결정 5-3). created는 UTC 10-03 16:30 = KST 10-04(날짜를 KST로 보이는지 본다)."""
    r = ev.run("r", status, statuses or ev.DONE["statuses"], extra if extra is not None else ev.DONE["extra"],
               policy_version=policy)
    return {"run": {"id": "r", "status": status, "error_code": None, "trigger": "auto", "policy_version": policy,
                    "jev_model": "jev-1.13.0", "generator_model": "llama3.1:8b", "created_at": created,
                    "finished_at": created},
            "company": company, "corp_code": corp, "rcept_no": "20260312000123", "question": question,
            "answer": ev.ANSWER, "claims": [{k: c[k] for k in CLAIM_KEYS} for c in r["claims"]],
            "passages": JR_PASSAGES}


def upd(uid, kind, decision, conviction, memo="", relied=None, review_on=None, created="2026-10-03T16:30:00+00:00"):
    return {"id": uid, "kind": kind, "decision": decision, "conviction": conviction, "memo": memo,
            "relied_claims": relied or [], "review_on": review_on, "created_at": created}


def entry(eid, snap, updates, *, source=True, run_id=None):
    return {"id": eid, "run_id": run_id or f"run-{eid}", "company": snap["company"], "corp_code": snap["corp_code"],
            "snapshot_version": "c1", "created_at": snap["run"]["created_at"], "snapshot": snap,
            "updates": updates, "source_available": source}


def list_item(e, today):
    latest = e["updates"][-1] if e["updates"] else None
    due = bool(latest and latest["review_on"] and latest["review_on"] <= today.isoformat())
    return {"id": e["id"], "run_id": e["run_id"], "company": e["company"], "corp_code": e["corp_code"],
            "question": e["snapshot"]["question"], "policy_version": e["snapshot"]["run"]["policy_version"],
            "run_status": e["snapshot"]["run"]["status"], "created_at": e["created_at"],
            "current": ({k: latest[k] for k in ("decision", "conviction", "review_on", "created_at")} if latest else None),
            "update_count": len(e["updates"]), "due": due}


def fixtures():
    t = today_kst()
    past = (t - datetime.timedelta(days=3)).isoformat()
    future = (t + datetime.timedelta(days=40)).isoformat()
    e1 = entry("e1", snapshot(policy="a2-provisional-2"), [
        upd("u1", "initial", "consider_buy", 4, memo="HBM 고객 다변화가 보인다 <script>x</script>", relied=[0, 1],
            review_on=past),
        upd("u2", "revisit", "watch", 3, memo="다시 보니 고객 집중이 크다", review_on=past,
            created="2026-10-04T02:00:00+00:00"),
    ])
    failed = snapshot("failed", ["unjudged", "unjudged", "unjudged", "not_claim", "unjudged"], {},
                      company="SK하이닉스", corp="00164779", question=Q2)
    e2 = entry("e2", failed, [upd("u3", "initial", "exclude", 2, review_on=future)], source=False)
    return {"e1": e1, "e2": e2}


def changes_ok():
    return {"status": "ok",
            "report": {"status": "replaced", "snapshot_rcept_no": "20260312000123",
                       "current_rcept_nos": ["20260315000999"]},
            "passages": [{"passage_idx": i, "passage_id": f"p{i}", "sha256": JR_PASSAGES[i]["sha256"],
                          "status": "gone" if i == 1 else "same"} for i in range(8)],
            "policy": {"snapshot": "a2-provisional-2", "current": "a2-v1"}}


def changes_bad_sha():
    """passage_idx 3의 해시가 스냅샷과 어긋난 응답: 짝이 틀렸으니 확인 불가여야 한다."""
    c = changes_ok()
    c["passages"][3]["sha256"] = "f" * 64
    return c


CHANGES_SAME = {"status": "ok",
                "report": {"status": "same", "snapshot_rcept_no": "20260312000123",
                           "current_rcept_nos": ["20260312000123"]},
                "passages": [{"passage_idx": i, "passage_id": f"p{i}", "sha256": JR_PASSAGES[i]["sha256"],
                              "status": "same"} for i in range(8)],
                "policy": {"snapshot": "a2-v1", "current": "a2-v1"}}


class JournalApi(ev.FakeApi):
    """판정 가짜 API(evidence_views.FakeApi)에 일지 경로를 더한다. 일지 경로가 먼저다."""

    def __init__(self, *, journal=200, entries=None, create=None, changes=None, changes_delay_s=0.0,
                 update_status=201, journal_seq=None, create_delay_s=0.0, **kw):
        super().__init__(**kw)
        self.journal_seq = list(journal_seq or [])  # GET /api/journal 응답 상태를 차례로(빈 뒤로는 정상)
        self.create_delay_s = create_delay_s
        self.journal = journal
        self.entries = dict(entries if entries is not None else fixtures())
        self.create = create or (201, None)
        self.changes = changes or {}
        self.changes_delay_s = changes_delay_s
        self.update_status = update_status
        self.queries: list[dict] = []

    def respond(self, method, path, query, body):
        if not path.startswith("/api/journal"):
            return super().respond(method, path, query, body)
        self.calls.append((method, path, body))
        if self.journal != 200:
            return self.journal, {"detail": "Not Found"}
        q = {k: v[0] for k, v in parse_qs(query or "").items()}
        today = today_kst()
        if path == "/api/journal" and method == "GET":
            self.queries.append(q)
            if self.journal_seq:
                st = self.journal_seq.pop(0)
                if st != 200:
                    return st, {"detail": "일시 오류"}
            items = [list_item(e, today) for e in sorted(self.entries.values(), key=lambda e: e["created_at"],
                                                          reverse=True)]
            due_count = sum(i["due"] for i in items)
            picked = [i for i in items if (q.get("due") != "true" or i["due"])
                      and (not q.get("corp_code") or i["corp_code"] == q["corp_code"])
                      and (not q.get("decision") or i["current"]["decision"] == q["decision"])]
            off, lim = int(q.get("offset", 0)), int(q.get("limit", 50))
            return 200, {"items": picked[off:off + lim], "total": len(picked), "due_count": due_count,
                         "limit": lim, "offset": off}
        if path == "/api/journal" and method == "POST":
            status, payload = self.create
            if status == 201 and payload is None:
                snap = snapshot()
                payload = entry("new1", snap, [upd("n1", "initial", body["decision"], body["conviction"],
                                                   body.get("memo", ""), body.get("relied_claims"),
                                                   body.get("review_on"))], run_id=body["run_id"])
                self.entries["new1"] = payload
            return status, payload
        if path == "/api/journal" and method == "DELETE":
            if q.get("confirm") != "delete-all":
                return 400, {"detail": "전체 삭제는 confirm=delete-all이 필요합니다."}
            n = len(self.entries)
            self.entries.clear()
            return 200, {"deleted": n}
        if path == "/api/journal/export":
            return 200, {"notice": NOTICE, "count": len(self.entries), "entries": list(self.entries.values())}
        m = re.fullmatch(r"/api/journal/([^/]+)/changes", path)
        if m:
            st, payload = self.changes.get(m.group(1), (404, {"detail": "Not Found"}))
            return st, payload
        m = re.fullmatch(r"/api/journal/([^/]+)/updates", path)
        if m:
            e = self.entries.get(m.group(1))
            if e is None:
                return 404, {"detail": "기록을 찾을 수 없습니다."}
            if self.update_status != 201:
                return self.update_status, {"detail": "기록을 저장하지 못했습니다."}
            u = upd(f"r{len(e['updates'])}", "revisit", body["decision"], body["conviction"], body.get("memo", ""),
                    body.get("relied_claims"), body.get("review_on"), created=datetime.datetime.now(
                        datetime.timezone.utc).isoformat())
            e["updates"].append(u)
            return 201, u
        m = re.fullmatch(r"/api/journal/([^/]+)", path)
        if m:
            e = self.entries.get(m.group(1))
            if e is None:
                return 404, {"detail": "기록을 찾을 수 없습니다."}
            if method == "DELETE":
                del self.entries[m.group(1)]
                return 200, {"deleted": 1}
            return 200, e
        return 404, {"detail": "Not Found"}

    def journal_calls(self, method=None):
        return [(m, p, b) for m, p, b in self.calls if p.startswith("/api/journal") and (method is None or m == method)]


async def open_app(browser, base, fake: JournalApi, *, hash_="agent-chat", width=1280, height=900):
    ctx = await browser.new_context(viewport={"width": width, "height": height}, accept_downloads=True)
    ctx.set_default_timeout(WAIT_MS)
    page = await ctx.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.external = []  # 정적 서버 밖으로 나가려 한 요청(끊는다)

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
        if re.fullmatch(r"/api/journal/[^/]+/changes", path) and fake.changes_delay_s:
            await asyncio.sleep(fake.changes_delay_s)
        if path == "/api/journal" and route.request.method == "POST" and fake.create_delay_s:
            await asyncio.sleep(fake.create_delay_s)
        if path == "/api/evidence/chat" and fake.chat_delay_s:
            await asyncio.sleep(fake.chat_delay_s)
        status, payload = fake.respond(route.request.method, path, query, body)
        headers = {}
        if path == "/api/journal/export" and status == 200:
            headers["Content-Disposition"] = f'attachment; filename="lumina-journal-{today_kst():%Y%m%d}.json"'
        await route.fulfill(status=status, content_type="application/json", body=json.dumps(payload),
                            headers=headers)

    await page.route("**/*", handle)
    await page.goto(f"{base}/app.html#{hash_}")
    await page.wait_for_timeout(600)
    return ctx, page


async def done_answer(page, fake, run_status="done", **run_kw):
    """근거 모드로 질문해 끝난 판정 답변을 하나 그린다."""
    fake.chat = ev.started("r1")
    fake.runs["r1"] = [ev.run("r1", run_status, ev.DONE["statuses"], ev.DONE["extra"], **run_kw)]
    await ev.turn_on(page)
    await ev.pick_company(page)
    await ev.ask(page)
    await page.wait_for_function("document.querySelector('.ev-msg .ev-summary')?.innerText.startsWith('AI 판정: ✅')")


def no_leak(ck, page, fake, name, external_before, calls_before=0):
    """external_before·calls_before 뒤(일지 화면을 다룬 동안)에 밖으로 나간 요청·판정/생성/알림 호출이 없는지."""
    ck.ok(page.external[external_before:] == [], f"{name}: 일지 화면에서 밖으로 나가는 요청 없음 {page.external[external_before:]}")
    bad = [(m, p) for m, p, _ in fake.calls[calls_before:]
           if p in ("/api/evidence/chat", "/api/chat") or p.endswith("/retry") or p.endswith("/rejudge")
           or p.startswith("/api/notification")]
    ck.ok(not bad, f"{name}: 일지가 판정·생성·알림 경로를 부르지 않는다 {bad}")


# ── 시나리오 ────────────────────────────────────────────────────────────────

async def s_pure(browser, base, ck):
    print("[pure] 순수 함수")
    ctx, page = await open_app(browser, base, JournalApi(flag=404))
    res = await page.evaluate("""async () => {
      const m = await import('/js/journal.js');
      return {
        notice: m.JOURNAL_NOTICE,
        labels: ['consider_buy','watch','exclude','zzz'].map(m.decisionLabel),
        kst: m.kstDate('2026-10-03T16:30:00+00:00'),
        kst_bad: m.kstDate(null),
        add1: m.addMonths('2026-01-31', 1), add3: m.addMonths('2026-11-30', 3), add12: m.addMonths('2026-10-04', 12),
        btn: [m.recordButtonState(false, {status:'done'}), m.recordButtonState(true, {status:'running'}),
              m.recordButtonState(true, {status:'pending'}), m.recordButtonState(true, {status:'failed'}),
              m.recordButtonState(true, {status:'done', journal_entry_id:'e1'}),
              m.recordButtonState(true, {status:'skipped', journal_entry_id:null})],
        band: ['done','partial','failed','limited','skipped'].map(m.noJudgeBand),
        head: m.snapshotHead({run:{policy_version:'a2-v1', created_at:'2026-10-03T16:30:00+00:00'}}),
        ch_bad: [m.changesModel(null), m.changesModel({}), m.changesModel({status:'ok'}),
                 m.changesModel({status:'unavailable'}), m.changesModel({status:'ok', report:{status:'weird'}, passages:[]}),
                 m.changesModel({status:'ok', report:{status:'same', current_rcept_nos:[]}, passages:[{passage_idx:0,status:'odd'}]})
                ].map(x => x.status),
        order: m.changesModel({status:'ok', report:{status:'same', snapshot_rcept_no:'1', current_rcept_nos:['1']},
                 passages:[0,1,2,3].map(i => ({passage_idx:i, sha256:'h'+i, status:'same'})), policy:{snapshot:'a', current:'a'}},
                {claims:[{idx:0,status:'no_evidence',source_idx:0},{idx:1,status:'supported',source_idx:3},
                         {idx:2,status:'contradicted',source_idx:2}],
                 passages:[0,1,2,3].map(i => ({sha256:'h'+i}))}).passages.map(p => p.passage_idx),
        sha: (() => {
          const snap = {claims: [], passages: [{sha256:'a'}, {sha256:'b'}]};
          const mk = ps => ({status:'ok', report:{status:'same', snapshot_rcept_no:'1', current_rcept_nos:['1']}, passages: ps});
          return [mk([{passage_idx:0, sha256:'a', status:'same'}, {passage_idx:1, sha256:'b', status:'gone'}]),
                  mk([{passage_idx:0, sha256:'b', status:'same'}]),            // 짝이 바뀐 해시
                  mk([{passage_idx:0, status:'same'}]),                        // 해시 없음
                  mk([{passage_idx:5, sha256:'a', status:'same'}]),            // 범위 밖
                 ].map(r => m.changesModel(r, snap).status)
                 .concat([m.changesModel(mk([{passage_idx:0, sha256:'a', status:'same'}]), null).status]);
        })(),
        tips: [m.pastBadgeTitle('supported', {status:'supported', route:'jev', confidence:'높음'}, {policy_version:'a2-v1'}),
               m.pastBadgeTitle('supported', {status:'supported', route:'lex_high'}, {policy_version:'a2-v1'}),
               m.pastBadgeTitle('no_evidence', {status:'no_evidence', route:'lex_low'}, {policy_version:'a2-v1'})],
        trunc: m.truncate('가'.repeat(45), 40),
        validate: [m.validateJudgment({}), m.validateJudgment({decision:'watch'}),
                   m.validateJudgment({decision:'watch', conviction:3, memo:'x'.repeat(2001)}),
                   m.validateJudgment({decision:'watch', conviction:3, memo:'ok'})],
      };
    }""")
    ck.ok(res["notice"] == NOTICE, "pure: 고지 문구가 spec 4-2와 글자까지 같다")
    ck.ok(res["labels"] == ["매수 검토", "관망", "제외", "zzz"], f"pure: 판단 이름 {res['labels']}")
    ck.ok(res["kst"] == "2026-10-04" and res["kst_bad"] == "-", f"pure: 날짜는 KST {res['kst']}")
    ck.ok((res["add1"], res["add3"], res["add12"]) == ("2026-02-28", "2027-02-28", "2027-10-04"),
          f"pure: N개월 뒤(말일 보정) {res['add1']} {res['add3']} {res['add12']}")
    ck.ok(res["btn"] == ["hidden", "disabled", "disabled", "record", "recorded", "record"],
          f"pure: 버튼 상태(꺼짐·진행 중·종결·기록됨) {res['btn']}")
    ck.ok(res["band"][:2] == [None, None] and res["band"][2] == "판정 없이 기록한 판단(당시 판정: 실패)"
          and all(b and b.startswith("판정 없이 기록한 판단(당시 판정: ") for b in res["band"][2:]),
          f"pure: 판정 없이 기록 띠 {res['band']}")
    ck.ok(res["head"] == "당시 판정 정책 a2-v1 · 2026-10-04 판정", f"pure: 스냅샷 머리 {res['head']}")
    ck.ok(res["ch_bad"] == ["unavailable"] * 6, f"pure: 모양이 어긋난 변화 응답은 확인 불가(변화 없음 아님) {res['ch_bad']}")
    ck.ok(res["order"] == [3, 2, 0, 1], f"pure: ✅·⚠️ 근거 문단을 맨 위에 {res['order']}")
    ck.ok(res["sha"] == ["ok", "unavailable", "unavailable", "unavailable", "unavailable"],
          f"pure: 변화 응답 문단 해시가 스냅샷 같은 위치 문단과 다르면 확인 불가 {res['sha']}")
    t_jev, t_hi, t_lo = res["tips"]
    ck.ok(t_jev.startswith("당시 AI 판정 — ") and "AI 판정(JEV 모델)" in t_jev and TIP in t_jev,
          f"pure: JEV 배지 툴팁은 당시 AI 판정 {t_jev}")
    ck.ok(all(t.startswith("당시 규칙 판정 — ") and "규칙 판정(" in t and "JEV 호출 없음" in t and TIP in t
              and "AI 판정" not in t for t in (t_hi, t_lo)),
          f"pure: 규칙 판정 배지 툴팁은 출처가 앞뒤로 같다 {t_hi} / {t_lo}")
    ck.ok(res["trunc"] == "가" * 40 + "…", "pure: 질문 앞 40자")
    v = res["validate"]
    ck.ok(v[0] and "내 판단" in v[0] and v[1] and "내 확신" in v[1] and v[2] and "2,000" in v[2] and v[3] is None,
          f"pure: 입력 확인 {v}")
    ck.ok(not page.errors, f"pure: JS 오류 없음 {page.errors}")
    await ctx.close()


async def s_flag_off(browser, base, ck):
    print("[flag-off] JOURNAL_ENABLED 꺼짐: 탭·버튼 없음")
    fake = JournalApi(journal=404, acked=True)
    ctx, page = await open_app(browser, base, fake)
    ck.ok(await page.locator('.lnb-item[data-view="journal"]').count() == 1, "flag-off: 메뉴 항목은 DOM에 있다")
    ck.ok(not await page.is_visible('.lnb-item[data-view="journal"]'), "flag-off: 판단 일지 탭이 보이지 않는다")
    await done_answer(page, fake)
    ck.ok(await page.locator(".ev-msg .ev-journal, .ev-msg .ev-journal-link").count() == 0,
          "flag-off: 요약줄에 판단 기록 버튼 없음")
    ck.ok(len(fake.journal_calls()) == 1, f"flag-off: 기능 확인 1회만 {fake.journal_calls()}")
    await ctx.close()

    fake = JournalApi(journal=404)
    ctx, page = await open_app(browser, base, fake, hash_="journal")
    await page.wait_for_function("document.querySelector('.view.active')?.dataset.view === 'agent-chat'")
    ck.ok(True, "flag-off: #journal로 들어와도 상담 화면으로 돌린다")
    ck.ok(not page.errors, f"flag-off: JS 오류 없음 {page.errors}")
    await ctx.close()

    # 근거 모드 가짜 API의 빈 200({})은 일지 켜짐으로 보지 않는다(모양 확인)
    fake = JournalApi(acked=True)
    fake.journal = 200
    fake.respond_orig = fake.respond
    fake.respond = lambda m, p, q, b: (200, {}) if p == "/api/journal" else fake.respond_orig(m, p, q, b)
    ctx, page = await open_app(browser, base, fake)
    ck.ok(not await page.is_visible('.lnb-item[data-view="journal"]'), "flag-off: 모양이 다른 200 응답은 꺼짐으로 본다")
    await ctx.close()


async def s_probe_retry(browser, base, ck):
    print("[probe-retry] 기능 확인 5xx·네트워크 오류는 꺼짐으로 굳히지 않고 다음 진입 때 다시 확인한다")
    fake = JournalApi(journal_seq=[500])
    ctx, page = await open_app(browser, base, fake)
    ck.ok(not await page.is_visible('.lnb-item[data-view="journal"]'), "probe-retry: 첫 확인 500이면 탭은 아직 없다")
    await page.evaluate("location.hash = 'journal'")
    await page.wait_for_selector("#jr-list .jr-row")
    ck.ok(await page.locator(".view.active").get_attribute("data-view") == "journal",
          "probe-retry: 다시 들어오면 다시 확인해 일지를 연다")
    ck.ok(await page.is_visible('.lnb-item[data-view="journal"]'), "probe-retry: 확인되면 탭이 보인다")
    ck.ok(not page.errors, f"probe-retry: JS 오류 없음 {page.errors}")
    await ctx.close()

    # 네트워크 오류: 첫 GET /api/journal을 끊는다
    srv_fake = JournalApi()
    ctx2 = await browser.new_context(viewport={"width": 1280, "height": 900})
    ctx2.set_default_timeout(WAIT_MS)
    page = await ctx2.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    cut = {"n": 0}

    async def handle(route):
        url = route.request.url
        if not url.startswith(base):
            return await route.abort()
        path, _, query = url[len(base):].partition("?")
        if not path.startswith("/api/"):
            return await route.continue_()
        if path == "/api/journal" and route.request.method == "GET" and cut["n"] == 0:
            cut["n"] += 1
            return await route.abort()
        status, payload = srv_fake.respond(route.request.method, path, query, None)
        await route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))

    await page.route("**/*", handle)
    await page.goto(f"{base}/app.html#journal")
    await page.wait_for_selector("#jr-retry")
    ck.ok("다시" in await page.locator('.view[data-view="journal"]').inner_text()
          and await page.locator(".view.active").get_attribute("data-view") == "journal",
          "probe-retry: 네트워크 오류면 상담 화면으로 돌리지 않고 다시 시도 안내")
    await page.click("#jr-retry")
    await page.wait_for_selector("#jr-list .jr-row")
    ck.ok(True, "probe-retry: 다시 시도로 일지를 연다")
    ck.ok(not page.errors, f"probe-retry: JS 오류 없음 {page.errors}")
    await ctx2.close()

    print("[probe-404-cached] 404는 꺼짐으로 굳힌다(다시 묻지 않는다)")
    fake = JournalApi(journal=404)
    ctx, page = await open_app(browser, base, fake)
    await page.evaluate("location.hash = 'journal'")
    await page.wait_for_function("document.querySelector('.view.active')?.dataset.view === 'agent-chat' && location.hash !== '#journal'")
    await page.wait_for_timeout(200)
    ck.ok(len(fake.journal_calls()) == 1, f"probe-404-cached: 404 뒤로는 다시 묻지 않는다 {len(fake.journal_calls())}")
    await ctx.close()

    print("[evidence-retry] 근거 모드 확인 5xx는 굳히지 않는다(다음 상세 열기 때 다시 확인)")
    fake = JournalApi(notice_status=500, changes={"e1": (200, CHANGES_SAME)})
    ctx, page = await open_app(browser, base, fake, hash_="journal")
    await page.click('#jr-list .jr-row[data-id="e1"]')
    await page.wait_for_function("!document.querySelector('#jr-detail .jr-col-changes').innerText.includes('확인하는 중')")
    ck.ok(await page.locator("#jr-detail .jr-reask").count() == 0, "evidence-retry: 확인 실패면 다시 묻기 숨김")
    fake.notice_status = 200
    await page.click("#jr-back")
    await page.click('#jr-list .jr-row[data-id="e1"]')
    await page.wait_for_selector("#jr-detail .jr-actions .jr-reask")
    ck.ok(fake.n("GET", "/api/evidence/notice") == 2, f"evidence-retry: 다시 확인 {fake.n('GET', '/api/evidence/notice')}")
    ck.ok(not page.errors, f"evidence-retry: JS 오류 없음 {page.errors}")
    await ctx.close()


async def s_button(browser, base, ck):
    print("[button] 요약줄 판단 기록 버튼: 진행 중 비활성 → 종결 활성, 기록됨 링크")
    fake = JournalApi(acked=True, chat=ev.started("r1"),
                      runs={"r1": [ev.run("r1", "running"), ev.run("r1", "done", ev.DONE["statuses"], ev.DONE["extra"])]})
    ctx, page = await open_app(browser, base, fake)
    ck.ok(await page.is_visible('.lnb-item[data-view="journal"]'), "button: 판단 일지 탭이 보인다")
    await ev.turn_on(page)
    await ev.pick_company(page)
    await ev.ask(page)
    await page.wait_for_selector(".ev-msg .ev-journal")
    btn = page.locator(".ev-msg .ev-journal")
    ck.ok(await btn.is_disabled(), "button: 판정 중에는 비활성")
    tip = await page.locator(".ev-msg .ev-journal-wrap").get_attribute("title")
    ck.ok(tip == "판정이 끝난 뒤 기록할 수 있습니다", f"button: 비활성 툴팁 {tip}")
    await page.wait_for_function("document.querySelector('.ev-msg .ev-summary')?.innerText.startsWith('AI 판정: ✅')")
    ck.ok(await btn.is_enabled() and (await btn.inner_text()).strip() == "판단 기록", "button: 종결되면 활성")
    ck.ok(await page.locator(".ev-msg .ev-summary-text").inner_text() != "", "button: 요약줄 문구는 그대로")
    ck.ok(not page.errors, f"button: JS 오류 없음 {page.errors}")
    await ctx.close()

    print("[button-recorded] 스레드 복원: 이미 기록이 있으면 '기록됨 · 일지에서 보기' → 일지 상세")
    done = ev.run("r1", "done", ev.DONE["statuses"], ev.DONE["extra"], chat_id="h1")
    done["journal_entry_id"] = "e1"
    msgs = [{"id": "h1", "question": Q1, "answer": ev.ANSWER, "steps": [], "citations": [],
             "latest_evidence_run": {"id": "r1", "status": "done", "counts": done["counts"]}}]
    fake = JournalApi(active={"id": ev.CID, "title": "t"}, conv={"id": ev.CID, "messages": msgs, "msg_total": 1},
                      timeline={"conversation_id": ev.CID, "runs": [done]}, changes={"e1": (200, CHANGES_SAME)})
    ctx, page = await open_app(browser, base, fake)
    await page.wait_for_selector(".ev-msg .ev-journal-link")
    link = page.locator(".ev-msg .ev-journal-link")
    ck.ok((await link.inner_text()).strip() == "기록됨 · 일지에서 보기", "button-recorded: 기록됨 링크")
    ck.ok(await page.locator(".ev-msg .ev-journal").count() == 0, "button-recorded: 두 번째 기록 버튼 없음")
    await link.click()
    await page.wait_for_selector("#jr-detail:not(.hidden) .jr-col-snapshot")
    ck.ok(await page.locator(".view.active").get_attribute("data-view") == "journal", "button-recorded: 일지 탭으로 이동")
    ck.ok(fake.n("GET", "/api/journal/e1") == 1, "button-recorded: 그 기록의 상세를 연다")
    ck.ok(not page.errors, f"button-recorded: JS 오류 없음 {page.errors}")
    await ctx.close()


async def s_form(browser, base, ck):
    print("[form] 기록 양식: 고지·기본 선택 없음·기댄 문장·메모·확신·다시 볼 날짜 → 저장")
    fake = JournalApi(acked=True)
    ctx, page = await open_app(browser, base, fake)
    await done_answer(page, fake)
    await page.click(".ev-msg .ev-journal")
    form = page.locator(".ev-msg .jr-form")
    await form.wait_for()
    ck.ok((await form.locator(".jr-form-notice").inner_text()).strip() == NOTICE, "form: 양식 머리에 고정 고지(접지 않음)")
    heads = await form.inner_text()
    ck.ok(all(h in heads for h in ("내 판단", "기댄 문장", "내 메모", "내 확신", "다시 볼 날짜")), "form: 칸 이름")
    ck.ok(await form.locator("input.jr-decision:checked").count() == 0, "form: 내 판단 기본 선택 없음")
    ck.ok(await form.locator("input.jr-conviction:checked").count() == 0, "form: 내 확신 기본 선택 없음")
    dec = await form.locator("label.jr-decision-opt").all_inner_texts()
    ck.ok([d.strip() for d in dec] == ["매수 검토", "관망", "제외"], f"form: 판단 세 가지 {dec}")
    relied = await form.locator("input.jr-relied").evaluate_all("els => els.map(e => Number(e.value))")
    ck.ok(relied == [0, 1, 2, 4], f"form: 기댄 문장 후보(비주장 제외) {relied}")
    rb = [t.strip() for t in await form.locator(".jr-relied-list .ev-badge").all_inner_texts()]
    ck.ok(rb == ["✅", "✅", "⚠️", "❔"], f"form: 문장 옆 당시 배지 {rb}")
    attrs = await form.locator(".jr-relied-list .ev-badge").evaluate_all(
        "els => els.map(e => [e.getAttribute('role'), e.getAttribute('aria-label'), e.title])")
    ck.ok(all(r == "img" and a == t and t.startswith("당시 AI 판정 — ") and TIP in t for r, a, t in attrs),
          f"form: 기댄 문장 배지는 role=img·aria-label·spec 4-1 문구 {attrs[:1]}")
    ck.ok("확신도" not in await form.locator(".jr-conviction-row").inner_text(),
          "form: 내 확신 줄에 AI 판정 확신도를 두지 않는다")
    for w in FORBIDDEN_WORDS:
        ck.ok(w not in heads, f"form: '{w}' 없음")
    ck.ok(not re.search(r"✅\s*\d+\s*·|\d+\s*%", heads), "form: 배지 개수·비율로 판단을 제안하지 않는다")

    # 필수 칸 없이 저장 → 요청 없이 안내
    await form.locator(".jr-save").click()
    msg = (await form.locator(".jr-form-msg").inner_text()).strip()
    ck.ok("내 판단" in msg and fake.n("POST", "/api/journal") == 0, f"form: 필수 칸 누락 안내, 요청 없음 {msg}")

    await form.locator('input.jr-decision[value="watch"]').check()
    await form.locator('input.jr-conviction[value="3"]').check()
    await form.locator('input.jr-relied[value="1"]').check()
    await form.locator('input.jr-relied[value="2"]').check()
    await form.locator("textarea.jr-memo-input").fill("고객 집중이 걱정된다")
    ck.ok("11 / 2,000" in await form.locator(".jr-memo-count").inner_text(), "form: 메모 글자 수")
    ck.ok(await form.locator("textarea.jr-memo-input").get_attribute("maxlength") == "2000", "form: 메모 2,000자 제한")
    await form.locator('button.jr-quick[data-months="3"]').click()
    want3 = await page.evaluate("async () => { const m = await import('/js/journal.js'); return m.addMonths(m.todayKst(), 3); }")
    ck.ok(await form.locator("input.jr-review").input_value() == want3, f"form: 3개월 뒤 빠른 선택 {want3}")
    await form.locator('button.jr-quick[data-months="1"]').click()
    want1 = await page.evaluate("async () => { const m = await import('/js/journal.js'); return m.addMonths(m.todayKst(), 1); }")
    ck.ok(await form.locator("input.jr-review").input_value() == want1, "form: 1개월 뒤 빠른 선택")
    ext0, calls0 = len(page.external), len(fake.calls)
    fake.create_delay_s = 0.8
    await form.locator(".jr-save").click()
    await page.wait_for_timeout(200)
    ck.ok(await form.locator(".jr-save").is_disabled() and await form.locator(".jr-cancel").is_disabled(),
          "form: 저장 중에는 저장·취소 모두 비활성")
    await page.wait_for_selector(".ev-msg .ev-journal-link")
    body = fake.bodies("POST", "/api/journal")[0]
    ck.ok(body == {"run_id": "r1", "decision": "watch", "conviction": 3, "memo": "고객 집중이 걱정된다",
                   "relied_claims": [1, 2], "review_on": want1}, f"form: 요청 본문(스냅샷 없음) {body}")
    toast = await page.locator("#_toast_el").inner_text()
    ck.ok(toast == "판단을 기록했습니다 · 일지에서 보기", f"form: 저장 토스트 {toast}")
    ck.ok(await page.locator(".ev-msg .jr-form").count() == 0, "form: 저장하면 양식을 닫는다")
    ck.ok((await page.locator(".ev-msg .ev-journal-link").inner_text()).strip() == "기록됨 · 일지에서 보기",
          "form: 버튼이 기록됨 링크로 바뀐다")
    no_leak(ck, page, fake, "form", ext0, calls0)
    ck.ok(not page.errors, f"form: JS 오류 없음 {page.errors}")
    await ctx.close()


async def _form_error(browser, base, ck, name, status, payload):
    fake = JournalApi(acked=True, create=(status, payload))
    ctx, page = await open_app(browser, base, fake)
    await done_answer(page, fake)
    await page.click(".ev-msg .ev-journal")
    form = page.locator(".ev-msg .jr-form")
    await form.locator('input.jr-decision[value="exclude"]').check()
    await form.locator('input.jr-conviction[value="5"]').check()
    await form.locator("textarea.jr-memo-input").fill("남겨야 할 메모")
    await form.locator(".jr-save").click()
    await page.wait_for_function("document.querySelector('.ev-msg .jr-form-msg')?.innerText.trim().length > 0 "
                                 "|| document.querySelector('.ev-msg .ev-journal-link')")
    return ctx, page, fake, form


async def s_form_errors(browser, base, ck):
    print("[form-500] 저장 실패: 양식과 입력 유지")
    ctx, page, fake, form = await _form_error(browser, base, ck, "form-500", 500, {"detail": "기록을 저장하지 못했습니다."})
    ck.ok((await form.locator(".jr-form-msg").inner_text()).strip() == "저장하지 못했습니다. 입력은 그대로 있습니다.",
          "form-500: 저장 실패 문구")
    ck.ok(await form.locator("textarea.jr-memo-input").input_value() == "남겨야 할 메모"
          and await form.locator('input.jr-decision[value="exclude"]').is_checked(), "form-500: 입력 그대로")
    ck.ok(await form.locator(".jr-save").is_enabled() and await form.locator(".jr-cancel").is_enabled(),
          "form-500: 다시 저장·취소할 수 있다")
    await ctx.close()

    print("[form-network] 네트워크 오류도 같은 문구")
    fake = JournalApi(acked=True)
    ctx, page = await open_app(browser, base, fake)
    await done_answer(page, fake)
    await page.route("**/api/journal", lambda r: r.abort() if r.request.method == "POST" else r.fallback())
    await page.click(".ev-msg .ev-journal")
    form = page.locator(".ev-msg .jr-form")
    await form.locator('input.jr-decision[value="watch"]').check()
    await form.locator('input.jr-conviction[value="1"]').check()
    await form.locator(".jr-save").click()
    await page.wait_for_function("document.querySelector('.ev-msg .jr-form-msg')?.innerText.trim().length > 0")
    ck.ok((await form.locator(".jr-form-msg").inner_text()).strip() == "저장하지 못했습니다. 입력은 그대로 있습니다.",
          "form-network: 네트워크 오류 문구")
    await ctx.close()

    for name, status, payload in (("form-404", 404, {"detail": "판정 기록을 찾을 수 없습니다."}),
                                  ("form-409-gone", 409, {"detail": "원래 답변을 찾을 수 없어 기록할 수 없습니다."})):
        print(f"[{name}] 원 실행 사라짐")
        ctx, page, fake, form = await _form_error(browser, base, ck, name, status, payload)
        ck.ok((await form.locator(".jr-form-msg").inner_text()).strip()
              == "원래 판정 기록을 찾을 수 없어 기록할 수 없습니다", f"{name}: 실행 사라짐 문구")
        ck.ok(await form.locator(".jr-save").is_disabled(), f"{name}: 저장 버튼 비활성")
        await ctx.close()

    print("[form-409-exists] 이미 기록 있음 → 기록됨 링크")
    ctx, page, fake, form = await _form_error(browser, base, ck, "form-409-exists", 409,
                                              {"detail": "이 판정에는 이미 기록이 있습니다.", "entry_id": "e1"})
    ck.ok((await page.locator(".ev-msg .ev-journal-link").inner_text()).strip() == "기록됨 · 일지에서 보기",
          "form-409-exists: 기존 기록으로 링크")
    ck.ok(await page.locator(".ev-msg .jr-form").count() == 0, "form-409-exists: 양식을 닫는다")
    await ctx.close()

    print("[form-409-null] 이미 기록 있음(entry_id 없음) → 잠그지 않고 목록 새로고침 안내")
    ctx, page, fake, form = await _form_error(browser, base, ck, "form-409-null", 409,
                                              {"detail": "이 판정에는 이미 기록이 있습니다.", "entry_id": None})
    m = (await form.locator(".jr-form-msg").inner_text()).strip()
    ck.ok("이미" in m and "새로고침" in m and "찾을 수 없어" not in m, f"form-409-null: 안내 {m}")
    ck.ok(await form.locator(".jr-save").is_enabled(), "form-409-null: 저장 버튼을 잠그지 않는다")
    await ctx.close()

    print("[form-422] 검증 오류: 칸 이름만 안내, 입력 유지")
    ctx, page, fake, form = await _form_error(browser, base, ck, "form-422", 422,
                                              {"detail": [{"loc": ["body", "memo"], "msg": "too long"}]})
    m = (await form.locator(".jr-form-msg").inner_text()).strip()
    ck.ok(m.startswith("입력을 확인해 주세요") and "메모" in m, f"form-422: 안내 {m}")
    ck.ok(await form.locator("textarea.jr-memo-input").input_value() == "남겨야 할 메모", "form-422: 입력 그대로")
    await ctx.close()

    print("[form-retry-closes] 다시 판정하면 열린 양식을 닫는다(다른 실행)")
    fake = JournalApi(acked=True)
    ctx, page = await open_app(browser, base, fake)
    partial = ev.run("r1", "partial", ["supported", "unjudged", "no_evidence", "not_claim", "unjudged"],
                     {0: {"source_idx": 0}, 1: {"reason": "deadline"}, 4: {"reason": "deadline"}}, retryable=True)
    fake.chat = ev.started("r1")
    fake.runs["r1"] = [partial]
    fake.retry = ev.started("r9")
    fake.runs["r9"] = [ev.run("r9", "running"), ev.run("r9", "done", ev.DONE["statuses"], ev.DONE["extra"])]
    await ev.turn_on(page)
    await ev.pick_company(page)
    await ev.ask(page)
    await page.wait_for_selector(".ev-msg .ev-retry")
    await page.click(".ev-msg .ev-journal")
    await page.wait_for_selector(".ev-msg .jr-form")
    await page.click(".ev-msg .ev-retry")
    await page.wait_for_function("document.querySelector('.ev-msg .ev-summary')?.innerText.startsWith('AI 판정: ✅ 2')")
    ck.ok(await page.locator(".ev-msg .jr-form").count() == 0, "form-retry-closes: 양식이 닫힌다")
    ck.ok(await page.locator(".ev-msg .ev-journal").is_enabled(), "form-retry-closes: 새 실행에서 다시 기록 가능")
    ck.ok(not page.errors, f"form-retry-closes: JS 오류 없음 {page.errors}")
    await ctx.close()


async def s_list(browser, base, ck):
    print("[list-empty] 비어 있음")
    fake = JournalApi(entries={})
    ctx, page = await open_app(browser, base, fake, hash_="journal")
    await page.wait_for_selector("#jr-empty:not(.hidden)")
    ck.ok((await page.locator("#jr-empty").inner_text()).strip()
          == "아직 기록한 판단이 없습니다. 근거 모드 답변에서 판단 기록을 눌러 시작합니다.", "list-empty: 문구")
    ck.ok((await page.locator("#jr-view-notice").inner_text()).strip() == NOTICE, "list-empty: 탭 머리 고정 고지")
    ck.ok(not await page.is_visible("#jr-due-banner"), "list-empty: 다시 볼 기록 안내 없음")
    await ctx.close()

    print("[list] 목록·다시 볼 때 된 것·거르기")
    fake = JournalApi()
    ctx, page = await open_app(browser, base, fake, hash_="journal")
    ext0 = len(page.external)
    await page.wait_for_selector("#jr-list .jr-row")
    rows = page.locator("#jr-list .jr-row")
    ck.ok(await rows.count() == 2, "list: 두 줄")
    ck.ok((await page.locator("#jr-due-banner").inner_text()).strip() == "다시 볼 날짜가 지난 기록 1건",
          "list: 다시 볼 기록 안내")
    ck.ok(await page.evaluate("document.body.classList.contains('jr-due')"), "list: 일지 탭 점 표시")
    r1 = page.locator('#jr-list .jr-row[data-id="e1"]')
    t1 = await r1.inner_text()
    ck.ok("삼성전자" in t1 and Q1 in t1 and "관망" in t1 and "내 확신 3" in t1 and "2026-10-04" in t1,
          f"list: 행 내용(최근 판단·내 확신·기록일 KST) {t1}")
    ck.ok(await r1.locator(".jr-due-dot").count() == 1, "list: 다시 볼 날짜가 지난 행에 점")
    r2 = page.locator('#jr-list .jr-row[data-id="e2"]')
    t2 = await r2.inner_text()
    ck.ok(Q2[:40] + "…" in t2 and Q2 not in t2, "list: 질문 앞 40자")
    ck.ok(await r2.locator(".jr-due-dot").count() == 0, "list: 지나지 않은 행에는 점 없음")
    chip_cls = set(await page.locator("#jr-list .jr-chip").evaluate_all("els => els.map(e => e.className)"))
    ck.ok(len(chip_cls) == 1, f"list: 판단 칩은 모두 같은 모양(색으로 판단을 끌지 않는다) {chip_cls}")
    text = await page.locator('.view[data-view="journal"]').inner_text()
    for w in FORBIDDEN_WORDS:
        ck.ok(w not in text, f"list: '{w}' 없음")
    ck.ok("✅" not in text, "list: 목록에 배지 요약 숫자 없음")
    opts = await page.locator("#jr-f-company option").all_inner_texts()
    ck.ok(opts == ["전체 회사", "SK하이닉스", "삼성전자"], f"list: 회사 거르기 목록 {opts}")

    await page.check("#jr-f-due")
    await page.wait_for_function("document.querySelectorAll('#jr-list .jr-row').length === 1")
    ck.ok(fake.queries[-1].get("due") == "true", f"list: 다시 볼 때 된 것만 → due=true {fake.queries[-1]}")
    await page.uncheck("#jr-f-due")
    await page.select_option("#jr-f-company", "00164779")
    await page.wait_for_function("document.querySelector('#jr-list .jr-row')?.dataset.id === 'e2'")
    ck.ok(fake.queries[-1].get("corp_code") == "00164779", "list: 회사 거르기 → corp_code")
    await page.select_option("#jr-f-company", "")
    await page.click('.jr-f-decision button[data-decision="consider_buy"]')
    await page.wait_for_selector("#jr-empty:not(.hidden)")
    ck.ok(fake.queries[-1].get("decision") == "consider_buy", "list: 판단 칩 거르기 → decision")
    ck.ok((await page.locator("#jr-empty").inner_text()).strip() == "조건에 맞는 기록이 없습니다.",
          "list: 거른 결과가 없을 때 문구(빈 일지와 구분)")
    ck.ok(await page.get_attribute('.jr-f-decision button[data-decision="consider_buy"]', "aria-pressed") == "true",
          "list: 고른 칩 표시")
    await page.click('.jr-f-decision button[data-decision=""]')
    await page.wait_for_function("document.querySelectorAll('#jr-list .jr-row').length === 2")
    ck.ok("decision" not in fake.queries[-1], "list: 전체로 되돌리기")
    no_leak(ck, page, fake, "list", ext0)
    ck.ok(not page.errors, f"list: JS 오류 없음 {page.errors}")
    await ctx.close()


async def s_detail(browser, base, ck):
    print("[detail] 상세 세 칸·당시 근거·변화 칸(확인 중 → 결과)")
    fake = JournalApi(acked=True, changes={"e1": (200, changes_ok())}, changes_delay_s=0.8)
    ctx, page = await open_app(browser, base, fake, hash_="journal")
    ext0 = len(page.external)
    await page.click('#jr-list .jr-row[data-id="e1"]')
    await page.wait_for_selector("#jr-detail:not(.hidden) .jr-col-changes")
    ch = page.locator("#jr-detail .jr-col-changes")
    ck.ok("공시 변화를 확인하는 중…" in await ch.inner_text(), "detail: 변화 칸 확인 중")
    ck.ok(not await page.is_visible("#jr-list"), "detail: 목록은 접는다")
    j = await page.locator("#jr-detail .jr-col-judgment").inner_text()
    ck.ok(j.startswith("당시 판단") and "내 판단" in j and "내 메모" in j, "detail: 당시 판단 칸 머리·출처 표시")
    ups = page.locator("#jr-detail .jr-update")
    ck.ok(await ups.count() == 2, "detail: 판단 기록 두 줄(처음·다시 보기)")
    u0 = await ups.nth(0).inner_text()
    ck.ok("처음 판단" in u0 and "매수 검토" in u0 and "내 확신 4" in u0, f"detail: 처음 판단 {u0}")
    ck.ok("<script>x</script>" in u0, "detail: 메모는 이스케이프해 그대로")
    ck.ok("📈 삼성전자의 주요 제품은 메모리 반도체입니다." in u0 and "2025년 DX 부문" in u0, "detail: 기댄 문장 본문")
    ja = await page.locator("#jr-detail .jr-col-judgment .ev-badge").evaluate_all(
        "els => els.map(e => [e.getAttribute('role'), e.getAttribute('aria-label'), e.title])")
    ck.ok(len(ja) == 2 and all(r == "img" and a == t and t.startswith("당시 AI 판정 — ") and TIP in t for r, a, t in ja),
          f"detail: 판단 칸 기댄 문장 배지 role=img·aria-label·spec 4-1 문구 {ja[:1]}")
    u1 = await ups.nth(1).inner_text()
    ck.ok("다시 보기" in u1 and "관망" in u1, f"detail: 다시 보기 줄이 시간순 아래 {u1}")
    ck.ok("확신도" not in j, "detail: 내 확신 칸에 AI 판정 확신도를 섞지 않는다")
    s = page.locator("#jr-detail .jr-col-snapshot")
    st = await s.inner_text()
    ck.ok(st.startswith("당시 근거") and "당시 판정 정책 a2-provisional-2 · 2026-10-04 판정" in st, f"detail: 스냅샷 머리 {st[:80]}")
    ck.ok(Q1 in st, "detail: 질문")
    ck.ok((await s.locator(".ai-label").inner_text()).strip() == "AI 생성 답변", "detail: AI 생성 답변 표시")
    ck.ok("당시 AI 판정" in st, "detail: 당시 AI 판정 표시")
    b = [t.strip() for t in await s.locator(".ev-badge").all_inner_texts()]
    ck.ok(b == ["✅", "✅", "⚠️", "❔"], f"detail: 당시 배지 {b}")
    tip = await s.locator(".ev-badge").first.get_attribute("title")
    ck.ok(tip.startswith("당시 AI 판정") and "AI 판정(JEV 모델) · 검색된 공시 문단 기준이며 사실 여부를 보증하지 않습니다" in tip,
          f"detail: 배지 툴팁 머리에 당시 {tip}")
    await s.locator(".ev-badge").nth(1).click()
    pt = await s.locator(".ev-panel").inner_text()
    ck.ok("174조 8,877억원" in pt and "근거 문단 · 2025.12 사업보고서(접수번호 20260312000123)" in pt, "detail: 근거 문단 펼치기")
    ck.ok(not re.search(r"0\.\d", st + pt), "detail: 확률 숫자 없음")
    ck.ok(await s.locator(".jr-source-gone").count() == 0 and await s.locator(".jr-nojudge").count() == 0,
          "detail: 정상 기록에는 띠·삭제 안내 없음")

    await page.wait_for_function("!document.querySelector('#jr-detail .jr-col-changes').innerText.includes('확인하는 중')")
    ct = await ch.inner_text()
    ck.ok(ct.startswith("그 뒤 바뀐 것"), "detail: 변화 칸 머리")
    ck.ok("적재된 보고서가 바뀌었습니다: 당시 20260312000123 → 현재 20260315000999" in ct, f"detail: 보고서 교체 {ct}")
    ck.ok("당시 판정 기준 a2-provisional-2, 현재 a2-v1. 당시 배지는 당시 기준으로 보존됩니다." in ct, "detail: 정책 변화")
    ck.ok("이 비교는 이 앱에 적재된 사업보고서 문단만 봅니다. 그 밖의 새 공시는 확인하지 않습니다." in ct, "detail: 비교 범위 도움말")
    ps = await ch.locator(".jr-ch-passage").evaluate_all("els => els.map(e => e.dataset.status)")
    ck.ok(ps.count("gone") == 1 and ps.count("same") == 7, f"detail: 문단별 상태 {ps}")
    gone = await ch.locator('.jr-ch-passage[data-status="gone"]').inner_text()
    ck.ok("당시 문단과 같은 본문이 지금 적재된 문단에 없습니다" in gone and "174조" not in gone,
          "detail: gone 문단은 표시만(대체 본문 없음)")
    ck.ok("당시 문단이 지금도 적재되어 있습니다" in ct, "detail: same 문구")
    ck.ok(await ch.locator(".jr-reask").count() >= 1, "detail: gone이면 같은 질문 다시 묻기")
    for w in FORBIDDEN_WORDS:
        ck.ok(w not in await page.locator("#jr-detail").inner_text(), f"detail: '{w}' 없음")

    # 같은 질문 다시 묻기: 채팅 화면에 회사·질문을 채우기만 한다(보내지 않는다)
    await page.click("#jr-detail .jr-actions .jr-reask")
    await page.wait_for_function("document.querySelector('.view.active')?.dataset.view === 'agent-chat'")
    await page.wait_for_function("document.getElementById('chat-input').value.length > 0")
    ck.ok(await page.input_value("#chat-input") == Q1 and await page.input_value("#ev-company") == "삼성전자"
          and await page.is_checked("#ev-mode"), "detail: 같은 회사·질문을 근거 모드에 채운다")
    await page.wait_for_timeout(300)
    ck.ok(fake.n("POST", "/api/evidence/chat") == 0, "detail: 보내기는 사용자가 누른다")
    no_leak(ck, page, fake, "detail", ext0)
    fake.chat = ev.started("r7")
    fake.runs["r7"] = [ev.run("r7", "done", ev.DONE["statuses"], ev.DONE["extra"])]
    await page.click("#chat-send")
    await page.wait_for_selector(".ev-msg")
    sent = fake.bodies("POST", "/api/evidence/chat")[0]
    ck.ok(sent["corp_code"] == "00126380" and sent["question"] == Q1, f"detail: 보내면 같은 회사·질문 {sent}")
    ck.ok(not page.errors, f"detail: JS 오류 없음 {page.errors}")
    await ctx.close()

    print("[detail-nojudge] 판정 없이 기록·원 대화 삭제됨·변화 확인 불가")
    for label, resp in (("unavailable", (200, {"status": "unavailable"})), ("404", (404, {"detail": "Not Found"})),
                        ("500", (500, {"detail": "err"})), ("malformed", (200, {"status": "ok"})),
                        ("sha-mismatch", (200, changes_bad_sha()))):
        fake = JournalApi(flag=404, changes={"e2": resp})
        ctx, page = await open_app(browser, base, fake, hash_="journal")
        await page.click('#jr-list .jr-row[data-id="e2"]')
        await page.wait_for_selector("#jr-detail:not(.hidden) .jr-col-snapshot")
        await page.wait_for_function("!document.querySelector('#jr-detail .jr-col-changes').innerText.includes('확인하는 중')")
        ct = await page.locator("#jr-detail .jr-col-changes").inner_text()
        ck.ok("지금은 공시 변화를 확인할 수 없습니다" in ct, f"detail-nojudge[{label}]: 확인 불가 문구")
        ck.ok("변화 없음" not in ct and "지금도 적재되어" not in ct and "같습니다" not in ct
              and await page.locator("#jr-detail .jr-ch-passage").count() == 0,
              f"detail-nojudge[{label}]: 확인 불가가 변화 없음으로 보이지 않는다 {ct}")
        if label == "unavailable":
            s = page.locator("#jr-detail .jr-col-snapshot")
            ck.ok((await s.locator(".jr-nojudge").inner_text()).strip() == "판정 없이 기록한 판단(당시 판정: 실패)",
                  "detail-nojudge: 회색 띠")
            ck.ok((await s.locator(".jr-source-gone").inner_text()).strip()
                  == "원래 대화는 삭제되었습니다(이 기록의 스냅샷은 남아 있습니다)", "detail-nojudge: 원 대화 삭제 안내")
            ck.ok([t.strip() for t in await s.locator(".ev-badge").all_inner_texts()] == ["⊘"] * 4,
                  "detail-nojudge: ⊘를 ❔로 바꾸지 않는다")
            ck.ok(await page.locator("#jr-detail .jr-reask").count() == 0, "detail-nojudge: 근거 모드 꺼짐이면 다시 묻기 숨김")
            ck.ok(await page.is_visible("#jr-delete") and await page.is_visible("#jr-export"),
                  "detail-nojudge: 근거 모드 꺼져도 삭제·내보내기는 그대로")
        ck.ok(not page.errors, f"detail-nojudge[{label}]: JS 오류 없음 {page.errors}")
        await ctx.close()


async def s_revisit(browser, base, ck):
    print("[revisit] 다시 보기 기록 추가(덧붙이기만)")
    fake = JournalApi(changes={"e1": (200, CHANGES_SAME)})
    ctx, page = await open_app(browser, base, fake, hash_="journal")
    ext0 = len(page.external)
    await page.click('#jr-list .jr-row[data-id="e1"]')
    await page.wait_for_selector("#jr-detail:not(.hidden) .jr-update")
    ct = await page.locator("#jr-detail .jr-col-changes").inner_text()
    await page.wait_for_function("!document.querySelector('#jr-detail .jr-col-changes').innerText.includes('확인하는 중')")
    ct = await page.locator("#jr-detail .jr-col-changes").inner_text()
    ck.ok("적재된 보고서가 당시와 같습니다" in ct and "판정 기준은 당시와 같습니다" in ct, f"revisit: same 결과 {ct}")
    ck.ok(await page.locator("#jr-detail .jr-update button").count() == 0, "revisit: 판단 줄마다 고치기·지우기 버튼 없음")
    await page.click("#jr-revisit-open")
    form = page.locator("#jr-detail .jr-form")
    await form.wait_for()
    ck.ok((await form.locator(".jr-form-notice").inner_text()).strip() == NOTICE, "revisit: 양식 머리 고지")
    ck.ok(await form.locator("input.jr-decision:checked").count() == 0, "revisit: 기본 선택 없음")
    await form.locator('input.jr-decision[value="exclude"]').check()
    await form.locator('input.jr-conviction[value="2"]').check()
    await form.locator("textarea.jr-memo-input").fill("고객 집중 확인, 제외")
    fake.update_status = 500
    await form.locator(".jr-save").click()
    await page.wait_for_function("document.querySelector('#jr-detail .jr-form-msg')?.innerText.trim().length > 0")
    ck.ok((await form.locator(".jr-form-msg").inner_text()).strip() == "저장하지 못했습니다. 입력은 그대로 있습니다.",
          "revisit: 저장 실패 문구")
    ck.ok(await form.locator("textarea.jr-memo-input").input_value() == "고객 집중 확인, 제외", "revisit: 입력 유지")
    fake.update_status = 201
    await form.locator(".jr-save").click()
    await page.wait_for_function("document.querySelectorAll('#jr-detail .jr-update').length === 3")
    body = fake.bodies("POST", "/api/journal/e1/updates")[-1]
    ck.ok(body == {"decision": "exclude", "conviction": 2, "memo": "고객 집중 확인, 제외", "relied_claims": [],
                   "review_on": None}, f"revisit: 요청 본문 {body}")
    texts = await page.locator("#jr-detail .jr-update").all_inner_texts()
    ck.ok("매수 검토" in texts[0] and "관망" in texts[1] and "제외" in texts[2] and "고객 집중 확인, 제외" in texts[2],
          "revisit: 이전 판단은 그대로, 새 줄이 아래에")
    ck.ok(fake.n("PUT", "/api/journal/e1") == 0 and fake.n("PATCH", "/api/journal/e1") == 0, "revisit: 고치기 요청 없음")
    no_leak(ck, page, fake, "revisit", ext0)
    ck.ok(not page.errors, f"revisit: JS 오류 없음 {page.errors}")
    await ctx.close()


async def s_delete_export(browser, base, ck):
    print("[delete] 기록 삭제(확인)")
    fake = JournalApi()
    ctx, page = await open_app(browser, base, fake, hash_="journal")
    ext0 = len(page.external)
    await page.click('#jr-list .jr-row[data-id="e2"]')
    await page.wait_for_selector("#jr-detail:not(.hidden) .jr-col-snapshot")
    await page.click("#jr-delete")
    ck.ok(await page.is_visible("#jr-confirm"), "delete: 확인 대화상자")
    ck.ok("복구할 수 없습니다" in await page.locator("#jr-confirm").inner_text(), "delete: 되돌릴 수 없음 안내")
    await page.click("#jr-confirm-cancel")
    ck.ok(fake.n("DELETE", "/api/journal/e2") == 0, "delete: 취소하면 지우지 않는다")
    await page.click("#jr-delete")
    await page.click("#jr-confirm-ok")
    await page.wait_for_function("document.querySelectorAll('#jr-list .jr-row').length === 1")
    ck.ok(fake.n("DELETE", "/api/journal/e2") == 1, "delete: 기록 삭제 요청")
    ck.ok(await page.is_visible("#jr-list") and not await page.is_visible("#jr-detail"), "delete: 목록으로 돌아간다")

    print("[export] 내보내기 JSON")
    async with page.expect_download() as dl:
        await page.click("#jr-export")
    d = await dl.value
    ck.ok(d.suggested_filename == f"lumina-journal-{today_kst():%Y%m%d}.json", f"export: 파일명 {d.suggested_filename}")
    path = await d.path()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    ck.ok(data.get("notice") == NOTICE and data.get("count") == 1, "export: 서버가 준 JSON 그대로")

    print("[delete-all] 전체 삭제는 '전체 삭제' 입력 뒤에만")
    await page.click("#jr-delete-all")
    ck.ok(await page.is_visible("#jr-confirm-input"), "delete-all: 입력 칸")
    ck.ok(await page.is_disabled("#jr-confirm-ok"), "delete-all: 입력 전 비활성")
    await page.fill("#jr-confirm-input", "전체삭제")
    ck.ok(await page.is_disabled("#jr-confirm-ok"), "delete-all: 글자가 다르면 비활성")
    await page.fill("#jr-confirm-input", "전체 삭제")
    ck.ok(await page.is_enabled("#jr-confirm-ok"), "delete-all: 정확히 입력하면 활성")
    await page.click("#jr-confirm-ok")
    await page.wait_for_selector("#jr-empty:not(.hidden)")
    dels = [(m, p) for m, p, _ in fake.journal_calls("DELETE")]
    ck.ok(("DELETE", "/api/journal") in dels, f"delete-all: 전체 삭제 요청 {dels}")
    ck.ok(len(fake.entries) == 0, "delete-all: confirm=delete-all 쿼리로 불렀다(가짜 API가 지움)")
    no_leak(ck, page, fake, "delete", ext0)
    ck.ok(not page.errors, f"delete: JS 오류 없음 {page.errors}")
    await ctx.close()

    print("[delete-forgets-link] 지운 기록을 가리키던 요약줄 링크를 판단 기록 버튼으로 되돌린다")
    fake = JournalApi(acked=True)
    ctx, page = await open_app(browser, base, fake)
    await done_answer(page, fake)
    await page.click(".ev-msg .ev-journal")
    form = page.locator(".ev-msg .jr-form")
    await form.locator('input.jr-decision[value="watch"]').check()
    await form.locator('input.jr-conviction[value="3"]').check()
    await form.locator(".jr-save").click()
    await page.click(".ev-msg .ev-journal-link")
    await page.wait_for_selector("#jr-detail:not(.hidden) .jr-col-snapshot")
    await page.click("#jr-delete")
    await page.click("#jr-confirm-ok")
    await page.wait_for_selector("#jr-list:not(.hidden)")
    await page.click('.lnb-item[data-view="agent-chat"]')
    await page.wait_for_selector(".ev-msg .ev-journal")
    ck.ok(await page.locator(".ev-msg .ev-journal-link").count() == 0, "delete-forgets-link: 링크가 버튼으로")
    await ctx.close()


async def s_mobile(browser, base, ck):
    print("[mobile] 375px: 상세 세 칸이 위아래로, 가로 넘침 없음")
    fake = JournalApi(changes={"e1": (200, changes_ok())})
    ctx, page = await open_app(browser, base, fake, hash_="journal", width=375, height=740)
    await page.wait_for_selector("#jr-list .jr-row")
    await page.click('#jr-list .jr-row[data-id="e1"]')
    await page.wait_for_selector("#jr-detail:not(.hidden) .jr-col-changes")
    await page.wait_for_function("!document.querySelector('#jr-detail .jr-col-changes').innerText.includes('확인하는 중')")
    boxes = await page.evaluate("""() => ['.jr-col-judgment', '.jr-col-snapshot', '.jr-col-changes']
      .map(s => document.querySelector('#jr-detail ' + s).getBoundingClientRect()).map(r => [r.left, r.top])""")
    ck.ok(boxes[0][0] == boxes[1][0] == boxes[2][0] and boxes[0][1] < boxes[1][1] < boxes[2][1],
          f"mobile: 세 칸이 위아래로 {boxes}")
    over = await page.evaluate("""() => {
      const out = [];
      document.querySelectorAll('.view[data-view="journal"] *').forEach(el => {
        const r = el.getBoundingClientRect();
        if (r.width && r.right > window.innerWidth + 1) out.push(el.className || el.tagName);
      });
      return out.slice(0, 5);
    }""")
    ck.ok(not over, f"mobile: 가로로 넘치는 요소 없음 {over}")
    await ctx.close()

    fake = JournalApi(changes={"e1": (200, changes_ok())})
    ctx, page = await open_app(browser, base, fake, hash_="journal")
    await page.click('#jr-list .jr-row[data-id="e1"]')
    await page.wait_for_selector("#jr-detail:not(.hidden) .jr-col-changes")
    tops = await page.evaluate("""() => ['.jr-col-judgment', '.jr-col-snapshot', '.jr-col-changes']
      .map(s => Math.round(document.querySelector('#jr-detail ' + s).getBoundingClientRect().top))""")
    ck.ok(len(set(tops)) == 1, f"desktop: 세 칸이 나란히 {tops}")
    if os.environ.get("EV_SCREENSHOT"):
        await page.screenshot(path=os.environ["EV_SCREENSHOT"], full_page=True)
    await ctx.close()


async def main() -> int:
    srv, base = ev._serve()
    ck = ev.Checks()
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=ev._chromium())
        for scenario in (s_pure, s_flag_off, s_probe_retry, s_button, s_form, s_form_errors, s_list, s_detail, s_revisit,
                         s_delete_export, s_mobile):
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
