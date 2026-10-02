# tests/evidence/test_passage_store.py
"""문단 저장소(P3, Qdrant evidence_passages): 평가용 top_k와 같은 순위, corp_code 필터, 결정적 포인트 id,
재적재 시 건너뛰기·덮어쓰기·남은 문단 삭제, 회사 목록, 앱 시작 시 연결.

Qdrant는 qdrant-client의 메모리 모드(:memory:)를, 임베딩은 고정 벡터를 돌려주는 가짜를 쓴다(외부 호출 없음).
"""
import asyncio

import numpy as np
import pytest
from qdrant_client import AsyncQdrantClient

from app.routes import evidence as evidence_routes
from app.services.evidence import retrieve, store
from app.services.evidence.dart import Corp
from app.services.evidence.passages import Passage

SAMSUNG = Corp("00126380", "삼성전자", "005930")
HYNIX = Corp("00164779", "SK하이닉스", "000660")
DIM = 16


def _passages(corp: Corp, n: int, rcept_no: str = "20260312000123", tag: str = "") -> list[Passage]:
    return [Passage(f"{corp.corp_code}-{'I1' if i < 2 else 'II'}-{i:04d}", corp.corp_code, rcept_no,
                    "I1" if i < 2 else "II", i, f"{corp.corp_name} 문단 {i}{tag}") for i in range(n)]


class FakeEmbed:
    """본문(접두어 포함)마다 고정 무작위 벡터. 미리 지정한 본문은 그 벡터를 쓴다."""

    def __init__(self, seed: int = 7, fixed: dict[str, list[float]] | None = None):
        self.rng = np.random.default_rng(seed)
        self.fixed = dict(fixed or {})
        self.calls: list[str] = []

    async def __call__(self, text: str) -> list[float]:
        self.calls.append(text)
        if text not in self.fixed:
            self.fixed[text] = self.rng.normal(size=DIM).tolist()
        return self.fixed[text]


def _store(embed=None):
    return store.PassageStore(AsyncQdrantClient(location=":memory:"), embed or FakeEmbed())


def test_point_id_is_deterministic_uuid():
    a = store.point_id("00126380-II-0003")
    assert a == store.point_id("00126380-II-0003")
    assert a != store.point_id("00126380-II-0004")
    import uuid
    uuid.UUID(a)


def test_search_rank_matches_eval_top_k_and_filters_corp():
    question = "주요 제품과 매출 비중은?"
    rng = np.random.default_rng(1)
    q = rng.normal(size=DIM).tolist()
    sam, hyn = _passages(SAMSUNG, 30), _passages(HYNIX, 10)
    fixed = {retrieve.QUERY_PREFIX + question: q}
    sam_vecs = [rng.normal(size=DIM).tolist() for _ in sam]
    fixed.update({retrieve.DOC_PREFIX + p.text: v for p, v in zip(sam, sam_vecs)})
    # 다른 회사 문단 하나는 질문과 같은 벡터다 — 필터가 없으면 1위로 끼어든다
    fixed[retrieve.DOC_PREFIX + hyn[0].text] = list(q)
    embed = FakeEmbed(fixed=fixed)
    s = _store(embed)

    async def go():
        await s.load(SAMSUNG, sam)
        await s.load(HYNIX, hyn)
        return await s.search(SAMSUNG.corp_code, question)

    got = asyncio.run(go())
    want = [sam[i].id for i in retrieve.top_k(q, sam_vecs, 8)]
    assert [p["passage_id"] for p in got] == want
    assert len(got) == 8
    assert embed.calls[-1] == retrieve.QUERY_PREFIX + question
    assert set(embed.calls[:-1]) == {retrieve.DOC_PREFIX + p.text for p in sam + hyn}


def test_search_returns_p2_passage_fields():
    sam = _passages(SAMSUNG, 3)
    s = _store()

    async def go():
        await s.load(SAMSUNG, sam)
        return await s.search(SAMSUNG.corp_code, "q", k=8)

    got = asyncio.run(go())
    assert len(got) == 3
    by_id = {p.id: p for p in sam}
    for row in got:
        p = by_id[row["passage_id"]]
        assert row == {"passage_id": p.id, "rcept_no": p.rcept_no, "section": p.section, "idx": p.idx,
                       "sha256": p.sha256, "text": p.text}


def test_search_unknown_corp_is_empty():
    s = _store()

    async def go():
        await s.load(SAMSUNG, _passages(SAMSUNG, 3))
        return await s.search("99999999", "q")

    assert asyncio.run(go()) == []


def test_reload_same_report_skips_embedding_and_keeps_count():
    embed = FakeEmbed()
    s = _store(embed)
    sam = _passages(SAMSUNG, 5)

    async def go():
        first = await s.load(SAMSUNG, sam)
        n = len(embed.calls)
        second = await s.load(SAMSUNG, sam)
        return first, second, n, len(await s.existing(SAMSUNG.corp_code))

    first, second, n, count = asyncio.run(go())
    assert (first.embedded, first.removed, first.kept) == (5, 0, 0)
    assert (second.embedded, second.removed, second.kept) == (0, 0, 5)
    assert len(embed.calls) == n == 5
    assert count == 5


def test_reload_new_report_overwrites_changed_and_removes_tail():
    embed = FakeEmbed()
    s = _store(embed)
    old = _passages(SAMSUNG, 6, rcept_no="20260312000123")
    # 정정 보고서: 문단 4개, 본문 하나만 바뀌었지만 접수번호가 달라 모두 다시 넣는다
    new = _passages(SAMSUNG, 4, rcept_no="20260320000555", tag="")
    new[3] = Passage(new[3].id, new[3].corp_code, new[3].rcept_no, new[3].section, new[3].idx, "바뀐 본문")

    async def go():
        await s.load(SAMSUNG, old)
        res = await s.load(SAMSUNG, new)
        rows = await s.search(SAMSUNG.corp_code, "q", k=8)
        return res, rows, len(await s.existing(SAMSUNG.corp_code))

    res, rows, count = asyncio.run(go())
    assert (res.embedded, res.removed) == (4, 2)
    assert count == 4
    assert {r["passage_id"] for r in rows} == {p.id for p in new}
    assert {r["rcept_no"] for r in rows} == {"20260320000555"}
    assert "바뀐 본문" in {r["text"] for r in rows}


def test_reload_same_rcept_changed_text_reembeds_only_that_passage():
    embed = FakeEmbed()
    s = _store(embed)
    old = _passages(SAMSUNG, 4)
    new = list(old)
    new[2] = Passage(old[2].id, old[2].corp_code, old[2].rcept_no, old[2].section, old[2].idx, "고친 본문")

    async def go():
        await s.load(SAMSUNG, old)
        return await s.load(SAMSUNG, new)

    res = asyncio.run(go())
    assert (res.embedded, res.kept, res.removed) == (1, 3, 0)
    assert embed.calls[-1] == retrieve.DOC_PREFIX + "고친 본문"


def test_load_does_not_touch_other_corp():
    s = _store()

    async def go():
        await s.load(SAMSUNG, _passages(SAMSUNG, 3))
        await s.load(HYNIX, _passages(HYNIX, 2))
        await s.load(SAMSUNG, _passages(SAMSUNG, 1, rcept_no="20260401000001"))
        return len(await s.existing(SAMSUNG.corp_code)), len(await s.existing(HYNIX.corp_code))

    assert asyncio.run(go()) == (1, 2)


def test_embed_failure_leaves_previous_points():
    calls = {"n": 0}
    base = FakeEmbed()

    async def flaky(text):
        calls["n"] += 1
        if calls["n"] > 3:
            raise RuntimeError("ollama down")
        return await base(text)

    s = _store(flaky)

    async def go():
        await s.load(SAMSUNG, _passages(SAMSUNG, 3))
        with pytest.raises(RuntimeError):
            await s.load(SAMSUNG, _passages(SAMSUNG, 2, rcept_no="20260401000001"))
        have = await s.existing(SAMSUNG.corp_code)  # 검색은 임베딩을 다시 부르므로 payload로 본다
        return len(await s.existing(SAMSUNG.corp_code)), {r["rcept_no"] for r in have.values()}

    assert asyncio.run(go()) == (3, {"20260312000123"})


def test_companies_lists_loaded_corps_by_name_with_counts():
    s = _store()

    async def go():
        await s.load(SAMSUNG, _passages(SAMSUNG, 3))
        await s.load(HYNIX, _passages(HYNIX, 2, rcept_no="20260310000777"))
        return await s.companies()

    assert asyncio.run(go()) == [
        {"corp_code": HYNIX.corp_code, "corp_name": "SK하이닉스", "stock_code": "000660",
         "rcept_no": "20260310000777", "passages": 2},
        {"corp_code": SAMSUNG.corp_code, "corp_name": "삼성전자", "stock_code": "005930",
         "rcept_no": "20260312000123", "passages": 3},
    ]


def test_existing_reads_only_skip_fields():
    s = _store()

    async def go():
        await s.load(SAMSUNG, _passages(SAMSUNG, 1))
        return await s.existing(SAMSUNG.corp_code)

    (row,) = asyncio.run(go()).values()
    assert set(row) == {"passage_id", "rcept_no", "sha256"}


def test_exists_is_false_before_first_load():
    s = _store()

    async def go():
        before = await s.exists()
        await s.load(SAMSUNG, _passages(SAMSUNG, 1))
        return before, await s.exists()

    assert asyncio.run(go()) == (False, True)


def test_search_and_companies_raise_without_collection():
    """컬렉션이 없거나 Qdrant가 죽었으면 예외 → 라우트가 503(passage_search_failed·company_list_failed)으로 바꾼다."""
    s = _store()

    async def search():
        await s.search(SAMSUNG.corp_code, "q")

    async def companies():
        await s.companies()

    for fn in (search, companies):
        with pytest.raises(Exception):
            asyncio.run(fn())


def test_query_embedding_has_short_timeout():
    async def slow(text):
        await asyncio.sleep(5)
        return [1.0] * DIM

    s = store.PassageStore(AsyncQdrantClient(location=":memory:"), slow, query_timeout=0.05)
    assert store.QUERY_EMBED_TIMEOUT_S <= 10

    async def go():
        await s.search(SAMSUNG.corp_code, "q")

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(go())


# ── 앱 시작 시 연결 ─────────────────────────────────────────────────────────────

@pytest.fixture
def unwired():
    def reset():
        evidence_routes.set_passage_store(None)
        store._wired = None
    reset()
    yield
    reset()


def _enable(monkeypatch, on=True):
    from app.config import settings
    monkeypatch.setattr(settings, "EVIDENCE_CHAT_ENABLED", on)


def test_wire_connects_search_and_companies_when_flag_on(monkeypatch, unwired):
    _enable(monkeypatch)
    s = _store()

    async def go():
        await s.load(SAMSUNG, _passages(SAMSUNG, 2))
        ok = await store.wire(s)
        rows = await evidence_routes.get_passage_search()(SAMSUNG.corp_code, "q")
        corps = await evidence_routes.get_company_list()()
        return ok, rows, corps

    ok, rows, corps = asyncio.run(go())
    assert ok is True
    assert len(rows) == 2
    assert [c["corp_code"] for c in corps] == [SAMSUNG.corp_code]
    assert store._wired is s


def test_wire_without_collection_recovers_without_restart(monkeypatch, unwired):
    """시작 때 컬렉션(또는 Qdrant)이 없어도 연결해 둔다. 나중에 적재되면 재시작 없이 검색된다."""
    _enable(monkeypatch)
    s = _store()

    async def go():
        ok = await store.wire(s)
        search = evidence_routes.get_passage_search()
        try:
            await search(SAMSUNG.corp_code, "q")
            first = "ok"
        except Exception:
            first = "error"
        created = await s.exists()  # 시작 시 빈 컬렉션을 만들지 않는다
        await s.load(SAMSUNG, _passages(SAMSUNG, 2))
        return ok, first, created, await evidence_routes.get_passage_search()(SAMSUNG.corp_code, "q")

    ok, first, created, rows = asyncio.run(go())
    assert (ok, first, created) == (True, "error", False)
    assert len(rows) == 2


def test_wire_does_nothing_when_flag_off(monkeypatch, unwired):
    _enable(monkeypatch, False)
    s = _store()

    async def go():
        await s.load(SAMSUNG, _passages(SAMSUNG, 1))
        return await store.wire(s)

    assert asyncio.run(go()) is False
    assert evidence_routes.get_passage_search() is None
    assert evidence_routes.get_company_list() is None


def test_unwire_disconnects_and_closes(monkeypatch, unwired):
    _enable(monkeypatch)
    s = _store()
    closed = []

    async def aclose():
        closed.append(True)
    s.aclose = aclose

    async def go():
        await store.wire(s)
        await store.unwire()

    asyncio.run(go())
    assert evidence_routes.get_passage_search() is None and store._wired is None
    assert closed == [True]
