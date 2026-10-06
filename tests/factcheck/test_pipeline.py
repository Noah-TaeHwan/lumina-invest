"""팩트체커 판정 파이프라인 테스트: 가짜 JEV(ServiceJevClient.ask 모양)·가짜 저장소(search 인터페이스)·고정 XBRL.

복합 문장 부분 확인(XBRL 일치 + 나머지 미지지 → ❔), XBRL 불일치 → ⚠️(JEV 안 부름), ✅는 JEV 지지일 때만,
JEV 시간 초과·실패 → unjudged, 마감 → busy, 결과 순서·점진 산출, 검색 범위(기간·보고서 종류), 동시성 상한.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from app.services.factcheck import pipeline
from app.services.factcheck.pipeline import FactcheckPipeline, SentenceResult

SAMSUNG = "00126380"
NAMES = {SAMSUNG: ["삼성전자"], "00164779": ["SK하이닉스"], "00181712": ["SK"]}


def passage(text, *, rcept_no="20260814003699", period="2026H1", report_type="half", section="II. 사업의 내용", idx=0):
    return {"passage_id": f"{SAMSUNG}-{rcept_no}-{section}-{idx}", "corp_code": SAMSUNG, "rcept_no": rcept_no,
            "report_type": report_type, "report_nm": "반기보고서 (2026.06)", "period": period,
            "rcept_dt": rcept_no[:8], "section": section, "idx": idx, "text": text, "superseded": False,
            "is_correction": False}


class FakeStore:
    """search(corp_code, query, *, periods, report_types, k)만 흉내 낸다. 질의 문장 → 문단 목록."""

    def __init__(self, by_query=None, default=None, fail=False, latest=None, sync=False):
        self.by_query, self.default, self.fail, self.latest, self.calls = by_query or {}, default or [], fail, latest, []
        if latest is None:
            self.latest_period = None  # 속성이 없을 때와 같게(파이프라인이 사실 행으로 기준 시점을 정한다)
        if sync:
            self.search = self._search_sync

    def _hit(self, corp_code, query, periods, report_types, k):
        self.calls.append({"corp_code": corp_code, "query": query, "periods": periods,
                           "report_types": report_types, "k": k})
        if self.fail:
            raise ConnectionError("qdrant down")
        for key, ps in self.by_query.items():
            if key in query:
                return ps[:k]
        return self.default[:k]

    async def search(self, corp_code, query, *, periods=None, report_types=None, k=8):
        return self._hit(corp_code, query, periods, report_types, k)

    def _search_sync(self, corp_code, query, *, periods=None, report_types=None, k=8):
        return self._hit(corp_code, query, periods, report_types, k)

    async def latest_period(self, corp_code):  # noqa: F811 — latest가 있을 때만 쓰인다
        return self.latest


@dataclass
class R:
    """ServiceJevResult와 같은 모양의 응답."""

    ok: bool
    answers: dict | None
    key: str = "k"
    latency_ms: float = 1.0
    input_tokens: int = 100
    attempts: int = 1
    error: str | None = None
    error_code: str | None = None
    http_status: int | None = 200
    cached: bool = False


def _claim(state: str) -> str:
    return next(l[len("[Claim] "):] for l in state.splitlines() if l.startswith("[Claim] "))


class FakeJev:
    """주장 문장(부분 문자열) → 동작. 동작: ('support', j) / ('contradict', j) / 'nothing' / 'fail' / ('sleep', s) /
    ('wait', event). 기본은 nothing. 동시 실행 수 최댓값을 잰다."""

    def __init__(self, rules=None):
        self.rules, self.calls, self.active, self.max_active = rules or {}, [], 0, 0

    async def ask(self, state, questions, *, user_id, log_ctx=None, usage=None):
        claim = _claim(state)
        self.calls.append({"claim": claim, "state": state, "questions": questions, "user_id": user_id})
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            act = next((v for k, v in self.rules.items() if k in claim), "nothing")
            if isinstance(act, tuple) and act[0] == "sleep":
                await asyncio.sleep(act[1])
                act = act[2] if len(act) > 2 else "nothing"
            if isinstance(act, tuple) and act[0] == "wait":
                await act[1].wait()
                act = "nothing"
            if act == "fail":
                return R(False, None, error="HTTP 500", error_code="http_5xx", http_status=500)
            n = len(questions)
            s, c = [0.05] * n, [0.02] * n
            if isinstance(act, tuple) and act[0] == "support":
                s[act[1]] = 0.95
            if isinstance(act, tuple) and act[0] == "contradict":
                c[act[1]] = 0.9
            return R(True, {f"p{j}": {"supports": s[j - 1], "contradicts": c[j - 1]} for j in range(1, n + 1)})
        finally:
            self.active -= 1


def make(store=None, jev=None, facts=(), **kw):
    return FactcheckPipeline(store=store or FakeStore(), jev=jev or FakeJev(), facts=list(facts), names=NAMES,
                             user_id="anon:test", **kw)


def collect(p: FactcheckPipeline, text: str, as_of="2026H1") -> list[SentenceResult]:
    async def go():
        return [r async for r in p.check(SAMSUNG, text, as_of=as_of)]
    return asyncio.run(go())


def sub(x: dict, keys=("account_nm", "period", "fs_div", "amount")) -> dict:
    return {k: x[k] for k in keys}


def by_idx(results):
    return {r.idx: r for r in results}


Q2_TEXT = "2026년 2분기 매출액은 171.5조원이며 HBM 판매 호조가 실적을 이끌었다."
Q2_PASSAGE = passage(Q2_TEXT)


def test_result_shape_matches_contract():
    p = make(FakeStore(default=[Q2_PASSAGE]), FakeJev({"HBM": ("support", 0)}))
    (r,) = collect(p, "2026년 2분기 매출은 171.5조원으로 HBM 판매 호조 덕분이다.")
    assert set(vars(r)) == {"idx", "text", "category", "status", "evidence", "xbrl", "reason"}
    assert r.evidence == [{"rcept_no": "20260814003699", "report_nm": "반기보고서 (2026.06)", "period": "2026H1",
                           "section": "II. 사업의 내용", "text": Q2_TEXT, "superseded": False,
                           "is_correction": False}]


def test_supported_only_when_jev_supports_whole_sentence(facts):
    p = make(FakeStore(default=[Q2_PASSAGE]), FakeJev({"HBM": ("support", 0)}), facts)
    (r,) = collect(p, "2026년 2분기 매출은 171.5조원으로 HBM 판매 호조 덕분이다.")
    assert (r.category, r.status) == ("checked", "supported")
    assert sub(r.xbrl) == {"account_nm": "매출액", "period": "2026Q2", "fs_div": "CFS", "amount": 171499470000000}


def test_compound_sentence_partial_confirmation(facts):
    # 숫자는 XBRL과 일치하지만 나머지(HBM 판매 호조)는 판정에서 지지되지 않음 → ❔ 부분 확인
    p = make(FakeStore(default=[passage("2026년 2분기 매출액은 171.5조원이다.")]), FakeJev(), facts)
    (r,) = collect(p, "2026년 2분기 매출은 171.5조원으로 HBM 판매 호조 덕분이다.")
    assert (r.status, r.reason) == ("no_evidence", "xbrl_partial")
    assert r.xbrl["amount"] == 171499470000000


def test_xbrl_match_alone_amount_only_sentence_is_exact(facts):
    # 금액 주장만 + XBRL 일치 → 결정적 대조 ✅(xbrl_exact, JEV 없음). 설계 문서 Codex #2 예외(2026-10-06 개정)
    jev = FakeJev()
    p = make(FakeStore(default=[passage("반도체 사업 개요")]), jev, facts)
    (r,) = collect(p, "2026년 2분기 매출은 171.5조원이다.")
    assert (r.status, r.reason) == ("supported", "xbrl_exact") and jev.calls == []

def test_xbrl_mismatch_is_contradicted_without_jev(facts):
    jev = FakeJev({"172": ("support", 0)})
    store = FakeStore(default=[Q2_PASSAGE])
    p = make(store, jev, facts)
    (r,) = collect(p, "2026년 2분기 매출은 172조원으로 HBM 판매 호조 덕분이다.")
    assert (r.status, r.reason) == ("contradicted", "xbrl_mismatch")
    assert sub(r.xbrl) == {"account_nm": "매출액", "period": "2026Q2", "fs_div": "CFS", "amount": 171499470000000}
    assert jev.calls == []


def test_jev_contradiction(facts):
    p = make(FakeStore(default=[Q2_PASSAGE]), FakeJev({"감소": ("contradict", 0)}), facts)
    (r,) = collect(p, "HBM 판매는 2분기에 감소했다.")
    assert r.status == "contradicted" and r.evidence[0]["text"] == Q2_TEXT


def test_number_check_blocks_support_from_passage_without_the_number():
    # 문단에 주장 숫자가 없으면 JEV가 지지해도 ✅ 출처가 될 수 없다(evidence.judge.sys_decision 재사용)
    p = make(FakeStore(default=[passage("HBM 매출이 크게 늘었다.")]), FakeJev({"HBM": ("support", 0)}))
    (r,) = collect(p, "HBM 매출은 2분기에 12조원이었다.")
    assert r.status == "no_evidence"


def test_jev_timeout_is_unjudged(facts):
    p = make(FakeStore(default=[Q2_PASSAGE]), FakeJev({"HBM": ("sleep", 1.0)}), facts, jev_timeout_s=0.05)
    (r,) = collect(p, "2026년 2분기 매출은 171.5조원으로 HBM 판매 호조 덕분이다.")
    assert (r.status, r.reason) == ("unjudged", "timeout")
    assert r.xbrl is not None  # XBRL 대조 결과는 남긴다


def test_jev_failure_is_unjudged():
    p = make(FakeStore(default=[Q2_PASSAGE]), FakeJev({"HBM": "fail"}))
    (r,) = collect(p, "HBM 판매는 2분기에 늘었다.")
    assert (r.status, r.reason) == ("unjudged", "http_5xx")


def test_deadline_leaves_rest_busy():
    p = make(FakeStore(default=[Q2_PASSAGE]), FakeJev({"느린": ("sleep", 1.0)}), jev_timeout_s=5.0, deadline_s=0.1)
    rs = by_idx(collect(p, "느린 문장 2분기 HBM.\n빠른 문장 2분기 HBM."))
    assert (rs[0].status, rs[0].reason) == ("unjudged", "busy")
    assert rs[1].status == "no_evidence"


def test_skipped_sentences_do_not_call_store_or_jev():
    store, jev = FakeStore(default=[Q2_PASSAGE]), FakeJev()
    # 'SK는…' 문장은 맨 뒤(그 뒤의 이름 없는 문장은 회사 상속으로 건너뛰므로 사유별 건너뛰기를 보려면 마지막에)
    rs = collect(make(store, jev), "HBM 시장은 더 커질 것으로 보인다.\n"
                                   "목표주가 12만원을 제시한다.\nHBM 매출 비중은 40%다.\nSK는 2분기 매출 79.3조원을 기록했다.")
    got = [(r.category, r.status, r.reason) for r in sorted(rs, key=lambda r: r.idx)]
    assert got == [("opinion", "skipped", "opinion"), ("out_of_scope", "skipped", "market"),
                   ("derived", "skipped", "derived:other"), ("other_company", "skipped", "other_company:SK")]
    assert store.calls == [] and jev.calls == []


def test_search_scope_from_claim_period_and_growth():
    store = FakeStore(default=[Q2_PASSAGE])
    collect(make(store), "2026년 2분기 매출은 전년 동기 대비 130% 증가했다.\n작년 HBM 판매가 늘었다.\n삼성전자 HBM 판매가 늘었다.")
    c = {x["query"]: x for x in store.calls}
    growth = c["2026년 2분기 매출은 전년 동기 대비 130% 증가했다."]
    assert growth["report_types"] == ["preliminary"] and "2026Q2" in growth["periods"] and growth["k"] == 8
    last_year = c["작년 HBM 판매가 늘었다."]
    assert "2025" in last_year["periods"] and "2024" not in last_year["periods"] and last_year["report_types"] is None
    assert c["삼성전자 HBM 판매가 늘었다."]["periods"] is None  # 기간 불명 → 전체


def test_no_passages_is_no_evidence_without_jev():
    jev = FakeJev()
    (r,) = collect(make(FakeStore(default=[]), jev), "HBM 판매는 2분기에 늘었다.")
    assert (r.status, r.reason) == ("no_evidence", "no_passages") and jev.calls == []


def test_jev_request_uses_judge_state_and_user():
    jev = FakeJev()
    collect(make(FakeStore(default=[Q2_PASSAGE, passage("둘째 문단", idx=1)]), jev), "HBM 판매는 2분기에 늘었다.")
    (call,) = jev.calls
    assert call["state"].splitlines() == ["[Company] 삼성전자", "[Claim] HBM 판매는 2분기에 늘었다.",
                                          f"[Passage 1] {Q2_TEXT}", "[Passage 2] 둘째 문단"]
    assert list(call["questions"]) == ["p1", "p2"] and call["user_id"] == "anon:test"


def test_results_progressive_in_completion_order():
    release = None

    async def go():
        nonlocal release
        release = asyncio.Event()
        p = make(FakeStore(default=[Q2_PASSAGE]), FakeJev({"느린": ("wait", release), "빠른": "nothing"}))
        out = []
        async for r in p.check(SAMSUNG, "느린 문장 2분기 HBM.\n의견일 뿐이라고 판단한다.\n빠른 문장 2분기 HBM.",
                               as_of="2026H1"):
            out.append(r)
            if r.idx == 2:
                release.set()  # 빠른 문장 결과를 받은 뒤에야 느린 문장이 끝난다(점진 산출이 아니면 교착)
        return out

    out = asyncio.run(asyncio.wait_for(go(), 5))
    assert [r.idx for r in out] == [1, 2, 0]
    assert out[0].status == "skipped"


def test_every_sentence_exactly_once_and_text_preserved():
    text = "2분기 HBM 판매가 늘었다. 의견이라고 본다.\n- 3분기 HBM 판매가 줄었다."
    rs = collect(make(FakeStore(default=[Q2_PASSAGE])), text)
    assert sorted(r.idx for r in rs) == [0, 1, 2]
    assert [r.text for r in sorted(rs, key=lambda r: r.idx)] == ["2분기 HBM 판매가 늘었다.", "의견이라고 본다.",
                                                                  "3분기 HBM 판매가 줄었다."]


def test_per_request_concurrency_cap():
    jev = FakeJev({"HBM": ("sleep", 0.05)})
    # 연도 있는 분기만(연도 없는 '3분기'는 as_of 뒤라 당겨 풀려 검색·JEV 없이 ❔가 된다)
    text = "\n".join(f"2024년 {i}분기 HBM 판매 {i}." for i in range(1, 5)) + "\n" + "\n".join(
        f"2025년 {i}분기 HBM 판매." for i in range(1, 5))
    rs = collect(make(FakeStore(default=[passage("HBM")]), jev, per_request=4), text)
    assert len(rs) == 8 and jev.max_active <= 4 and len(jev.calls) == 8


def test_store_failure_raises():
    async def go():
        return [r async for r in make(FakeStore(fail=True)).check(SAMSUNG, "HBM 판매는 2분기에 늘었다.",
                                                                  as_of="2026H1")]
    with pytest.raises(pipeline.StoreUnavailable):
        asyncio.run(go())


def test_sync_store_supported():
    (r,) = collect(make(FakeStore(default=[Q2_PASSAGE], sync=True), FakeJev({"HBM": ("support", 0)})),
                   "2026년 2분기 HBM 판매 호조로 매출액은 171.5조원이다.")
    assert r.status == "supported"


def test_default_as_of_from_store_then_today(facts, monkeypatch):
    # as_of 없음 → store.latest_period → 없으면 오늘까지 끝난 분기. XBRL 행의 최신 기간으로 정하지 않는다(A1)
    async def go(pp, text):
        return [r async for r in pp.check(SAMSUNG, text)]
    p = make(FakeStore(default=[passage("x")], latest="2026H1"), FakeJev(), facts)
    (r,) = asyncio.run(go(p, "작년 매출은 300조원이다."))
    assert (r.status, r.xbrl["period"]) == ("contradicted", "2025")
    monkeypatch.setattr(pipeline, "_today", lambda: pipeline.date(2026, 10, 6))  # → 2026Q3
    p = make(FakeStore(default=[passage("x")]), FakeJev(), facts)
    (r,) = asyncio.run(go(p, "3분기 매출은 86.1조원이다."))
    assert r.xbrl is None  # 2026Q3(XBRL 없음). 행 최신 기간(2026Q2) 기준이면 2025Q3로 당겨 일치로 나왔다


def test_shifted_bare_quarter_never_contradicted(facts):
    # as_of 2026H1에서 '3분기'는 2025Q3로 당긴 해석 — 확신할 수 없으므로 ⚠️ 대신 최대 ❔(A1)
    jev = FakeJev({"3분기": ("contradict", 0)})
    (r,) = collect(make(FakeStore(default=[Q2_PASSAGE]), jev, facts), "3분기 매출은 90조원이다.")
    assert (r.status, r.reason) == ("no_evidence", "period_ambiguous")
    (r,) = collect(make(FakeStore(default=[Q2_PASSAGE]), FakeJev(), facts), "2025년 3분기 매출은 90조원이다.")
    assert (r.status, r.reason) == ("contradicted", "xbrl_mismatch")


def test_period_digits_removed_before_number_check():
    # '2분기'의 2가 문단에 없어도 금액이 맞으면 ✅ 출처가 될 수 있다(B1)
    p = make(FakeStore(default=[passage("매출액은 171.5조원이며 HBM 판매 호조가 실적을 이끌었다.")]),
             FakeJev({"HBM": ("support", 0)}))
    (r,) = collect(p, "2026년 2분기 매출은 171.5조원으로 HBM 판매 호조 덕분이다.")
    assert r.status == "supported"


def test_xbrl_result_keeps_selected_row_fields(facts):
    p = make(FakeStore(default=[Q2_PASSAGE]), FakeJev({"HBM": ("support", 0)}), facts)
    (r,) = collect(p, "2026년 2분기 매출은 171.5조원으로 HBM 판매 호조 덕분이다.")
    assert sub(r.xbrl, ("unit", "rcept_no", "cumulative", "is_correction", "column")) == {
        "unit": "원", "rcept_no": "20260814003699", "cumulative": False, "is_correction": False,
        "column": "thstrm"}


class SlowStore(FakeStore):
    """검색(임베딩 포함)이 느린 저장소. 동시에 도는 검색 수 최댓값을 잰다."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.active = self.max_active = 0

    async def search(self, corp_code, query, *, periods=None, report_types=None, k=8):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.02)
            return self._hit(corp_code, query, periods, report_types, k)
        finally:
            self.active -= 1


def test_search_runs_inside_per_request_limit():
    store = SlowStore(default=[passage("HBM")])
    text = "\n".join(f"2025년 {i}분기 HBM 판매." for i in range(1, 5)) + "\n" + "\n".join(
        f"2024년 {i}분기 HBM 판매." for i in range(1, 5))
    rs = collect(make(store, FakeJev(), per_request=2), text)
    assert len(rs) == 8 and len(store.calls) == 8 and store.max_active <= 2


def test_module_level_check_requires_configure():
    pipeline.configure(None)

    async def go():
        return [r async for r in pipeline.check(SAMSUNG, "HBM", as_of="2026H1")]
    with pytest.raises(RuntimeError):
        asyncio.run(go())
    p = make(FakeStore(default=[Q2_PASSAGE]))
    pipeline.configure(p)
    try:
        rs = asyncio.run(go())
        assert [r.idx for r in rs] == [] or all(isinstance(r, SentenceResult) for r in rs)
        async def go2():
            return [r async for r in pipeline.check(SAMSUNG, "2분기 HBM 판매가 늘었다.", as_of="2026H1")]
        (r,) = asyncio.run(go2())
        assert r.status == "no_evidence"
    finally:
        pipeline.configure(None)


def test_jev_triage_flag_passed_through():
    jev = FakeJev()
    p = make(FakeStore(default=[Q2_PASSAGE]), jev)
    (r,) = collect(p, "회사는 HBM 사업을 확대하고 있다.")
    assert (r.status, r.reason) == ("skipped", "no_fact_marker") and jev.calls == []


def test_for_user_shares_global_concurrency():
    # 요청마다 for_user 사본을 써도 서버 전체 동시성(D9)은 하나의 세마포어로 묶인다
    jev = FakeJev({"HBM": ("sleep", 0.05)})
    base = make(FakeStore(default=[passage("HBM")]), jev, global_limit=1)
    a, b = base.for_user("anon:a"), base.for_user("anon:b")
    assert (a.user_id, b.user_id, base.user_id) == ("anon:a", "anon:b", "anon:test")

    async def go():
        async def run(p):
            return [r async for r in p.check(SAMSUNG, "2분기 HBM 판매.\n3분기 HBM 판매.", as_of="2026H1")]
        return await asyncio.gather(run(a), run(b))

    ra, rb = asyncio.run(go())
    assert len(ra) == len(rb) == 2 and jev.max_active == 1
    assert {c["user_id"] for c in jev.calls} == {"anon:a", "anon:b"}


def test_force_check_judges_every_sentence_and_keeps_scope_flag():
    # '직접 검수 요청'(T3): 1단계 분류·검수 안 함을 건너뛰고 모두 2단계로. 범위 밖 표시는 category·reason에 남긴다
    store, jev = FakeStore(default=[Q2_PASSAGE]), FakeJev({"HBM": ("support", 0)})
    text = "회사는 HBM 사업을 확대하고 있다.\nSK는 HBM 판매가 늘었다.\nHBM 시장은 더 커질 것으로 보인다."

    async def go(force):
        return [r async for r in make(store, jev).check(SAMSUNG, text, as_of="2026H1", force_check=force)]

    off = by_idx(asyncio.run(go(False)))
    assert [off[i].status for i in range(3)] == ["skipped"] * 3 and jev.calls == []
    on = by_idx(asyncio.run(go(True)))
    assert len(jev.calls) == 3 and len(store.calls) == 3
    assert (on[0].category, on[0].status) == ("checked", "supported")
    assert (on[1].category, on[1].status, on[1].reason) == ("other_company", "supported", "other_company:SK")
    assert (on[2].category, on[2].status, on[2].reason) == ("out_of_scope", "supported", "forecast")


def test_module_level_check_passes_force_check():
    p = make(FakeStore(default=[Q2_PASSAGE]), FakeJev())
    pipeline.configure(p)
    try:
        async def go():
            return [r async for r in pipeline.check(SAMSUNG, "회사는 HBM 사업을 확대하고 있다.", as_of="2026H1",
                                                    force_check=True)]
        (r,) = asyncio.run(go())
        assert r.status == "no_evidence"
    finally:
        pipeline.configure(None)


# ---- T1 연동: 실제 FactcheckStore(메모리 Qdrant)·corp_names 사전 ----

def _real_store():
    import numpy as np
    from qdrant_client import AsyncQdrantClient

    from app.services.evidence.passages import Passage
    from app.services.factcheck import store as fstore
    rng = np.random.default_rng(3)
    vecs: dict[str, list[float]] = {}

    async def embed(text):
        return vecs.setdefault(text, rng.normal(size=8).tolist())

    st = fstore.FactcheckStore(AsyncQdrantClient(location=":memory:"), embed)

    def doc(rcept_no, report_type, period, **kw):
        return fstore.DocMeta(corp_code=SAMSUNG, rcept_no=rcept_no, report_type=report_type,
                              report_nm=f"{report_type} {period}", period=period, rcept_dt=rcept_no[:8], **kw)

    def ps(d, texts, section="II"):
        return [Passage(fstore.passage_id(SAMSUNG, d.rcept_no, section, i), SAMSUNG, d.rcept_no, section, i, t)
                for i, t in enumerate(texts)]

    async def load():
        cur = doc("20260730000001", "preliminary", "2026Q2", is_correction=True)
        old = doc("20260707000001", "preliminary", "2026Q2", superseded=True)
        await st.load_document(cur, ps(cur, ["현재값 문단 매출액 171.50조원"]) + ps(cur, ["정정 이력 171.00"], "CORR"))
        await st.load_document(old, ps(old, ["대체된 문단 매출액 171.00조원"]))
    asyncio.run(load())
    return st


def test_real_store_current_values_only_and_latest_period():
    st = _real_store()
    jev = FakeJev()

    async def go():
        p = make(st, jev)
        return [r async for r in p.check(SAMSUNG, "2분기 HBM 판매가 늘었다.")]  # as_of 없음 → store.latest_period
    (r,) = asyncio.run(go())
    assert r.status == "no_evidence"
    (call,) = jev.calls
    assert "현재값 문단" in call["state"]
    assert "대체된 문단" not in call["state"] and "정정 이력" not in call["state"]
    assert asyncio.run(st.latest_period(SAMSUNG)) == "2026Q2"


def test_pipeline_drops_superseded_and_history_passages_from_any_store():
    old = dict(passage("대체된 문단", rcept_no="old"), superseded=True)
    corr = dict(passage("정정 이력", idx=1), section="CORR")
    jev = FakeJev()
    collect(make(FakeStore(default=[old, corr, Q2_PASSAGE]), jev), "2분기 HBM 판매가 늘었다.")
    (call,) = jev.calls
    assert call["state"].splitlines()[2:] == [f"[Passage 1] {Q2_TEXT}"]


def test_build_pipeline_from_t1_corp_names(facts):
    from app.services.factcheck import corp_names
    entries = [{"corp_code": SAMSUNG, "corp_name": "삼성전자", "stock_code": "005930",
                "norm": corp_names.normalize("삼성전자")},
               {"corp_code": "00181712", "corp_name": "SK", "stock_code": "034730", "norm": "SK"},
               {"corp_code": "00164779", "corp_name": "SK하이닉스", "stock_code": "000660",
                "norm": corp_names.normalize("SK하이닉스")}]
    p = pipeline.build_pipeline(store=FakeStore(default=[Q2_PASSAGE]), jev=FakeJev(), corp_entries=entries,
                                facts=facts)
    assert p.names.display(SAMSUNG) == "삼성전자"
    rs = by_idx(collect(p, "SK는 2분기 매출 79.3조원을 기록했다.\n(주)삼성전자는 2분기 HBM 판매가 늘었다.\n"
                           "ＳＫ하이닉스는 HBM 1위다."))
    assert (rs[0].category, rs[0].reason) == ("other_company", "other_company:SK")
    assert rs[1].category == "checked"
    assert (rs[2].category, rs[2].reason) == ("other_company", "other_company:SK하이닉스")  # 전각도 정규화


# ---- PR #56 후속 1·3·4번 ----

def test_shifted_period_matching_xbrl_is_not_supported(facts):
    # as_of 2026H1의 '3분기'(2025Q3로 당김)가 XBRL과 맞아도 기간 해석을 확신할 수 없으므로 최대 ❔
    p = make(FakeStore(default=[passage("3분기 매출액은 86.1조원이다.")]), FakeJev({"86.1": ("support", 0)}), facts)
    (r,) = collect(p, "3분기 매출은 86.1조원이다.")
    assert (r.status, r.reason) == ("no_evidence", "period_ambiguous")


def test_force_check_other_company_numbers_not_refuted_by_selected_xbrl(facts):
    jev = FakeJev()
    p = make(FakeStore(default=[Q2_PASSAGE]), jev, facts)

    async def go():
        return [r async for r in p.check(SAMSUNG, "SK하이닉스의 2026년 2분기 매출은 79.3조원이다.", as_of="2026H1",
                                         force_check=True)]
    (r,) = asyncio.run(go())
    assert r.category == "other_company" and r.status != "contradicted" and r.xbrl is None
    assert len(jev.calls) == 1


def test_comparison_period_digits_removed_before_number_check():
    p = make(FakeStore(default=[passage("매출액은 333.6조원으로 10.9% 늘었다.")]), FakeJev({"대비": ("support", 0)}))
    (r,) = collect(p, "2025년 매출은 2024년 대비 10.9% 늘어 333.6조원이다.")
    assert r.status == "supported"


# ---- 실데이터 스모크 결함 1(교차 검수 반영): XBRL 일치 행으로 만든 근거 한 줄을 판정 문단 맨 앞에 ----

HYNIX = "00164779"
NOISE = [passage(t, rcept_no="20250314000001", period="2025", report_type="annual", idx=i) for i, t in enumerate(
    ["2025년 2월 자기주식 소각 결정", "기업어음 발행일자 2023년 09월 15일", "2025년 1월 이사회 개최",
     "2024년 12월 31일 기준 임원 현황", "2023년 3월 정기주주총회", "2025년 3월 배당 기준일",
     "2024년 6월 사채 발행", "2023년 11월 자기주식 취득"])]
SS_RCEPT, HY_RCEPT = "20250814003156", "20231114002574"
OP, REV = "dart_OperatingIncomeLoss", "ifrs-full_Revenue"
XBRL_HEAD = "[재무제표(XBRL) 값]"


def _fact(corp, account, amount, period, start, end, rcept_no, *, fs="CFS", cumulative=False, nm="영업이익",
          reprt_code="11012", bsns_year="2025", report_type="periodic"):
    return {"corp_code": corp, "period": period, "fs_div": fs, "account_id": account, "account_nm": nm,
            "amount": amount, "rcept_no": rcept_no, "period_start": start, "period_end": end,
            "value_kind": "duration", "cumulative": cumulative, "currency": "KRW", "unit": "원",
            "rcept_dt": rcept_no[:8], "is_correction": False, "report_type": report_type, "superseded": False,
            "rounding_unit": 1, "column": "thstrm_add" if cumulative else "thstrm", "reprt_code": reprt_code,
            "bsns_year": bsns_year}


SS_FACTS = [
    _fact(SAMSUNG, OP, 4_676_057_000_000, "2025Q2", "2025-04-01", "2025-06-30", SS_RCEPT),
    _fact(SAMSUNG, OP, 11_361_329_000_000, "2025H1", "2025-01-01", "2025-06-30", SS_RCEPT, cumulative=True),
    _fact(SAMSUNG, OP, 1_190_832_000_000, "2025Q2", "2025-04-01", "2025-06-30", SS_RCEPT, fs="OFS"),
    _fact(SAMSUNG, REV, 74_566_317_000_000, "2025Q2", "2025-04-01", "2025-06-30", SS_RCEPT, nm="매출액"),
    _fact(SAMSUNG, OP, 23_527_391_000_000, "2025Q3", "2025-01-01", "2025-09-30", "20251114002447", cumulative=True,
          reprt_code="11014"),
]
HY_FACTS = [_fact(HYNIX, OP, -1_791_961_000_000, "2023Q3", "2023-07-01", "2023-09-30", HY_RCEPT,
                  reprt_code="11014", bsns_year="2023")]


class TextJev(FakeJev):
    """state에서 지정한 글자를 담은 문단을 지지한다(문단 번호를 몰라도 되게)."""

    def __init__(self, needle):
        super().__init__()
        self.needle = needle

    async def ask(self, state, questions, *, user_id, log_ctx=None, usage=None):
        lines = [l for l in state.splitlines() if l.startswith("[Passage ")]
        hit = next((j for j, l in enumerate(lines) if self.needle in l), None)
        self.rules = {"": ("support", hit)} if hit is not None else {}
        return await super().ask(state, questions, user_id=user_id, log_ctx=log_ctx, usage=usage)


def _texts(jev):
    (call,) = jev.calls
    return [l.split("] ", 1)[1] for l in call["state"].splitlines() if l.startswith("[Passage ")]


def _check(corp, text, store, jev, facts):
    p = FactcheckPipeline(store=store, jev=jev, facts=facts, names=NAMES, user_id="anon:test")

    async def go():
        return [r async for r in p.check(corp, text, as_of="2026H1")]
    (r,) = asyncio.run(go())
    return r


# JEV 경로(금액 외 낱말이 있는 문장)에서 근거 줄이 판정 문단 0번인지 본다. 금액 주장만인 문장은 xbrl_exact로 JEV를 안 탄다
@pytest.mark.parametrize("corp, text, line", [
    (SAMSUNG, "삼성전자의 2025년 2분기 연결 영업이익은 4.68조원으로 HBM 덕분이다.",
     "삼성전자 연결 영업이익 2025년 2분기(3개월): 4,676,057백만원 — 접수번호 20250814003156"),
    (SAMSUNG, "삼성전자의 2025년 2분기 연결 영업이익은 4.7조원으로 HBM 덕분이다.",
     "삼성전자 연결 영업이익 2025년 2분기(3개월): 4,676,057백만원 — 접수번호 20250814003156"),
    (SAMSUNG, "삼성전자의 2025년 상반기 연결 영업이익은 11.4조원으로 HBM 덕분이다.",     # 누적 행
     "삼성전자 연결 영업이익 2025년 상반기(누적): 11,361,329백만원 — 접수번호 20250814003156"),
    (SAMSUNG, "삼성전자의 2025년 2분기 별도 영업이익은 1.19조원으로 HBM 덕분이다.",     # OFS 힌트
     "삼성전자 별도 영업이익 2025년 2분기(3개월): 1,190,832백만원 — 접수번호 20250814003156"),
    (SAMSUNG, "삼성전자의 2025년 1~3분기 연결 영업이익은 23.5조원으로 HBM 덕분이다.",    # 3분기 누적 행
     "삼성전자 연결 영업이익 2025년 1~3분기(누적): 23,527,391백만원 — 접수번호 20251114002447"),
    (HYNIX, "SK하이닉스의 2023년 3분기 연결 영업손실은 1.79조원으로 HBM 탓이었다.",   # 음수 행
     "SK하이닉스 연결 영업이익 2023년 3분기(3개월): -1,791,961백만원 — 접수번호 20231114002574"),
    (SAMSUNG, "삼성전자의 2025년 2분기 연결 영업이익률은 6.3%로 HBM 덕분이다.",
     "삼성전자 연결 영업이익률 2025년 2분기(3개월): 6.27% — 접수번호 20250814003156"),
])
def test_xbrl_line_is_first_passage(corp, text, line):
    store, jev = FakeStore(default=NOISE), TextJev(XBRL_HEAD)
    r = _check(corp, text, store, jev, SS_FACTS + HY_FACTS)
    texts = _texts(jev)
    assert texts[0] == f"{XBRL_HEAD} {line}"
    assert len(texts) == 8 and texts[1:] == [p["text"] for p in NOISE[:7]]   # 전체 k 유지, 중복 없음
    assert len(store.calls) == 1                                             # Qdrant 추가 호출 없음(검색 1회)
    assert r.status == "supported"
    ev = r.evidence[0]
    assert (ev["section"], ev["text"]) == ("XBRL", texts[0])
    assert ev["rcept_no"] in line


def test_xbrl_evidence_names_original_report():
    jev = TextJev(XBRL_HEAD)
    r = _check(SAMSUNG, "삼성전자의 2025년 2분기 연결 영업이익은 4.68조원이다.", FakeStore(default=NOISE), jev, SS_FACTS)
    assert {k: r.evidence[0][k] for k in ("report_nm", "period")} == {"report_nm": "반기보고서 (2025.06)",
                                                                     "period": "2025Q2"}


def test_partial_match_gets_no_xbrl_line():
    # 영업이익은 맞고 순이익은 XBRL 행이 없다(partial) → 앵커 없음, dense만 → ✅ 아님
    store, jev = FakeStore(default=NOISE), TextJev(XBRL_HEAD)
    r = _check(SAMSUNG, "삼성전자의 2025년 2분기 연결 영업이익은 4.7조원, 순이익은 6.7조원이다.", store, jev, SS_FACTS)
    assert _texts(jev) == [p["text"] for p in NOISE]
    assert (r.status, r.reason) == ("no_evidence", "xbrl_partial")


def test_separate_only_is_capped_without_search_or_jev():
    # 연결/별도 표시 없음 + 연결 불일치·별도 일치 → ❔ separate_only, 검색·JEV 호출 없음
    store, jev = FakeStore(default=NOISE), TextJev(XBRL_HEAD)
    r = _check(SAMSUNG, "삼성전자의 2025년 2분기 영업이익은 1.2조원이다.", store, jev, SS_FACTS)
    assert (r.status, r.reason) == ("no_evidence", "separate_only")
    assert r.xbrl["fs_div"] == "OFS"
    assert store.calls == [] and jev.calls == []


def test_no_xbrl_match_uses_dense_only():
    store, jev = FakeStore(default=NOISE), FakeJev()
    collect(make(store, jev, SS_FACTS), "2025년 2분기 HBM 판매가 늘었다.")
    assert _texts(jev) == [p["text"] for p in NOISE]


def test_wrong_number_still_contradicted_by_xbrl():
    store, jev = FakeStore(default=NOISE), FakeJev()
    (r,) = collect(make(store, jev, SS_FACTS), "삼성전자의 2025년 2분기 연결 영업이익은 8.4조원이다.")
    assert (r.status, r.reason) == ("contradicted", "xbrl_mismatch") and jev.calls == [] and store.calls == []


# ---- 실데이터 스모크 결함 2(교차 검수 반영): 상대 기간 표현만 있으면 바로 ❔, 검색·JEV 0회 ----

def test_relative_only_period_skips_search_and_jev(facts):
    from conftest import prelim_rows
    rows = facts + prelim_rows()   # 2026Q2 잠정 영업이익 89.49조
    for claim in ("89.5조원", "84.6조원"):
        store, jev = FakeStore(default=[Q2_PASSAGE]), FakeJev({"같은 분기": ("support", 0)})
        (r,) = collect(make(store, jev, rows), f"같은 분기 연결 영업이익은 {claim}이다.")
        assert (r.status, r.reason) == ("no_evidence", "period_ambiguous"), claim
        assert store.calls == [] and jev.calls == [], claim


def test_current_quarter_word_resolves_and_xbrl_mismatch_is_contradicted(facts):
    # '당분기'는 as_of(2026H1)로 2026Q2가 된다 → ambiguous 아님, XBRL(89.49조)과 불일치 → ⚠️(조기 반환)
    store, jev = FakeStore(default=[Q2_PASSAGE]), FakeJev()
    (r,) = collect(make(store, jev, facts), "당분기 영업이익은 8.4조원이다.")
    assert (r.status, r.reason) == ("contradicted", "xbrl_mismatch")
    assert r.xbrl["period"] == "2026Q2" and store.calls == [] and jev.calls == []


def test_explicit_period_with_same_quarter_phrase_unchanged(facts):
    # 명시 기간이 있으면 '같은 분기'가 함께 있어도 ambiguous가 아니다 → XBRL 불일치 ⚠️ 그대로
    store, jev = FakeStore(default=[Q2_PASSAGE]), FakeJev()
    (r,) = collect(make(store, jev, facts), "2026년 2분기 영업이익은 89.5조원이고 같은 분기 매출은 172조원이다.")
    assert (r.status, r.reason) == ("contradicted", "xbrl_mismatch")


@pytest.mark.parametrize("label, cumulative, instant, want", [
    ("2025Q2", False, False, "2025년 2분기(3개월)"), ("2025Q3", True, False, "2025년 1~3분기(누적)"),
    ("2025H1", True, False, "2025년 상반기(누적)"), ("2025", False, False, "2025년 연간"),
    ("2025", False, True, "2025년 말"), ("2026H1", False, True, "2026년 6월 말"), ("2025Q3", False, True, "2025년 3분기 말"),
])
def test_xbrl_line_period_text(label, cumulative, instant, want):
    assert pipeline._period_text(label, cumulative, instant) == want


# ---- 재검수: 잠정실적 행의 근거 줄 머리말, separate_only + 다른 계정 불일치 ----

def _prelim_q2(correction: bool):
    from conftest import prelim_rows
    return [dict(r, is_correction=correction) for r in prelim_rows()]


@pytest.mark.parametrize("correction, head", [(True, "[잠정실적 정정 공시 값]"), (False, "[잠정실적 공시 값]")])
def test_preliminary_row_line_is_not_labeled_as_financial_statement(correction, head):
    store, jev = FakeStore(default=NOISE), TextJev("잠정실적")
    r = _check(SAMSUNG, "삼성전자의 2026년 2분기 확정 연결 영업이익은 89.5조원이다.", store, jev, _prelim_q2(correction))
    first = _texts(jev)[0]
    assert first == f"{head} 삼성전자 연결 영업이익 2026년 2분기(3개월): 89,490,000백만원 — 접수번호 20260730000001"
    assert XBRL_HEAD not in first
    assert r.evidence[0]["report_nm"] == "영업(잠정)실적(공정공시)" and r.evidence[0]["section"] == "XBRL"


def test_periodic_row_line_keeps_financial_statement_head():
    jev = TextJev(XBRL_HEAD)
    _check(SAMSUNG, "삼성전자의 2025년 2분기 연결 영업이익은 4.68조원으로 HBM 덕분이다.", FakeStore(default=NOISE), jev,
           SS_FACTS)
    assert _texts(jev)[0].startswith(XBRL_HEAD)


def test_separate_only_with_other_account_mismatch_is_contradicted():
    # 영업이익은 별도로만 맞고(separate_only), 매출은 연결·별도 모두 틀림 → ⚠️ xbrl_mismatch(검색·JEV 없음)
    rows = SS_FACTS + [_fact(SAMSUNG, REV, 50_000_000_000_000, "2025Q2", "2025-04-01", "2025-06-30", SS_RCEPT,
                             fs="OFS", nm="매출액")]
    store, jev = FakeStore(default=NOISE), FakeJev()
    r = _check(SAMSUNG, "삼성전자의 2025년 2분기 영업이익은 1.2조원, 매출은 100조원이다.", store, jev, rows)
    assert (r.status, r.reason) == ("contradicted", "xbrl_mismatch")
    assert r.xbrl["account_nm"] == "매출액" and store.calls == [] and jev.calls == []


# ---- 개선 1: 두 계정 모두 XBRL 일치 → 근거 줄을 한 문단으로 합친다 ----

def test_all_items_matched_lines_joined_into_one_passage():
    store, jev = FakeStore(default=NOISE), TextJev(XBRL_HEAD)
    r = _check(SAMSUNG, "삼성전자의 2025년 2분기 연결 매출은 74.6조원, 영업이익은 4.7조원으로 HBM 덕분이다.", store, jev,
               SS_FACTS)
    first = _texts(jev)[0]
    assert "74,566,317백만원" in first and "4,676,057백만원" in first
    assert first.count(XBRL_HEAD) == 2 and len(_texts(jev)) == 8
    assert r.status == "supported" and r.evidence[0]["section"] == "XBRL"


def test_one_of_two_items_mismatch_still_contradicted():
    store, jev = FakeStore(default=NOISE), TextJev(XBRL_HEAD)
    r = _check(SAMSUNG, "삼성전자의 2025년 2분기 연결 매출은 74.6조원, 영업이익은 8.4조원이다.", store, jev, SS_FACTS)
    assert (r.status, r.reason) == ("contradicted", "xbrl_mismatch") and jev.calls == []


# ---- 개선 2: 연도를 당겨 푼 분기 문장은 검색·JEV 없이 바로 ❔ ----

def test_shifted_sentence_skips_search_and_jev(facts):
    for text in ("3분기 매출은 86.1조원이다.", "3분기 매출은 90조원이다."):
        store, jev = FakeStore(default=[Q2_PASSAGE]), FakeJev({"3분기": ("support", 0)})
        (r,) = collect(make(store, jev, facts), text)
        assert (r.status, r.reason) == ("no_evidence", "period_ambiguous"), text
        assert store.calls == [] and jev.calls == [], text


# ---- 개선 3: 앞 문장 기간 상속 ----

def _run_text(text, store, jev, facts):
    p = FactcheckPipeline(store=store, jev=jev, facts=facts, names=NAMES, user_id="anon:test")

    async def go():
        return [r async for r in p.check(SAMSUNG, text, as_of="2026H1")]
    return by_idx(asyncio.run(go()))


def test_inherit_previous_sentence_period_mismatch():
    rs = _run_text("삼성전자의 2025년 2분기 연결 매출은 74.6조원이다. 같은 분기 연결 영업이익은 89.5조원이다.",
                   FakeStore(default=NOISE), TextJev(XBRL_HEAD), SS_FACTS)
    r = rs[1]
    assert (r.status, r.reason) == ("contradicted", "xbrl_mismatch,period_inherited:2025Q2")
    assert (r.xbrl["period"], r.xbrl["amount"]) == ("2025Q2", 4_676_057_000_000)


def test_inherit_previous_sentence_period_supported():
    rs = _run_text("삼성전자의 2025년 2분기 연결 매출은 74.6조원이다. 같은 분기 연결 영업이익은 4.7조원이다.",
                   FakeStore(default=NOISE), TextJev(XBRL_HEAD), SS_FACTS)
    assert (rs[1].status, rs[1].reason) == ("supported", "xbrl_exact,period_inherited:2025Q2")  # 금액 주장만


def test_inherit_skips_sentences_without_period():
    rs = _run_text("삼성전자의 2025년 2분기 연결 매출은 74.6조원이다.\nHBM 판매가 늘었다.\n"
                   "같은 분기 연결 영업이익은 89.5조원이다.", FakeStore(default=NOISE), FakeJev(), SS_FACTS)
    assert (rs[2].status, rs[2].reason) == ("contradicted", "xbrl_mismatch,period_inherited:2025Q2")


@pytest.mark.parametrize("text", [
    "같은 분기 연결 영업이익은 4.7조원이다.",                                   # 앞 문장 없음
    "2025년 2분기 매출은 74.6조원, 2024년 2분기 매출은 74.1조원이다. 같은 분기 영업이익은 4.7조원이다.",  # 기간 둘
    "3분기 매출은 86.1조원이다. 같은 분기 영업이익은 12.2조원이다.",              # 앞 문장 기간이 당긴 해석
])
def test_inherit_not_possible_stays_ambiguous(text, facts):
    store, jev = FakeStore(default=NOISE), FakeJev()
    rs = _run_text(text, store, jev, facts + SS_FACTS)
    last = rs[max(rs)]
    assert (last.status, last.reason) == ("no_evidence", "period_ambiguous")


def test_inherit_uses_nearest_previous_period():
    rs = _run_text("2024년 2분기 매출은 74.1조원이다. 삼성전자의 2025년 2분기 연결 매출은 74.6조원이다. "
                   "같은 분기 연결 영업이익은 89.5조원이다.", FakeStore(default=NOISE), FakeJev(), SS_FACTS)
    assert rs[2].reason == "xbrl_mismatch,period_inherited:2025Q2"


# ---- 금액 주장만으로 된 문장의 결정적 ✅(xbrl_exact, JEV 0회) ----

SS = "삼성전자의 2025년 2분기 연결 영업이익은"


@pytest.mark.parametrize("text", [
    f"{SS} 4.7조원이다.",
    f"{SS} 4.68조원이다.",
    f"{SS} 약 4.7조원이다.",
    f"{SS} 4.7조원을 기록했다.",
    f"{SS} 4.7조원으로 집계됐다.",
    "2025년 2분기 연결 영업이익은 4.7조원이었다.",
    "삼성전자의 2025년 2분기 연결 매출은 74.6조원, 영업이익은 4.7조원이다.",
    "삼성전자의 2025년 2분기 연결 매출은 74.6조원 그리고 영업이익은 4.7조원입니다.",
    "삼성전자의 2025년 2분기 연결 영업이익률은 6.27%였다.",
    "SK하이닉스의 2023년 3분기 연결 영업손실은 1.79조원이었다.",
])
def test_amount_only_sentence_is_exact_without_jev(text):
    corp = HYNIX if text.startswith("SK하이닉스") else SAMSUNG
    store, jev = FakeStore(default=NOISE), FakeJev()
    r = _check(corp, text, store, jev, SS_FACTS + HY_FACTS)
    assert (r.status, r.reason) == ("supported", "xbrl_exact"), text
    assert jev.calls == [] and store.calls == [], text
    assert r.evidence[0]["section"] == "XBRL" and r.evidence[0]["text"].startswith(XBRL_HEAD)


@pytest.mark.parametrize("text", [
    f"{SS} 4.7조원으로 사상 최대다.",
    f"{SS} 4.7조원으로 전년 대비 감소했다.",
    f"{SS} 4.7조원으로 HBM 덕분이다.",
    f"{SS} 4.7조원으로 흑자다.",
    f"{SS} 약간 4.7조원이다.",
    "삼성전자의 2025년 2분기 연결 기준 영업이익은 4.7조원이다.",
    f"{SS} 4.7조원 집계됐다.",                                      # '집계됐다'는 '로 집계됐다'만
    "삼성전자 SK하이닉스 2025년 2분기 연결 영업이익은 4.7조원이다.",   # 다른 회사 이름은 지우지 않는다
])
def test_extra_words_go_to_jev(text):
    store, jev = FakeStore(default=NOISE), FakeJev()
    r = _check(SAMSUNG, text, store, jev, SS_FACTS)
    assert (r.status, r.reason) == ("no_evidence", "xbrl_partial"), text
    assert len(jev.calls) == 1, text


def test_exact_not_used_for_mismatch_partial_separate_ambiguous_shifted(facts):
    cases = [
        (f"{SS} 8.4조원이다.", ("contradicted", "xbrl_mismatch")),
        ("삼성전자의 2025년 2분기 연결 영업이익은 4.7조원, 순이익은 6.7조원이다.", ("no_evidence", "xbrl_partial")),
        ("삼성전자의 2025년 2분기 영업이익은 1.2조원이다.", ("no_evidence", "separate_only")),
        ("같은 분기 연결 영업이익은 4.7조원이다.", ("no_evidence", "period_ambiguous")),
    ]
    for text, want in cases:
        r = _check(SAMSUNG, text, FakeStore(default=NOISE), FakeJev(), SS_FACTS)
        assert (r.status, r.reason) == want, text
    (r,) = collect(make(FakeStore(default=NOISE), FakeJev(), facts), "3분기 매출은 86.1조원이다.")  # shifted
    assert (r.status, r.reason) == ("no_evidence", "period_ambiguous")


def test_exact_not_used_for_force_checked_out_of_scope():
    jev = TextJev(XBRL_HEAD)
    p = FactcheckPipeline(store=FakeStore(default=NOISE), jev=jev, facts=SS_FACTS, names=NAMES, user_id="anon:test")

    async def go(text):
        return [r async for r in p.check(SAMSUNG, text, as_of="2026H1", force_check=True)]
    (r,) = asyncio.run(go("SK하이닉스의 2025년 2분기 연결 영업이익은 4.7조원이다."))   # 다른 회사
    assert r.reason != "xbrl_exact" and "xbrl_exact" not in (r.reason or "")
    (r,) = asyncio.run(go("삼성전자의 2025년 2분기 연결 영업이익은 4.7조원으로 예상된다."))   # 범위 밖(전망)
    assert "xbrl_exact" not in (r.reason or "") and len(jev.calls) >= 1


def test_exact_with_inherited_period_keeps_reason():
    rs = _run_text("삼성전자의 2025년 2분기 연결 매출은 74.6조원이다. 같은 분기 연결 영업이익은 4.7조원이다.",
                   FakeStore(default=NOISE), FakeJev(), SS_FACTS)
    assert (rs[0].status, rs[0].reason) == ("supported", "xbrl_exact")
    assert (rs[1].status, rs[1].reason) == ("supported", "xbrl_exact,period_inherited:2025Q2")


def test_jev_sentence_gets_inherited_period_in_parentheses():
    jev = FakeJev()
    rs = _run_text("삼성전자의 2025년 2분기 연결 매출은 74.6조원이다. 같은 분기 연결 영업이익은 4.7조원으로 HBM 덕분이다.",
                   FakeStore(default=NOISE), jev, SS_FACTS)
    (call,) = jev.calls
    assert call["claim"] == "같은 분기(2025년 2분기) 연결 영업이익은 4.7조원으로 HBM 덕분이다."
    assert rs[1].text == "같은 분기 연결 영업이익은 4.7조원으로 HBM 덕분이다."   # 화면 원문은 그대로


# ---- 상속 기간의 단위 맞춤 ----

H1_FACTS = SS_FACTS + [_fact(SAMSUNG, REV, 153_706_800_000_000, "2025H1", "2025-01-01", "2025-06-30", SS_RCEPT,
                             cumulative=True, nm="매출액")]


@pytest.mark.parametrize("text, reason", [
    ("2025년 상반기 연결 매출은 153.7조원이다. 같은 분기 연결 영업이익은 4.7조원이다.", "period_ambiguous"),
    ("2025년 2분기 연결 매출은 74.6조원이다. 같은 반기 연결 영업이익은 11.4조원이다.", "period_ambiguous"),
    ("2025년 2분기 연결 매출은 74.6조원이다. 같은 해 연결 영업이익은 43.6조원이다.", "period_ambiguous"),
    ("2025년 상반기 연결 매출은 153.7조원이다. 같은 반기 연결 영업이익은 11.4조원이다.", "xbrl_exact,period_inherited:2025H1"),
    ("2025년 상반기 연결 매출은 153.7조원이다. 같은 기간 연결 영업이익은 11.4조원이다.", "xbrl_exact,period_inherited:2025H1"),
    ("2025년 2분기 연결 매출은 74.6조원이다. 동 기간 연결 영업이익은 4.7조원이다.", "xbrl_exact,period_inherited:2025Q2"),
])
def test_inherit_requires_matching_unit(text, reason):
    rs = _run_text(text, FakeStore(default=NOISE), FakeJev(), H1_FACTS)
    assert rs[1].reason == reason, text


@pytest.mark.parametrize("text, reason", [
    (f"{SS} 4.7조원으로 시장 기대치를 넘었다.", "forecast"),   # '기대' → 범위 밖(전망 표지)
    (f"{SS} 4.7조원으로 컨센서스를 웃돌았다.", "market"),      # '컨센서스' → 범위 밖(시장)
])
def test_expectation_words_are_out_of_scope_not_exact(text, reason):
    store, jev = FakeStore(default=NOISE), FakeJev()
    r = _check(SAMSUNG, text, store, jev, SS_FACTS)
    assert (r.status, r.reason) == ("skipped", reason) and jev.calls == []


# ---- PR #63 적대 검수: 결정적 ✅(xbrl_exact)의 틀린 ✅ 경로 막기 ----

def _not_exact(text, facts=SS_FACTS, corp=SAMSUNG):
    r = _check(corp, text, FakeStore(default=NOISE), FakeJev(), facts)
    assert "xbrl_exact" not in (r.reason or ""), (text, r.status, r.reason)
    return r


@pytest.mark.parametrize("text", [
    f"{SS} - 4.68조원이다.",                      # 띄어 쓴 ASCII 빼기표
    f"{SS} 4.68조원으로 前年比 減少.",              # 한자
    f"{SS} 4.68조원(↓)이다.",                     # 기호
    f"{SS} 4.68조원이다!?",
])
def test_residual_symbols_block_exact(text):
    r = _not_exact(text)
    assert r.status != "supported"


@pytest.mark.parametrize("text", [f"{SS} −4.68조원이다.", f"{SS} △4.68조원이다."])
def test_unicode_minus_and_triangle_are_negative(text):
    # '−'(U+2212)·'△'(DART 음수 표기)는 음수 → 실제 흑자 4.68조와 어긋남 ⚠️(결정적 ✅ 아님)
    r = _not_exact(text)
    assert (r.status, r.reason) == ("contradicted", "xbrl_mismatch")


def test_unicode_minus_loss_matches_negative_fact():
    r = _check(HYNIX, "SK하이닉스의 2023년 3분기 연결 영업이익은 △1.79조원이었다.", FakeStore(default=NOISE), FakeJev(),
               HY_FACTS)
    assert (r.status, r.reason) == ("supported", "xbrl_exact")
    r = _check(HYNIX, "SK하이닉스의 2023년 3분기 연결 영업이익은 −1.79조원이었다.", FakeStore(default=NOISE), FakeJev(),
               HY_FACTS)
    assert (r.status, r.reason) == ("supported", "xbrl_exact")


@pytest.mark.parametrize("text", [
    "삼성전자의 2025년 1분기 및 2분기 연결 영업이익은 4.7조원이다.",
    "삼성전자의 2025년 1분기, 2분기 연결 영업이익은 4.7조원이다.",
    "삼성전자의 2025년 1분기도 2분기도 연결 영업이익은 4.7조원이다.",
    "삼성전자의 2025년 6월 말 연결 영업이익은 4.7조원이다.",        # 월 표기는 결정적 ✅ 금지
    "삼성전자의 2025년 6월 연결 영업이익은 4.7조원이다.",
])
def test_period_set_must_match_items(text):
    _not_exact(text)


@pytest.mark.parametrize("text", [
    "삼성전자의 2025년 2분기 연결 및 별도 영업이익은 4.68조원이다.",
    "삼성전자의 2025년 2분기 영업이익은 4.68조원(별도)이다.",
    "삼성전자의 2025년 2분기 연결 영업이익은 4.68조원, 별도도 같다.",
])
def test_fs_words_must_match_items(text):
    _not_exact(text)


def test_fs_word_matching_item_is_exact():
    r = _check(SAMSUNG, "삼성전자의 2025년 2분기 별도 영업이익은 1.19조원이다.", FakeStore(default=NOISE), FakeJev(), SS_FACTS)
    assert (r.status, r.reason) == ("supported", "xbrl_exact")


@pytest.mark.parametrize("text", [f"{SS} 4조원이다.", f"{SS} 약 4조원이다.", f"{SS} 5조원이다.",
                                  f"{SS} 4.6조원이다."])   # 4.676조를 버림해야 맞는 값(반올림이면 4.7)
def test_coarse_amount_goes_to_jev(text):
    # 유효숫자 1자리·버림 일치는 결정적 ✅ 금지(number_check 자체는 그대로 — ⚠️로 바꾸지 않는다)
    r = _not_exact(text)
    assert r.reason != "xbrl_mismatch" or text.endswith("5조원이다.")


@pytest.mark.parametrize("text", [f"{SS} 4.7조원이다.", f"{SS} 4.68조원이다.",
                                  "삼성전자의 2025년 2분기 연결 매출은 74.6조원이다.",
                                  "삼성전자의 2025년 2분기 연결 영업이익률은 6.27%였다."])
def test_precise_amount_still_exact(text):
    r = _check(SAMSUNG, text, FakeStore(default=NOISE), FakeJev(), SS_FACTS)
    assert (r.status, r.reason) == ("supported", "xbrl_exact"), text


def test_no_inherit_from_other_company_sentence():
    rs = _run_text("SK하이닉스의 2025년 2분기 연결 매출은 22.2조원이다. 같은 분기 연결 영업이익은 4.68조원이다.",
                   FakeStore(default=NOISE), FakeJev(), SS_FACTS)
    assert rs[0].category == "other_company"
    # 회사 상속이 먼저 걸린다(이름 없는 문장 → 앞 문장이 다른 회사): 기간 상속도 하지 않고 건너뜀
    assert (rs[1].status, rs[1].reason) == ("skipped", "other_company_inherited:SK하이닉스")
    # 이름이 있는 문장이면 기간만 막힌다(❔)
    rs = _run_text("SK하이닉스의 2025년 2분기 연결 매출은 22.2조원이다. 삼성전자의 같은 분기 연결 영업이익은 4.68조원이다.",
                   FakeStore(default=NOISE), FakeJev(), SS_FACTS)
    assert (rs[1].status, rs[1].reason) == ("no_evidence", "period_ambiguous")


def test_company_name_keeps_following_particle():
    _not_exact("2025년 2분기 연결 영업이익은 삼성전자보다 4.68조원이다.")


def test_account_name_with_period_word_not_exact():
    net = "ifrs-full_ProfitLoss"
    rows = [_fact(SAMSUNG, net, 34_451_351_000_000, "2025", "2025-01-01", "2025-12-31", "r1", nm="당기순이익",
                  reprt_code="11011")]
    _not_exact("삼성전자의 2025년 연결 반기순이익은 34.5조원이다.", rows)


# ---- 주어 없는 문장의 회사 상속: 가장 가까운 '회사 이름 있는' 앞 문장의 회사를 잇는다 ----

SK_S = "SK하이닉스의 2025년 2분기 연결 영업이익은 9.2조원이다."
PLAIN = "2025년 2분기 연결 영업이익은 4.7조원이다."


def _calls_for(store, jev, text):
    return [c for c in store.calls if c["query"] == text], [c for c in jev.calls if c["claim"] == text]


def test_unnamed_sentence_after_other_company_is_skipped():
    store, jev = FakeStore(default=NOISE), FakeJev()
    rs = _run_text(f"{SK_S} {PLAIN}", store, jev, SS_FACTS)
    assert (rs[0].status, rs[0].category) == ("skipped", "other_company")
    assert (rs[1].status, rs[1].category, rs[1].reason) == ("skipped", "other_company",
                                                             "other_company_inherited:SK하이닉스")
    assert store.calls == [] and jev.calls == []


def test_unnamed_sentence_after_selected_company_is_judged():
    rs = _run_text(f"삼성전자의 2025년 2분기 연결 매출은 74.6조원이다. {PLAIN}", FakeStore(default=NOISE), FakeJev(),
                   SS_FACTS)
    assert (rs[1].status, rs[1].reason) == ("supported", "xbrl_exact")


def test_named_sentence_does_not_inherit():
    rs = _run_text(f"{SK_S} 삼성전자의 {PLAIN}", FakeStore(default=NOISE), FakeJev(), SS_FACTS)
    assert (rs[1].status, rs[1].reason) == ("supported", "xbrl_exact")


def test_two_companies_before_unnamed_is_subject_ambiguous():
    store, jev = FakeStore(default=NOISE), FakeJev()
    rs = _run_text("삼성전자와 SK하이닉스는 2025년 2분기에 흑자였다. 영업이익은 4.7조원이다.", store, jev, SS_FACTS)
    assert (rs[1].status, rs[1].category, rs[1].reason) == ("skipped", "other_company", "subject_ambiguous")
    assert _calls_for(store, jev, "영업이익은 4.7조원이다.") == ([], [])


def test_nearest_named_sentence_is_used_across_unnamed_ones():
    store, jev = FakeStore(default=NOISE), FakeJev()
    rs = _run_text(f"{SK_S} 2025년 2분기 연결 매출은 22.2조원이다. {PLAIN}", store, jev, SS_FACTS)
    for i in (1, 2):
        assert (rs[i].status, rs[i].reason) == ("skipped", "other_company_inherited:SK하이닉스"), i
    assert store.calls == [] and jev.calls == []


def test_first_unnamed_sentence_is_selected_company():
    rs = _run_text(f"{PLAIN} {SK_S}", FakeStore(default=NOISE), FakeJev(), SS_FACTS)
    assert (rs[0].status, rs[0].reason) == ("supported", "xbrl_exact")


@pytest.mark.parametrize("pronoun", ["같은 회사의 ", "동사의 ", "이 회사의 ", "당사의 "])
def test_pronoun_subject_inherits_like_unnamed(pronoun):
    rs = _run_text(f"{SK_S} {pronoun}{PLAIN}", FakeStore(default=NOISE), FakeJev(), SS_FACTS)
    assert (rs[1].status, rs[1].reason) == ("skipped", "other_company_inherited:SK하이닉스")


def test_subject_inherited_sentence_does_not_pass_period():
    # 회사를 이어받아 건너뛴 문장은 기간도 물려주지 않는다(그 회사의 기간)
    rs = _run_text(f"{SK_S} 2025년 2분기 연결 매출은 22.2조원이다. 삼성전자의 같은 분기 연결 영업이익은 4.7조원이다.",
                   FakeStore(default=NOISE), FakeJev(), SS_FACTS)
    assert (rs[2].status, rs[2].reason) == ("no_evidence", "period_ambiguous")


def test_force_check_judges_subject_inherited_sentence_without_selected_xbrl():
    jev = FakeJev()
    p = FactcheckPipeline(store=FakeStore(default=NOISE), jev=jev, facts=SS_FACTS, names=NAMES, user_id="anon:test")

    async def go():
        return [r async for r in p.check(SAMSUNG, f"{SK_S} {PLAIN}", as_of="2026H1", force_check=True)]
    rs = by_idx(asyncio.run(go()))
    assert rs[1].category == "other_company" and rs[1].xbrl is None
    assert "other_company_inherited:SK하이닉스" in rs[1].reason and "xbrl_exact" not in rs[1].reason
    assert any(c["claim"] == PLAIN for c in jev.calls)


# ---- #66 검수: 상속 기준 앞 문장은 '주어 자리에 회사 이름 + 숫자 있음'일 때만 ----

COMMON_NAMES = {**NAMES, "00450728": ["도움"], "00994994": ["나노"], "00120021": ["LG"], "00000301": ["레이"],
                "00000302": ["대덕"], "00106641": ["기아"]}
SS_REV = "삼성전자의 2026년 2분기 연결 매출액은 171.5조원이다."
PLAIN_Q2 = "2026년 2분기 연결 영업이익은 89.5조원이다."


def _run_names(text, names, facts):
    p = FactcheckPipeline(store=FakeStore(default=NOISE), jev=FakeJev(), facts=facts, names=names, user_id="anon:test")

    async def go():
        return [r async for r in p.check(SAMSUNG, text, as_of="2026H1")]
    return by_idx(asyncio.run(go()))


@pytest.mark.parametrize("middle", [
    "환율 상승도 이익에 도움이 됐다.",
    "나노 공정 비중이 늘었다.",
    "애플과 LG 등 주요 고객사 수요가 견조했다.",
    "레이 트레이싱 수요가 늘었다.",
    "대덕 연구단지 투자도 이어졌다.",
    "모바일 부문은 기아 등 자동차 고객 확대로 성장했다.",
    "모바일 부문은 기아 등 자동차 고객 확대로 20% 성장했다.",   # 숫자는 있지만 이름이 주어 자리 밖
])
def test_common_word_names_do_not_change_subject(middle, facts):
    from conftest import prelim_rows
    rs = _run_names(f"{SS_REV} {middle} {PLAIN_Q2}", COMMON_NAMES, facts + prelim_rows())
    assert "inherited" not in (rs[2].reason or "") and rs[2].reason != "subject_ambiguous", middle
    assert (rs[2].status, rs[2].reason) == ("supported", "xbrl_exact"), middle


def test_other_company_without_number_does_not_change_subject(facts):
    # 한계(고정): 숫자 없는 다른 회사 문장은 회사 문맥을 바꾸지 않는다 → 다음 이름 없는 숫자 문장은 선택 회사로 판정
    from conftest import prelim_rows
    rs = _run_names(f"SK하이닉스는 견조한 실적을 냈다. {PLAIN_Q2}", COMMON_NAMES, facts + prelim_rows())
    assert "inherited" not in (rs[1].reason or "")
    assert (rs[1].status, rs[1].reason) == ("supported", "xbrl_exact")


def test_other_company_number_sentence_still_inherits_past_plain_middle():
    rs = _run_names(f"{SK_S} 환율 상승도 이익에 도움이 됐다. {PLAIN}", COMMON_NAMES, SS_FACTS)
    assert (rs[2].status, rs[2].reason) == ("skipped", "other_company_inherited:SK하이닉스")


# ---- 근사 표시어 금액은 조기 ⚠️ 하지 않는다(유효숫자 2자리 미만이면 결정적 ✅도 아님) ----

HY_Q1_OP = [_fact(HYNIX, OP, 37_610_000_000_000, "2026Q1", "2026-01-01", "2026-03-31", "r26q1")]


def test_approx_amount_not_early_contradicted():
    store, jev = FakeStore(default=NOISE), FakeJev()
    r = _check(HYNIX, "SK하이닉스의 2026년 1분기 연결 영업이익은 약 40조원이다.", store, jev, HY_Q1_OP)
    assert r.status != "contradicted" and r.reason != "xbrl_exact"
    r = _check(HYNIX, "SK하이닉스의 2026년 1분기 연결 영업이익은 약 50조원이다.", FakeStore(default=NOISE), FakeJev(),
               HY_Q1_OP)
    assert (r.status, r.reason) == ("contradicted", "xbrl_mismatch")


# ---- 목록 머리말: '정리하면·요약하면·살펴보면 + 다음과/아래와 같다' 꼴은 not_claim:lead ----

@pytest.mark.parametrize("text", [
    "두 회사의 실적을 요약하면 아래와 같다.",
    "주요 수치를 정리하면 다음과 같습니다.",
    "주요 수치를 정리하면 아래 표와 같다.",
    "실적을 요약하면 다음과 같음.",
    "두 회사 실적을 살펴보면 아래와 같다(단위: 조원).",
    "핵심을 요약하면 다음과 같이 정리된다.",
    "삼성전자와 SK하이닉스의 실적을 살펴보면 다음과 같다:",
])
def test_list_lead_sentences_skipped(text):
    store, jev = FakeStore(default=NOISE), FakeJev()
    (r,) = collect(make(store, jev), text)
    assert (r.status, r.category, r.reason) == ("skipped", "opinion", "not_claim:lead"), text
    assert store.calls == [] and jev.calls == []


def test_list_lead_with_number_not_skipped():
    (r,) = collect(make(FakeStore(default=NOISE), FakeJev()), "2분기 실적을 요약하면 아래와 같다.")
    assert r.status != "skipped"
