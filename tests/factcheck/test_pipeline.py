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


def test_xbrl_match_alone_is_not_supported(facts):
    # 숫자 하나 일치를 문장 전체 ✅로 올리지 않는다(Codex #2): JEV가 지지하지 않으면 ❔
    p = make(FakeStore(default=[passage("반도체 사업 개요")]), FakeJev(), facts)
    (r,) = collect(p, "2026년 2분기 매출은 171.5조원이다.")
    assert (r.status, r.reason) == ("no_evidence", "xbrl_partial")


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
    rs = collect(make(store, jev), "HBM 시장은 더 커질 것으로 보인다.\nSK는 2분기 매출 79.3조원을 기록했다.\n"
                                   "목표주가 12만원을 제시한다.\nHBM 매출 비중은 40%다.")
    got = [(r.category, r.status, r.reason) for r in sorted(rs, key=lambda r: r.idx)]
    assert got == [("opinion", "skipped", "opinion"), ("other_company", "skipped", "other_company:SK"),
                   ("out_of_scope", "skipped", "market"), ("derived", "skipped", "derived:other")]
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
    text = "\n".join(f"{i}분기 HBM 판매 {i}." for i in range(1, 5)) + "\n" + "\n".join(
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
