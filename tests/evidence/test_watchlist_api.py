# tests/evidence/test_watchlist_api.py
"""관심종목 저장·API(모듈 D spec 11절 P1, 수용 기준 10.1의 1~7).

마이그레이션 0011, 경로 네 개(5-1), 입력 검증과 메모 없는 422(5-2), 409 두 종류(5-3·4-2), corp_code 매핑과
나중에 채우기(4-3), 감사 로그·외부 전송 없음(5-4·8-1), 관리자 초기화(5-6).
가짜 회사 목록만 쓴다. DB가 필요한 테스트는 pg 픽스처(EVIDENCE_TEST_DATABASE_URL)를 쓴다.
"""
import asyncio
import json
import logging
import uuid

import pytest
from sqlalchemy import func, select, text

from app.models import AuditEvent, WatchlistItem
from tests.evidence.conftest import alembic_downgrade, alembic_upgrade
from tests.evidence.p2_support import FakeJev, FakeLLM, Who, client, database, make_app, make_runner, seed_user

NOTE = "비밀메모-보유 300주, 다음 달 추가 매수 계획"
LOADED = [
    {"corp_code": "00126380", "corp_name": "삼성전자", "stock_code": "005930", "rcept_no": "1", "passages": 3},
    {"corp_code": "00877059", "corp_name": "에코프로비엠", "stock_code": "247540", "rcept_no": "3", "passages": 1},
]


class Companies:
    """근거 모드 문단 저장소의 companies() 대역. 부른 횟수를 센다."""

    def __init__(self, loaded=LOADED, exc: Exception | None = None):
        self.loaded, self.exc, self.calls = loaded, exc, 0

    async def __call__(self):
        self.calls += 1
        if self.exc:
            raise self.exc
        return [dict(c) for c in self.loaded]


async def _count(factory, **where) -> int:
    async with factory() as db:
        stmt = select(func.count()).select_from(WatchlistItem)
        for k, v in where.items():
            stmt = stmt.where(getattr(WatchlistItem, k) == v)
        return await db.scalar(stmt)


# ── 마이그레이션 ──────────────────────────────────────────────────────────────

def test_migration_0011_table_constraints_and_downgrade(pg):
    async def info():
        async with database(pg) as factory, factory() as db:
            cols = {r[0]: (r[1], r[2], r[3]) for r in await db.execute(text(
                "SELECT column_name, data_type, character_maximum_length, is_nullable "
                "FROM information_schema.columns WHERE table_name = 'watchlist_items'"))}
            uniques = sorted(r[0] for r in await db.execute(text(
                "SELECT conname FROM pg_constraint WHERE contype = 'u' AND conrelid::regclass::text = 'watchlist_items'")))
            indexes = sorted(r[0] for r in await db.execute(text(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'watchlist_items'")))
            fks = sorted(tuple(r) for r in await db.execute(text(
                "SELECT tc.table_name, ccu.table_name FROM information_schema.table_constraints tc "
                "JOIN information_schema.constraint_column_usage ccu ON ccu.constraint_name = tc.constraint_name "
                "WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_name = 'watchlist_items'")))
            version = await db.scalar(text("SELECT version_num FROM alembic_version"))
            return cols, uniques, indexes, fks, version

    cols, uniques, indexes, fks, version = asyncio.run(info())
    assert version == "0011"
    assert cols == {
        "id": ("uuid", None, "NO"), "user_id": ("uuid", None, "NO"),
        "symbol": ("character varying", 20, "NO"), "name": ("character varying", 100, "YES"),
        "corp_code": ("character varying", 8, "YES"), "market": ("character varying", 16, "YES"),
        "note": ("character varying", 200, "YES"), "created_at": ("timestamp with time zone", None, "NO"),
    }
    assert uniques == ["uq_watchlist_user_symbol"]
    assert "ix_watchlist_items_user_created" in indexes
    assert fks == [("watchlist_items", "users")]
    alembic_downgrade(pg, "0010")
    try:
        after = asyncio.run(info())
        assert after[0] == {} and after[4] == "0010"
        async def journal_left():
            async with database(pg) as factory, factory() as db:
                return sorted(r[0] for r in await db.execute(text(
                    "SELECT tablename FROM pg_tables WHERE tablename LIKE 'judgment_%'")))
        assert asyncio.run(journal_left()) == ["judgment_entries", "judgment_updates"]  # 표만 사라진다
    finally:
        alembic_upgrade(pg)
    assert asyncio.run(info())[4] == "0011"


def test_model_reexported():
    from app import models

    assert "WatchlistItem" in models.__all__ and models.WatchlistItem.__tablename__ == "watchlist_items"


# ── 추가·목록·메모·삭제 ────────────────────────────────────────────────────────

def test_add_list_patch_delete_roundtrip(pg):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory, with_chat=False)
            app = make_app(factory, Who(seed["user"]), companies=Companies())
            async with client(app) as c:
                a = await c.post("/api/watchlist", json={"symbol": " 005930.ks ", "name": " 삼성전자 ",
                                                         "exchange": "KSC", "note": ""})
                b = await c.post("/api/watchlist", json={"symbol": "aapl", "name": "Apple Inc.",
                                                         "exchange": "NASDAQ", "note": NOTE})
                listed = await c.get("/api/watchlist")
                patched = await c.patch(f"/api/watchlist/{b.json()['id']}", json={"note": "새 메모\n둘째 줄"})
                cleared = await c.patch(f"/api/watchlist/{b.json()['id']}", json={"note": ""})
                deleted = await c.delete(f"/api/watchlist/{a.json()['id']}")
                after = await c.get("/api/watchlist")
            return a, b, listed, patched, cleared, deleted, after

    a, b, listed, patched, cleared, deleted, after = asyncio.run(go())
    assert a.status_code == 201 and b.status_code == 201
    ja = a.json()
    assert {k: ja[k] for k in ("symbol", "name", "corp_code", "market", "note")} == {
        "symbol": "005930.KS", "name": "삼성전자", "corp_code": "00126380", "market": "KOSPI", "note": None}
    assert set(ja) == {"id", "symbol", "name", "corp_code", "market", "note", "created_at"}
    jb = b.json()
    assert (jb["symbol"], jb["corp_code"], jb["market"], jb["note"]) == ("AAPL", None, "NASDAQ", NOTE)
    assert listed.status_code == 200
    body = listed.json()
    assert body["limit"] == 100 and [i["id"] for i in body["items"]] == [ja["id"], jb["id"]]  # 추가한 순서
    assert body["items"][0] == ja
    assert patched.status_code == 200 and patched.json() == {**jb, "note": "새 메모\n둘째 줄"}
    assert cleared.status_code == 200 and cleared.json()["note"] is None
    assert deleted.status_code == 204 and deleted.content == b""
    assert [i["id"] for i in after.json()["items"]] == [jb["id"]]


def test_unauthenticated_is_401(pg):
    async def go():
        async with database(pg) as factory:
            app = make_app(factory, Who(None))
            async with client(app) as c:
                return [await c.get("/api/watchlist"), await c.post("/api/watchlist", json={"symbol": "AAPL"}),
                        await c.patch(f"/api/watchlist/{uuid.uuid4()}", json={"note": "x"}),
                        await c.delete(f"/api/watchlist/{uuid.uuid4()}")]

    assert [r.status_code for r in asyncio.run(go())] == [401] * 4


def test_no_feature_flag_in_router():
    """기능 플래그가 없다(결정 5-5): 라우터에 의존성이 붙지 않는다."""
    from app.routes import watchlist

    assert watchlist.router.prefix == "/api/watchlist" and watchlist.router.dependencies == []
    assert watchlist.MAX_ITEMS == 100


def test_main_registers_watchlist_router():
    """main.py가 지역 import 패턴으로 라우터를 붙인다(app.main은 무거워 import하지 않고 소스를 읽는다)."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "app/main.py").read_text(encoding="utf-8")
    assert "from app.routes import watchlist as watchlist_routes" in src
    assert "app.include_router(watchlist_routes.router)" in src
    assert src.index("watchlist_routes.router") > src.index("journal_routes.router")


# ── 수용 기준 1: 소유자 확인 ──────────────────────────────────────────────────

def test_other_users_items_are_404_and_unchanged(pg):
    async def go():
        async with database(pg) as factory:
            me, other = await seed_user(factory, with_chat=False), await seed_user(factory, with_chat=False)
            who = Who(other["user"])
            app = make_app(factory, who)
            async with client(app) as c:
                theirs = (await c.post("/api/watchlist", json={"symbol": "AAPL", "note": NOTE})).json()
                who.user = me["user"]
                await c.post("/api/watchlist", json={"symbol": "MSFT"})
                patch = await c.patch(f"/api/watchlist/{theirs['id']}", json={"note": "덮어쓰기"})
                dele = await c.delete(f"/api/watchlist/{theirs['id']}")
                missing = await c.delete(f"/api/watchlist/{uuid.uuid4()}")
                bad = [await c.patch("/api/watchlist/not-a-uuid", json={"note": "x"}),
                       await c.delete("/api/watchlist/not-a-uuid")]
                mine = (await c.get("/api/watchlist")).json()
            async with factory() as db:
                row = await db.get(WatchlistItem, uuid.UUID(theirs["id"]))
                kept = (row.symbol, row.note)
            return patch, dele, missing, bad, mine, kept

    patch, dele, missing, bad, mine, kept = asyncio.run(go())
    for r in (patch, dele, missing, *bad):
        assert r.status_code == 404 and r.json()["detail"] == "관심종목을 찾을 수 없습니다."
    assert kept == ("AAPL", NOTE)
    assert [i["symbol"] for i in mine["items"]] == ["MSFT"]


# ── 수용 기준 2·3: 409 두 종류 ────────────────────────────────────────────────

def test_duplicate_symbol_case_insensitive_is_409_with_first_id(pg):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory, with_chat=False)
            app = make_app(factory, Who(seed["user"]))
            async with client(app) as c:
                first = await c.post("/api/watchlist", json={"symbol": "005930.KS", "note": "첫 메모"})
                second = await c.post("/api/watchlist", json={"symbol": "005930.ks", "note": NOTE})
            return first, second, await _count(factory)

    first, second, n = asyncio.run(go())
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json() == {"detail": "이미 관심종목에 있습니다.", "code": "duplicate", "item_id": first.json()["id"]}
    assert n == 1 and NOTE not in second.text


async def _fill(factory, user_id: str, n: int) -> None:
    async with factory() as db:
        db.add_all([WatchlistItem(user_id=uuid.UUID(user_id), symbol=f"T{i:03d}") for i in range(n)])
        await db.commit()


def test_limit_100_is_409_and_counts_only_my_rows(pg):
    async def go():
        async with database(pg) as factory:
            me, other = await seed_user(factory, with_chat=False), await seed_user(factory, with_chat=False)
            await _fill(factory, me["user"]["id"], 100)
            await _fill(factory, other["user"]["id"], 99)
            who = Who(me["user"])
            app = make_app(factory, who)
            async with client(app) as c:
                over = await c.post("/api/watchlist", json={"symbol": "AAPL", "note": NOTE})
                dup = await c.post("/api/watchlist", json={"symbol": "t000"})  # 이미 있는 종목은 duplicate
                who.user = other["user"]
                ok = await c.post("/api/watchlist", json={"symbol": "AAPL"})
            return (over, dup, ok, await _count(factory, user_id=uuid.UUID(me["user"]["id"])),
                    await _count(factory, user_id=uuid.UUID(other["user"]["id"])))

    over, dup, ok, mine, theirs = asyncio.run(go())
    assert over.status_code == 409
    assert over.json() == {"detail": "관심종목은 100개까지 담을 수 있습니다.", "code": "limit", "limit": 100}
    assert dup.status_code == 409 and dup.json()["code"] == "duplicate"
    assert ok.status_code == 201 and mine == 100 and theirs == 100


def test_insert_race_on_unique_is_409_duplicate_with_competitor_id(pg, monkeypatch):
    """존재 확인과 커밋 사이에 같은 종목이 먼저 저장되면(유일 제약 uq_watchlist_user_symbol) 409 duplicate."""
    from app.services import watchlist as wl

    holder = {}
    real = wl.load_companies

    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory, with_chat=False)

            async def racing(companies):  # 확인 뒤·삽입 전에 다른 세션이 먼저 저장한다
                async with factory() as other:
                    item = WatchlistItem(user_id=uuid.UUID(seed["user"]["id"]), symbol="005930.KS")
                    other.add(item)
                    await other.commit()
                    holder["competitor"] = str(item.id)
                return await real(companies)
            monkeypatch.setattr(wl, "load_companies", racing)
            app = make_app(factory, Who(seed["user"]), companies=Companies())
            async with client(app) as c:
                r = await c.post("/api/watchlist", json={"symbol": "005930.KS", "note": NOTE})
            return r, await _count(factory)

    r, n = asyncio.run(go())
    assert r.status_code == 409 and r.json()["code"] == "duplicate" and r.json()["item_id"] == holder["competitor"]
    assert n == 1 and NOTE not in r.text


def test_other_integrity_error_is_500_not_409(pg, caplog):
    caplog.set_level(logging.DEBUG)

    async def ddl(sql):
        async with database(pg) as factory, factory() as db:
            await db.execute(text(sql))
            await db.commit()

    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory, with_chat=False)
            app = make_app(factory, Who(seed["user"]))
            async with client(app) as c:
                r = await c.post("/api/watchlist", json={"symbol": "AAPL", "note": NOTE})
            return r, await _count(factory)

    asyncio.run(ddl("ALTER TABLE watchlist_items ADD CONSTRAINT ck_test_block CHECK (symbol <> 'AAPL')"))
    try:
        r, n = asyncio.run(go())
    finally:
        asyncio.run(ddl("ALTER TABLE watchlist_items DROP CONSTRAINT ck_test_block"))
    assert r.status_code == 500 and "이미" not in r.text and n == 0
    assert "비밀메모" not in r.text and "비밀메모" not in "\n".join(x.getMessage() for x in caplog.records)


# ── 수용 기준 4: corp_code 매핑 ───────────────────────────────────────────────

def test_mapping_on_add(pg):
    companies = Companies()

    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory, with_chat=False)
            app = make_app(factory, Who(seed["user"]), companies=companies)
            async with client(app) as c:
                out = {}
                for sym in ("005930.KS", "247540.KQ", "000000.KS"):
                    out[sym] = await c.post("/api/watchlist", json={"symbol": sym})
                calls_kr = companies.calls
                out["AAPL"] = await c.post("/api/watchlist", json={"symbol": "AAPL"})
                out["ignored"] = await c.post("/api/watchlist", json={"symbol": "MSFT", "corp_code": "12345678",
                                                                      "market": "FAKE", "id": str(uuid.uuid4())})
            return out, calls_kr

    out, calls_kr = asyncio.run(go())
    assert all(r.status_code == 201 for r in out.values())
    got = {s: (r.json()["corp_code"], r.json()["market"]) for s, r in out.items()}
    assert got == {"005930.KS": ("00126380", "KOSPI"), "247540.KQ": ("00877059", "KOSDAQ"),
                   "000000.KS": (None, "KOSPI"), "AAPL": (None, None), "ignored": (None, None)}
    assert calls_kr == 3 and companies.calls == 3  # 국내 심볼에서만 부른다


@pytest.mark.parametrize("companies", [None, Companies(exc=RuntimeError("down"))])
def test_mapping_without_store_or_failure_still_adds(pg, companies):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory, with_chat=False)
            app = make_app(factory, Who(seed["user"]), companies=companies)
            async with client(app) as c:
                return await c.post("/api/watchlist", json={"symbol": "005930.KS"})

    r = asyncio.run(go())
    assert r.status_code == 201 and r.json()["corp_code"] is None and r.json()["market"] == "KOSPI"


def test_list_backfills_null_domestic_rows_once(pg):
    """근거 모드가 꺼진 동안 추가한 국내 종목은 null → 켜진 뒤 목록 조회에서 채운다(companies() 1회)."""
    companies = Companies()

    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory, with_chat=False)
            off = make_app(factory, Who(seed["user"]), companies=None)
            async with client(off) as c:
                for sym in ("005930.KS", "247540.KQ", "000000.KS", "AAPL"):
                    await c.post("/api/watchlist", json={"symbol": sym})
                before = (await c.get("/api/watchlist")).json()
            on = make_app(factory, Who(seed["user"]), companies=companies)
            async with client(on) as c:
                first = (await c.get("/api/watchlist")).json()
                calls_first = companies.calls
            async with factory() as db:
                stored = {r.symbol: r.corp_code for r in (await db.execute(select(WatchlistItem))).scalars()}
            # 채운 값은 적재 목록에서 빠져도 지우지 않는다
            companies.loaded = []
            async with client(on) as c:
                second = (await c.get("/api/watchlist")).json()
            return before, first, calls_first, stored, second

    before, first, calls_first, stored, second = asyncio.run(go())
    assert [i["corp_code"] for i in before["items"]] == [None] * 4
    assert [i["corp_code"] for i in first["items"]] == ["00126380", "00877059", None, None]
    assert calls_first == 1
    assert stored == {"005930.KS": "00126380", "247540.KQ": "00877059", "000000.KS": None, "AAPL": None}
    assert [i["corp_code"] for i in second["items"]] == ["00126380", "00877059", None, None]


def test_list_does_not_call_store_without_null_domestic_rows(pg):
    companies = Companies()

    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory, with_chat=False)
            app = make_app(factory, Who(seed["user"]), companies=companies)
            async with client(app) as c:
                empty = await c.get("/api/watchlist")
                await c.post("/api/watchlist", json={"symbol": "005930.KS"})  # 1회(추가 매핑)
                await c.post("/api/watchlist", json={"symbol": "AAPL"})
                listed = await c.get("/api/watchlist")
            return empty, listed

    empty, listed = asyncio.run(go())
    assert empty.json() == {"items": [], "limit": 100}
    assert len(listed.json()["items"]) == 2 and companies.calls == 1


def test_list_backfill_failure_still_lists(pg):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory, with_chat=False)
            async with client(make_app(factory, Who(seed["user"]), companies=None)) as c:
                await c.post("/api/watchlist", json={"symbol": "005930.KS"})
            bad = Companies(exc=RuntimeError("down"))
            async with client(make_app(factory, Who(seed["user"]), companies=bad)) as c:
                return await c.get("/api/watchlist"), bad.calls

    r, calls = asyncio.run(go())
    assert r.status_code == 200 and r.json()["items"][0]["corp_code"] is None and calls == 1


# ── 수용 기준 5: 메모 없는 422 ─────────────────────────────────────────────────

LONG_NOTE = NOTE + "가" * 200


@pytest.mark.parametrize("case,loc", [
    ("note_too_long", ["body", "note"]), ("bad_symbol", ["body", "symbol"]), ("missing_symbol", ["body", "symbol"]),
    ("symbol_too_long", ["body", "symbol"]), ("name_too_long", ["body", "name"]),
    ("exchange_too_long", ["body", "exchange"]), ("note_nul", ["body", "note"]), ("name_nul", ["body", "name"]),
    ("not_json", ["body"]), ("not_object", ["body"]),
])
def test_add_validation_422_never_echoes_input(pg, case, loc):
    secret_symbol = "비밀심볼$$"
    body = {"symbol": "AAPL", "name": "Apple", "note": NOTE}
    kw = {}
    if case == "note_too_long":
        body["note"] = LONG_NOTE
    elif case == "bad_symbol":
        body["symbol"] = secret_symbol
    elif case == "missing_symbol":
        body.pop("symbol")
    elif case == "symbol_too_long":
        body["symbol"] = "A" * 21
    elif case == "name_too_long":
        body["name"] = "비밀이름" + "나" * 100
    elif case == "exchange_too_long":
        body["exchange"] = "비밀거래소" + "X" * 32
    elif case == "note_nul":
        body["note"] = "비밀메모\u0000끝"
    elif case == "name_nul":
        body["name"] = "비밀이름\u0000끝"
    elif case == "not_json":
        kw = {"content": ("{" + NOTE).encode(), "headers": {"content-type": "application/json"}}
    elif case == "not_object":
        body = [NOTE]

    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory, with_chat=False)
            app = make_app(factory, Who(seed["user"]))
            async with client(app) as c:
                r = await c.post("/api/watchlist", **kw) if kw else await c.post("/api/watchlist", json=body)
            return r, await _count(factory)

    r, n = asyncio.run(go())
    assert r.status_code == 422, r.text
    assert n == 0
    for secret in ("비밀메모", "비밀심볼", "비밀이름", "비밀거래소", "가" * 50, "A" * 21):
        assert secret not in r.text
    errs = r.json()["detail"]
    assert all(set(e) == {"loc", "msg"} for e in errs)
    assert errs[0]["loc"] == loc


def test_note_too_long_message_matches_spec(pg):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory, with_chat=False)
            async with client(make_app(factory, Who(seed["user"]))) as c:
                return await c.post("/api/watchlist", json={"symbol": "AAPL", "note": "가" * 201})

    r = asyncio.run(go())
    assert r.json() == {"detail": [{"loc": ["body", "note"], "msg": "메모는 200자 이하입니다."}]}


def test_note_exactly_200_and_symbols_with_special_chars_ok(pg):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory, with_chat=False)
            async with client(make_app(factory, Who(seed["user"]))) as c:
                return [await c.post("/api/watchlist", json={"symbol": s, "note": "가" * 200})
                        for s in ("BRK-B", "CL=F", "1^A", "A" * 20, "^KS11")]

    rs = asyncio.run(go())
    assert [r.status_code for r in rs] == [201, 201, 201, 201, 422]  # spec 5-2 정규식: 첫 글자는 영숫자
    assert [r.json()["symbol"] for r in rs[:4]] == ["BRK-B", "CL=F", "1^A", "A" * 20]


@pytest.mark.parametrize("body,loc", [({"note": LONG_NOTE}, ["body", "note"]), ({}, ["body", "note"]),
                                      ({"note": "비밀메모\u0000"}, ["body", "note"]), ([NOTE], ["body"])])
def test_patch_validation_422_never_echoes_note(pg, body, loc):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory, with_chat=False)
            async with client(make_app(factory, Who(seed["user"]))) as c:
                iid = (await c.post("/api/watchlist", json={"symbol": "AAPL", "note": "원래"})).json()["id"]
                r = await c.patch(f"/api/watchlist/{iid}", json=body)
                after = (await c.get("/api/watchlist")).json()["items"][0]["note"]
            return r, after

    r, after = asyncio.run(go())
    assert r.status_code == 422 and "비밀메모" not in r.text and "가" * 50 not in r.text
    assert all(set(e) == {"loc", "msg"} for e in r.json()["detail"]) and r.json()["detail"][0]["loc"] == loc
    assert after == "원래"


# ── 수용 기준 6: 감사 로그·외부 호출 없음, 로그에 메모 없음 ───────────────────

@pytest.fixture
def audit_db(monkeypatch):
    """감사 로그가 테스트 DB에 실제로 쓰이게 한다(불렸다면 행이 남는다)."""
    from app.services import audit

    holder = {}
    monkeypatch.setattr(audit, "get_session_factory", lambda: holder["factory"])
    return holder


def test_no_audit_no_jev_llm_dispatch_and_note_not_logged(pg, audit_db, caplog, capsys, monkeypatch):
    caplog.set_level(logging.DEBUG)
    from app.services import audit as audit_mod
    from app.services import notification

    calls = {"audit": 0, "dispatch": 0}
    real_audit = audit_mod.audit

    async def counting_audit(*a, **k):
        calls["audit"] += 1
        return await real_audit(*a, **k)

    async def counting_dispatch(*a, **k):
        calls["dispatch"] += 1

    monkeypatch.setattr(audit_mod, "audit", counting_audit)
    monkeypatch.setattr(notification, "dispatch", counting_dispatch)
    jev, llm = FakeJev(), FakeLLM()

    async def go():
        async with database(pg) as factory:
            audit_db["factory"] = factory
            seed = await seed_user(factory, with_chat=False)
            app = make_app(factory, Who(seed["user"]), llm=llm, runner=make_runner(jev),
                           companies=Companies(exc=RuntimeError(NOTE)))
            async with client(app) as c:
                iid = (await c.post("/api/watchlist", json={"symbol": "005930.KS", "note": NOTE})).json()["id"]
                await c.post("/api/watchlist", json={"symbol": "005930.KS", "note": NOTE})  # 409
                await c.post("/api/watchlist", json={"symbol": "AAPL", "note": NOTE + "가" * 200})  # 422
                await c.get("/api/watchlist")
                await c.patch(f"/api/watchlist/{iid}", json={"note": NOTE + " 다시"})
                await c.delete(f"/api/watchlist/{iid}")
            async with factory() as db:
                n_audit = await db.scalar(select(func.count()).select_from(AuditEvent))
            return n_audit

    n_audit = asyncio.run(go())
    assert n_audit == 0 and calls == {"audit": 0, "dispatch": 0}
    assert jev.calls == 0 and llm.calls == []
    out = capsys.readouterr()
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for blob in (logged, out.out, out.err):
        assert "비밀메모" not in blob


def test_watchlist_modules_do_not_import_jev_llm_notification_or_audit():
    """관심종목 코드는 JEV·LLM·알림·감사 로그 모듈을 import하지 않는다(결정 5-4·8-1)."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    files = [root / "app/routes/watchlist.py", root / "app/services/watchlist.py", root / "app/models/watchlist.py"]
    banned = ("app.lib.jev", "app.lib.jev_service", "app.lib.llm_client", "app.lib.ollama",
              "app.services.notification", "app.services.audit", "app.services.stock")
    seen = []
    for f in files:
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                seen += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                seen += [node.module] + [f"{node.module}.{a.name}" for a in node.names]
    assert "app.services.watchlist" in seen  # 실제로 읽었다
    assert not [m for m in seen if m.startswith(banned)]


# ── 수용 기준 7: 관리자 초기화 ────────────────────────────────────────────────

def test_admin_reset_empties_watchlist_and_stats_count_rows(pg):
    async def go():
        async with database(pg) as factory:
            seed = await seed_user(factory, with_chat=False)
            who = Who(seed["user"])
            app = make_app(factory, who)
            async with client(app) as c:
                await c.post("/api/watchlist", json={"symbol": "AAPL", "note": NOTE})
                await c.post("/api/watchlist", json={"symbol": "MSFT"})
                who.user = {**seed["user"], "roles": ["admin"]}
                stats = (await c.get("/api/admin/stats")).json()["stats"]
                reset = await c.post("/api/admin/reset")
            return stats, reset, await _count(factory)

    stats, reset, n = asyncio.run(go())
    assert stats["postgres.watchlist_items"] == 2
    assert "비밀메모" not in json.dumps(stats, ensure_ascii=False) and "AAPL" not in json.dumps(stats)
    assert reset.status_code == 200 and n == 0


def test_admin_user_models_order():
    """관심종목은 부모 users를 지우기 전에 비운다(자식 없음)."""
    from app.models import WatchlistItem as W
    from app.routes import admin

    assert W in admin.USER_MODELS
