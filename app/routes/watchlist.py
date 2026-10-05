"""관심종목 API(모듈 D spec 5절, 11절 P1).

  GET    /api/watchlist        – 내 관심종목 전부(추가한 순서). null인 국내 행의 corp_code를 여기서 채운다(결정 4-3)
  POST   /api/watchlist        – 추가. 201과 항목. 중복·상한은 409(code=duplicate·limit)
  PATCH  /api/watchlist/{id}   – 메모만 고친다
  DELETE /api/watchlist/{id}   – 삭제. 204

- 기능 플래그가 없다(결정 5-5). 모든 경로는 로그인(get_current_user_any)이 필요하다.
- 남의 항목·없는 id·UUID가 아닌 id는 모두 404다(존재 여부를 드러내지 않는다).
- 메모는 어디로도 나가지 않는다(결정 8-1): JEV·LLM·알림·감사 로그를 부르지 않고(결정 5-4), 로그에는 예외 이름만 남긴다.
  검증 오류(422)는 본문을 직접 검사해 칸 이름과 사유(loc·msg)만 돌려준다(input 없음).
- Yahoo를 부르지 않는다(가격·등락을 담지 않는다). corp_code는 근거 모드 적재 회사 목록에서만 정한다(지어내지 않는다).
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from pydantic_core import PydanticCustomError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.postgres import get_pg_session
from app.lib.jwt_auth import get_current_user_any
from app.models import WatchlistItem
from app.routes.evidence import get_company_list
from app.services import watchlist as wl

log = logging.getLogger("app.watchlist.api")

MAX_ITEMS = 100  # 사용자당 상한(결정 4-2). 설정이 아니라 상수다
NAME_MAX, NOTE_MAX, EXCHANGE_MAX = 100, 200, 32
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-^=]{0,19}$")
UNIQUE_SYMBOL = "uq_watchlist_user_symbol"
NOT_FOUND = "관심종목을 찾을 수 없습니다."

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


# ── 스키마(본문은 직접 검사한다: 422에 메모가 되돌아가지 않게) ─────────────────────

def _text(v: Optional[str], *, max_len: int, too_long: str, nul: str, strip: bool) -> Optional[str]:
    """선택 문자열 칸 정리: (strip이면) 앞뒤 공백 제거, 빈 문자열은 None, 길이·NUL 검사.

    @param v 받은 값(None 가능)
    @param max_len 최대 글자 수
    @param too_long 길이 초과 사유
    @param nul NUL 포함 사유(PostgreSQL varchar는 NUL을 받지 않는다 — 그대로 두면 저장에서 500)
    @param strip 앞뒤 공백을 지울지
    @returns 정리한 값 또는 None
    """
    if v is None:
        return None
    if strip:
        v = v.strip()
    if "\x00" in v:
        raise PydanticCustomError("nul_char", nul)
    if len(v) > max_len:
        raise PydanticCustomError("too_long", too_long)
    return v or None


class WatchlistCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")  # 알 수 없는 칸(corp_code·market 포함)은 쓰지 않는다

    symbol: str
    name: Optional[str] = None
    exchange: Optional[str] = None
    note: Optional[str] = None

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v: str) -> str:
        v = v.strip().upper()
        if not SYMBOL_RE.match(v):
            raise PydanticCustomError("bad_symbol", "종목 심볼 형식이 아닙니다.")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: Optional[str]) -> Optional[str]:
        return _text(v, max_len=NAME_MAX, too_long="이름은 100자 이하입니다.",
                     nul="이름에 NUL 문자를 쓸 수 없습니다.", strip=True)

    @field_validator("exchange")
    @classmethod
    def _exchange(cls, v: Optional[str]) -> Optional[str]:
        return _text(v, max_len=EXCHANGE_MAX, too_long="거래소는 32자 이하입니다.",
                     nul="거래소에 NUL 문자를 쓸 수 없습니다.", strip=True)

    @field_validator("note")
    @classmethod
    def _note(cls, v: Optional[str]) -> Optional[str]:
        return _note(v)


class WatchlistNote(BaseModel):
    model_config = ConfigDict(extra="ignore")

    note: Optional[str]  # 칸은 필수, null·빈 문자열은 메모 지우기

    @field_validator("note")
    @classmethod
    def _note(cls, v: Optional[str]) -> Optional[str]:
        return _note(v)


def _note(v: Optional[str]) -> Optional[str]:
    """메모 검사: 200자 이하, NUL 없음, 빈 문자열은 None. 줄바꿈·공백은 그대로 둔다.

    @param v 받은 메모
    @returns 저장할 메모 또는 None
    """
    return _text(v, max_len=NOTE_MAX, too_long="메모는 200자 이하입니다.",
                 nul="메모에 NUL 문자를 쓸 수 없습니다.", strip=False)


def _invalid(errors: list[dict]) -> HTTPException:
    """칸 이름과 사유만 담은 422.

    @param errors `{"loc", "msg"}` 목록(입력값 없음)
    @returns 던질 HTTPException
    """
    return HTTPException(422, detail=errors)


async def _parse(request: Request, model: type[BaseModel]) -> BaseModel:
    """본문을 읽어 검사한다. 실패하면 칸 이름과 사유만 담은 422(input·ctx 없음). 일지 `_parse`와 같은 방식.

    @param request 요청
    @param model 검사할 pydantic 모델
    @returns 검사를 통과한 모델 인스턴스
    """
    try:
        raw = json.loads(await request.body() or b"null")
    except ValueError:
        raise _invalid([{"loc": ["body"], "msg": "JSON 본문이 아닙니다."}])
    try:
        return model.model_validate(raw)
    except ValidationError as e:
        raise _invalid([{"loc": ["body", *err["loc"]], "msg": err["msg"]}
                        for err in e.errors(include_url=False, include_context=False, include_input=False)])


def _constraint(exc: IntegrityError) -> str | None:
    """asyncpg 원 예외의 제약 이름. 예외 문자열(메모가 든 파라미터 포함)은 보지 않는다.

    @param exc SQLAlchemy 무결성 오류
    @returns 제약 이름, 알 수 없으면 None
    """
    return getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)


# ── 헬퍼 ──────────────────────────────────────────────────────────────────────

def _uuid_or_404(raw: str) -> uuid.UUID:
    """경로의 id를 UUID로 바꾼다. 형식이 아니면 없는 항목과 같은 404.

    @param raw 경로 문자열
    @returns UUID
    """
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise HTTPException(404, NOT_FOUND)


async def _owned_item(db: AsyncSession, item_id: str, user: dict) -> WatchlistItem:
    """내 항목을 id로 찾는다. 남의 항목·없는 id는 404.

    @param db DB 세션
    @param item_id 경로의 id
    @param user 현재 사용자
    @returns 내 관심종목 행
    """
    item = (await db.execute(select(WatchlistItem).where(WatchlistItem.id == _uuid_or_404(item_id),
                                                         WatchlistItem.user_id == uuid.UUID(user["id"])))
            ).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, NOT_FOUND)
    return item


def _item(i: WatchlistItem) -> dict:
    """응답용 항목 dict.

    @param i 관심종목 행
    @returns id·symbol·name·corp_code·market·note·created_at
    """
    return {"id": str(i.id), "symbol": i.symbol, "name": i.name, "corp_code": i.corp_code, "market": i.market,
            "note": i.note, "created_at": i.created_at.isoformat() if i.created_at else None}


def _duplicate(item_id: uuid.UUID | None) -> JSONResponse:
    """중복 409(결정 5-3). 화면은 이것을 '이미 있음'으로 받아 별만 채운다.

    @param item_id 이미 있는 항목 id(경쟁으로 다시 못 찾으면 None)
    @returns 409 응답 `{"detail", "code": "duplicate", "item_id"}`
    """
    return JSONResponse(status_code=409, content={"detail": "이미 관심종목에 있습니다.", "code": "duplicate",
                                                  "item_id": str(item_id) if item_id else None})


async def _existing_id(db: AsyncSession, uid: uuid.UUID, symbol: str) -> uuid.UUID | None:
    """같은 사용자·심볼 항목의 id.

    @param db DB 세션
    @param uid 사용자 id
    @param symbol 정규화한 심볼
    @returns 있으면 id, 없으면 None
    """
    return await db.scalar(select(WatchlistItem.id).where(WatchlistItem.user_id == uid,
                                                          WatchlistItem.symbol == symbol))


# ── 엔드포인트 ─────────────────────────────────────────────────────────────────

@router.get("", summary="내 관심종목 목록")
async def list_items(user=Depends(get_current_user_any), db: AsyncSession = Depends(get_pg_session),
                     companies=Depends(get_company_list)):
    """추가한 순서로 전부 돌려준다(상한 100개라 페이지 없음).

    corp_code가 null인 국내(`NNNNNN.KS/KQ`) 행이 있고 문단 저장소가 연결돼 있으면 적재 회사 목록을 한 번 불러
    맞는 행을 채우고 저장한다(결정 4-3 나중에 채우기). 한 번 채운 값은 지우지 않는다.

    @param user 현재 사용자
    @param db DB 세션
    @param companies 문단 저장소의 companies(근거 모드가 꺼져 연결이 없으면 None)
    @returns `{"items": [항목...], "limit": 100}`
    """
    uid = uuid.UUID(user["id"])
    rows = (await db.execute(select(WatchlistItem).where(WatchlistItem.user_id == uid)
                             .order_by(WatchlistItem.created_at, WatchlistItem.id))).scalars().all()
    items = [_item(r) for r in rows]
    todo = [i for i in items if i["corp_code"] is None and wl.stock_code_of(i["symbol"])]
    if todo and companies is not None:
        await db.rollback()  # 읽기만 했다. 저장소를 기다리는 동안 PG 연결을 쥐고 있지 않게 풀에 돌려준다
        loaded = await wl.load_companies(companies)
        filled = [(i, code) for i in todo if (code := wl.corp_code_for(wl.stock_code_of(i["symbol"]), loaded))]
        if filled:
            try:
                for i, code in filled:
                    await db.execute(update(WatchlistItem)
                                     .where(WatchlistItem.id == uuid.UUID(i["id"]), WatchlistItem.corp_code.is_(None))
                                     .values(corp_code=code))
                await db.commit()
            except Exception as exc:  # noqa: BLE001 — 채우기 실패는 목록을 막지 않는다
                await db.rollback()
                log.error(json.dumps({"event": "watchlist_backfill_failed", "error": type(exc).__name__}))
            else:
                for i, code in filled:
                    i["corp_code"] = code
    return {"items": items, "limit": MAX_ITEMS}


@router.post("", status_code=201, summary="관심종목 추가")
async def add_item(request: Request, user=Depends(get_current_user_any), db: AsyncSession = Depends(get_pg_session),
                   companies=Depends(get_company_list)):
    """종목 하나를 더한다. 같은 심볼이 있으면 409 duplicate(기존 id), 이미 100개면 409 limit.

    중복을 먼저 본다(100개인 사용자가 이미 있는 종목을 다시 눌러도 화면이 '이미 있음'으로 받게).
    국내 심볼이면 적재 회사 목록을 한 번 불러 corp_code를 정한다. 실패해도 null로 추가한다.

    @param request 본문 `{"symbol", "name"?, "exchange"?, "note"?}`(그 밖의 칸은 무시)
    @param user 현재 사용자
    @param db DB 세션
    @param companies 문단 저장소의 companies(없으면 None)
    @returns 201과 항목, 409 duplicate·limit, 422(loc·msg만)
    """
    body: WatchlistCreate = await _parse(request, WatchlistCreate)  # type: ignore[assignment]
    uid = uuid.UUID(user["id"])
    existing = await _existing_id(db, uid, body.symbol)
    if existing is not None:
        return _duplicate(existing)
    count = await db.scalar(select(func.count()).select_from(WatchlistItem).where(WatchlistItem.user_id == uid))
    if count >= MAX_ITEMS:
        return JSONResponse(status_code=409, content={"detail": f"관심종목은 {MAX_ITEMS}개까지 담을 수 있습니다.",
                                                      "code": "limit", "limit": MAX_ITEMS})
    corp_code = None
    stock_code = wl.stock_code_of(body.symbol)
    if stock_code:
        await db.rollback()  # 저장소를 기다리는 동안 PG 연결을 풀에 돌려준다
        corp_code = wl.corp_code_for(stock_code, await wl.load_companies(companies))

    item = WatchlistItem(id=uuid.uuid4(), user_id=uid, symbol=body.symbol, name=body.name, corp_code=corp_code,
                         market=wl.market_of(body.symbol, body.exchange), note=body.note,
                         created_at=datetime.now(timezone.utc))
    out = _item(item)  # 커밋·롤백 뒤에는 속성을 읽지 않는다
    db.add(item)
    try:
        await db.commit()
    except IntegrityError as exc:  # 예외 문자열에는 메모가 든 파라미터가 있다 — 로그에 넣지 않는다
        await db.rollback()
        if _constraint(exc) != UNIQUE_SYMBOL:
            log.error(json.dumps({"event": "watchlist_save_failed", "error": type(exc).__name__}))
            raise HTTPException(500, "관심종목을 저장하지 못했습니다.")
        # 존재 확인과 커밋 사이에 같은 종목이 먼저 저장됐다
        return _duplicate(await _existing_id(db, uid, body.symbol))
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        log.error(json.dumps({"event": "watchlist_save_failed", "error": type(exc).__name__}))
        raise HTTPException(500, "관심종목을 저장하지 못했습니다.")
    return JSONResponse(status_code=201, content=out)


@router.patch("/{item_id}", summary="관심종목 메모 고치기")
async def update_note(item_id: str, request: Request, user=Depends(get_current_user_any),
                      db: AsyncSession = Depends(get_pg_session)):
    """메모만 고친다. 빈 문자열·null은 메모 지우기다.

    @param item_id 항목 id(내 것이 아니면 404)
    @param request 본문 `{"note"}`
    @param user 현재 사용자
    @param db DB 세션
    @returns 200과 항목
    """
    body: WatchlistNote = await _parse(request, WatchlistNote)  # type: ignore[assignment]
    item = await _owned_item(db, item_id, user)
    item.note = body.note
    out = _item(item)
    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        log.error(json.dumps({"event": "watchlist_note_save_failed", "error": type(exc).__name__}))
        raise HTTPException(500, "관심종목을 저장하지 못했습니다.")
    return out


@router.delete("/{item_id}", status_code=204, summary="관심종목 삭제")
async def delete_item(item_id: str, user=Depends(get_current_user_any), db: AsyncSession = Depends(get_pg_session)):
    """내 항목 하나를 지운다.

    @param item_id 항목 id(내 것이 아니면 404)
    @param user 현재 사용자
    @param db DB 세션
    @returns 204(본문 없음)
    """
    item = await _owned_item(db, item_id, user)
    await db.delete(item)
    await db.commit()
    return Response(status_code=204)
