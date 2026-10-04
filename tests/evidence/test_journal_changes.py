# tests/evidence/test_journal_changes.py
"""판단 일지 변화 비교(모듈 C spec 결정 6-1·6-2, 10절 P2, 수용 기준 8.1의 5).

- 문단: 스냅샷 본문 sha256이 지금 그 회사 적재 문단의 sha256 집합 안에 있으면 same, 없으면 gone(passage_id로 짝짓지 않는다).
- 보고서: 지금 적재된 rcept_no 집합이 스냅샷 값 하나뿐이면 same, 다르면 replaced, 그 회사 문단이 0개면 company_gone.
- 저장소 없음·컬렉션 없음·예외 → {"status": "unavailable"} 하나.
- 비교 결과는 저장하지 않는다(결정 6-2).

저장소는 qdrant-client 메모리 모드(:memory:)와 가짜 임베딩을 쓴다(test_passage_store.py 방식, 외부 호출 없음).
API 테스트는 pg 픽스처(EVIDENCE_TEST_DATABASE_URL)를 쓴다.
"""
import asyncio
import uuid

import pytest
from qdrant_client import AsyncQdrantClient
from sqlalchemy import func, select

from app.config import settings
from app.models import JudgmentEntry, JudgmentUpdate
from app.routes import evidence as evidence_routes
from app.services.evidence import store
from app.services.evidence.dart import Corp
from app.services.evidence.passages import Passage
from app.services.evidence.runner import A2_PROVISIONAL, DEFAULT_POLICY
from app.services.journal import changes
from tests.evidence.p2_support import Who, client, database, make_app, seed_user
from tests.evidence.test_passage_store import FakeEmbed

SAMSUNG = Corp("00126380", "삼성전자", "005930")
HYNIX = Corp("00164779", "SK하이닉스", "000660")
RCEPT = "20260312000123"
TEXTS = ["회사는 메모리 반도체와 스마트폰을 생산한다.", "2025년 영업이익은 32조 7,260억원이다.",
         "주요 원재료는 웨이퍼다.", "주요 고객은 해외 IT 기업이다."]


def _passages(texts=TEXTS, rcept_no=RCEPT, corp=SAMSUNG, start=0) -> list[Passage]:
    return [Passage(f"{corp.corp_code}-II-{start + i:04d}", corp.corp_code, rcept_no, "II. 사업의 내용", start + i, t)
            for i, t in enumerate(texts)]


def _snapshot(passages: list[Passage], *, policy=DEFAULT_POLICY.version, claims=None) -> dict:
    """스냅샷 형식 c1에서 비교가 쓰는 칸만."""
    return {
        "run": {"id": str(uuid.uuid4()), "status": "done", "policy_version": policy},
        "company": SAMSUNG.corp_name, "corp_code": SAMSUNG.corp_code, "rcept_no": RCEPT,
        "question": "q", "answer": "a",
        "claims": claims if claims is not None else [],
        "passages": [{"passage_id": p.id, "section": p.section, "idx": p.idx, "sha256": p.sha256, "text": p.text}
                     for p in passages],
    }


def _store(embed=None):
    return store.PassageStore(AsyncQdrantClient(location=":memory:"), embed or FakeEmbed())


def _compare(snapshot, *loads):
    """loads: (corp, passages) 순서대로 적재한 뒤 비교한다. 비교 중 임베딩 호출 수도 돌려준다."""
    embed = FakeEmbed()
    s = _store(embed)

    async def go():
        for corp, ps in loads:
            await s.load(corp, ps)
        before = len(embed.calls)
        out = await changes.compare(s, snapshot)
        return out, len(embed.calls) - before

    return asyncio.run(go())


def _statuses(out) -> dict[str, str]:
    return {p["passage_id"]: p["status"] for p in out["passages"]}


# ── 문단 단위 ──────────────────────────────────────────────────────────────────

def test_same_report_all_passages_same_and_no_embedding():
    ps = _passages()
    out, embeds = _compare(_snapshot(ps), (SAMSUNG, ps))
    assert out["status"] == "ok"
    assert set(_statuses(out).values()) == {"same"}
    assert out["report"] == {"status": "same", "snapshot_rcept_no": RCEPT, "current_rcept_nos": [RCEPT]}
    assert out["policy"] == {"snapshot": DEFAULT_POLICY.version, "current": DEFAULT_POLICY.version}
    assert embeds == 0  # 비교는 Qdrant scroll만 쓴다(Ollama 임베딩·검색 없음)


def test_changed_text_is_gone_only_for_that_passage():
    ps = _passages()
    edited = list(ps)
    edited[2] = Passage(ps[2].id, ps[2].corp_code, ps[2].rcept_no, ps[2].section, ps[2].idx, "주요 원재료는 웨이퍼와 가스다.")
    out, _ = _compare(_snapshot(ps), (SAMSUNG, edited))
    assert _statuses(out) == {ps[0].id: "same", ps[1].id: "same", ps[2].id: "gone", ps[3].id: "same"}
    assert out["report"]["status"] == "same"


def test_inserted_passage_shifting_ids_keeps_same_text_same():
    """문단 하나가 끼어들어 passage_id가 한 칸씩 밀려도 본문이 같은 문단은 same(위치로 짝짓지 않는다)."""
    ps = _passages()
    shifted = _passages([TEXTS[0], "새로 끼어든 문단이다.", *TEXTS[1:]])
    assert shifted[1].id == ps[1].id and shifted[1].sha256 != ps[1].sha256  # 같은 id에 다른 본문
    out, _ = _compare(_snapshot(ps), (SAMSUNG, shifted))
    assert set(_statuses(out).values()) == {"same"}


def test_rcept_no_only_change_passages_same_report_replaced():
    """정정 보고서: 본문은 같고 접수번호만 바뀌었다 → 문단은 same, 보고서만 replaced."""
    ps = _passages()
    out, _ = _compare(_snapshot(ps), (SAMSUNG, _passages(rcept_no="20260320000555")))
    assert set(_statuses(out).values()) == {"same"}
    assert out["report"] == {"status": "replaced", "snapshot_rcept_no": RCEPT,
                             "current_rcept_nos": ["20260320000555"]}


def test_mixed_rcept_nos_are_replaced_with_whole_set():
    """적재가 도중에 실패해 한 회사 안에 접수번호가 섞이면 집합을 그대로 보인다."""
    ps = _passages()
    mixed = _passages(TEXTS[:2]) + _passages(TEXTS[2:], rcept_no="20260320000555", start=2)
    out, _ = _compare(_snapshot(ps), (SAMSUNG, mixed))
    assert out["report"] == {"status": "replaced", "snapshot_rcept_no": RCEPT,
                             "current_rcept_nos": [RCEPT, "20260320000555"]}
    assert set(_statuses(out).values()) == {"same"}


def test_company_without_passages_is_company_gone_and_all_gone():
    ps = _passages()
    out, _ = _compare(_snapshot(ps), (HYNIX, _passages(corp=HYNIX)))
    assert out["report"] == {"status": "company_gone", "snapshot_rcept_no": RCEPT, "current_rcept_nos": []}
    assert set(_statuses(out).values()) == {"gone"}


def test_other_corp_same_text_does_not_count():
    """같은 본문이 다른 회사에 있어도 그 회사 문단만 본다."""
    ps = _passages()
    out, _ = _compare(_snapshot(ps), (SAMSUNG, _passages(["전혀 다른 문단이다."])), (HYNIX, _passages(corp=HYNIX)))
    assert set(_statuses(out).values()) == {"gone"}
    assert out["report"]["status"] == "same"


def test_passages_keep_snapshot_order_with_passage_idx():
    """P3 화면(PR #30)과 맞춘 모양: 스냅샷 문단 순서 그대로, passage_idx는 스냅샷 passages 위치(source_idx와 같은 번호).
    ✅·⚠️ 근거 문단을 맨 위에 두는 것은 스냅샷 문장(source_idx)을 가진 화면이 한다."""
    ps = _passages()
    claims = [{"idx": 0, "status": "supported", "source_idx": 2}, {"idx": 1, "status": "contradicted", "source_idx": 3}]
    out, _ = _compare(_snapshot(ps, claims=claims), (SAMSUNG, ps[:1] + ps[2:]))
    assert out["passages"] == [{"passage_idx": i, "passage_id": p.id, "sha256": p.sha256,
                                "status": "gone" if i == 1 else "same"} for i, p in enumerate(ps)]
    assert set(out) == {"status", "report", "passages", "policy"}


def test_policy_change_reports_both_versions():
    ps = _passages()
    out, _ = _compare(_snapshot(ps, policy=A2_PROVISIONAL.version), (SAMSUNG, ps))
    assert out["policy"] == {"snapshot": A2_PROVISIONAL.version, "current": DEFAULT_POLICY.version}


# ── 확인 불가 ─────────────────────────────────────────────────────────────────

UNAVAILABLE = {"status": "unavailable"}


def test_no_store_is_unavailable():
    assert asyncio.run(changes.compare(None, _snapshot(_passages()))) == UNAVAILABLE


def test_missing_collection_is_unavailable_not_all_gone():
    """existing()은 컬렉션이 없으면 빈 dict를 준다 — exists()를 먼저 보지 않으면 모두 gone으로 보인다."""
    out, _ = _compare(_snapshot(_passages()))
    assert out == UNAVAILABLE


class Broken:
    def __init__(self, where: str):
        self.where = where
        self.calls: list[str] = []

    async def exists(self):
        self.calls.append("exists")
        if self.where == "exists":
            raise ConnectionError("qdrant down")
        return True

    async def existing(self, corp_code):
        self.calls.append("existing")
        raise ConnectionError("qdrant down")


@pytest.mark.parametrize("where", ["exists", "existing"])
def test_store_errors_are_unavailable(where, caplog):
    s = Broken(where)
    with caplog.at_level("WARNING", logger="app.journal.changes"):
        assert asyncio.run(changes.compare(s, _snapshot(_passages()))) == UNAVAILABLE
    assert s.calls[0] == "exists"
    assert "ConnectionError" in caplog.text and "qdrant down" not in caplog.text


def test_slow_store_is_unavailable(monkeypatch):
    class Slow(Broken):
        async def exists(self):
            await asyncio.sleep(5)
            return True

    monkeypatch.setattr(changes, "STORE_TIMEOUT_S", 0.05)
    assert asyncio.run(changes.compare(Slow("none"), _snapshot(_passages()))) == UNAVAILABLE


# ── API ───────────────────────────────────────────────────────────────────────

@pytest.fixture
def journal(monkeypatch):
    monkeypatch.setattr(settings, "JOURNAL_ENABLED", True)


async def _entry(factory, seed: dict, snapshot: dict) -> uuid.UUID:
    async with factory() as db:
        e = JudgmentEntry(id=uuid.uuid4(), user_id=uuid.UUID(seed["user"]["id"]), run_id=uuid.UUID(snapshot["run"]["id"]),
                          company=snapshot["company"], corp_code=snapshot["corp_code"], snapshot=snapshot,
                          snapshot_version="c1")
        db.add(e)
        db.add(JudgmentUpdate(entry_id=e.id, kind="initial", decision="watch", conviction=3, memo="",
                              relied_claims=[]))
        await db.commit()
        return e.id


def _app(factory, who, passage_store):
    app = make_app(factory, who)
    app.dependency_overrides[evidence_routes.get_passage_store] = lambda: passage_store
    return app


async def _counts(factory):
    async with factory() as db:
        return (await db.scalar(select(func.count()).select_from(JudgmentEntry)),
                await db.scalar(select(func.count()).select_from(JudgmentUpdate)))


def test_get_passage_store_returns_wired_store():
    s = object()
    try:
        evidence_routes.set_passage_store(s)
        assert evidence_routes.get_passage_store() is s
    finally:
        evidence_routes.set_passage_store(None)
    assert evidence_routes.get_passage_store() is None


def test_changes_api_compares_without_saving(pg, journal):
    ps = _passages()
    edited = list(ps)
    edited[0] = Passage(ps[0].id, ps[0].corp_code, "20260320000555", ps[0].section, ps[0].idx, "고친 본문")
    edited[1:] = _passages(TEXTS[1:], rcept_no="20260320000555", start=1)
    snapshot = _snapshot(ps, claims=[{"idx": 0, "status": "supported", "source_idx": 1}])
    s = _store()

    async def go():
        await s.load(SAMSUNG, edited)
        async with database(pg) as factory:
            seed = await seed_user(factory)
            eid = await _entry(factory, seed, snapshot)
            before = await _counts(factory)
            async with client(_app(factory, Who(seed["user"]), s)) as c:
                r1 = await c.get(f"/api/journal/{eid}/changes")
                r2 = await c.get(f"/api/journal/{eid}/changes")
            async with factory() as db:
                stored = (await db.get(JudgmentEntry, eid)).snapshot
            return r1, r2, before, await _counts(factory), stored

    r1, r2, before, after, stored = asyncio.run(go())
    assert r1.status_code == 200 and r1.json() == r2.json()
    body = r1.json()
    assert body["status"] == "ok"
    assert body["report"] == {"status": "replaced", "snapshot_rcept_no": RCEPT,
                             "current_rcept_nos": ["20260320000555"]}
    assert [(p["passage_idx"], p["passage_id"], p["status"]) for p in body["passages"]] == [
        (0, ps[0].id, "gone"), (1, ps[1].id, "same"), (2, ps[2].id, "same"), (3, ps[3].id, "same")]
    assert before == after == (1, 1) and stored == snapshot  # 결정 6-2: 비교 결과를 저장하지 않는다


def test_changes_api_unavailable_without_store(pg, journal, monkeypatch):
    """근거 모드가 꺼져 저장소가 연결되지 않았으면 unavailable(수용 기준 9)."""
    monkeypatch.setattr(settings, "EVIDENCE_CHAT_ENABLED", False)

    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            eid = await _entry(factory, seed, _snapshot(_passages()))
            async with client(_app(factory, Who(seed["user"]), None)) as c:
                return await c.get(f"/api/journal/{eid}/changes")

    r = asyncio.run(go())
    assert (r.status_code, r.json()) == (200, UNAVAILABLE)


def test_changes_api_unavailable_without_collection(pg, journal):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory)
            eid = await _entry(factory, seed, _snapshot(_passages()))
            async with client(_app(factory, Who(seed["user"]), _store())) as c:
                return await c.get(f"/api/journal/{eid}/changes")

    r = asyncio.run(go())
    assert (r.status_code, r.json()) == (200, UNAVAILABLE)


def test_changes_api_other_users_entry_is_404_before_store(pg, journal):
    s = Broken("none")

    async def go():
        async with database(pg) as factory:
            a, b = await seed_user(factory), await seed_user(factory)
            eid = await _entry(factory, a, _snapshot(_passages()))
            async with client(_app(factory, Who(b["user"]), s)) as c:
                return [await c.get(f"/api/journal/{x}/changes") for x in (eid, uuid.uuid4(), "not-a-uuid")]

    assert [r.status_code for r in asyncio.run(go())] == [404, 404, 404]
    assert s.calls == []  # 소유자 확인 전에는 저장소를 보지 않는다


def test_changes_api_flag_off_is_404(monkeypatch):
    monkeypatch.setattr(settings, "JOURNAL_ENABLED", False)
    s = Broken("none")

    class NoDb:
        def __call__(self):
            raise AssertionError("DB에 닿으면 안 된다")

    async def go():
        async with client(_app(NoDb(), Who(None), s)) as c:
            return await c.get(f"/api/journal/{uuid.uuid4()}/changes")

    assert asyncio.run(go()).status_code == 404
    assert s.calls == []
