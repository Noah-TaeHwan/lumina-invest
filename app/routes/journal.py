"""투자 판단 일지 API(모듈 C spec 5.1, 10절 P1 저장과 API).

  POST   /api/journal                       – 끝난 판정 실행 하나에서 판단 기록 생성(스냅샷은 서버가 복사, 결정 5-5)
  GET    /api/journal                       – 내 기록 목록(최근 판단 기준 거르기, total·due_count)
  GET    /api/journal/export                – 내 기록 내보내기(JSON 하나, 고지·AI 생성 표시, 결정 7-2)
  GET    /api/journal/{id}                  – 상세: 스냅샷, update 목록, source_available(원 실행 존재 여부)
  GET    /api/journal/{id}/changes          – 이후 공시 변화: 스냅샷과 지금 적재된 문단 비교(결정 6-1, 저장하지 않음)
  POST   /api/journal/{id}/updates          – 다시 보기 기록 덧붙이기(kind=revisit). 고치기·1건 삭제는 없다
  DELETE /api/journal/{id}                  – 기록과 update 전부 삭제
  DELETE /api/journal?confirm=delete-all    – 내 기록 전부 삭제(쿼리가 정확히 이 값일 때만, 아니면 400)

- JOURNAL_ENABLED 하나로만 막는다(꺼지면 모든 경로 404, 로그인 여부와 무관). 근거 모드 플래그와 묶지 않는다(결정 5-6).
- 남의 기록·실행은 404다(존재 여부를 드러내지 않는다).
- 메모는 어디로도 나가지 않는다(결정 7-4): JEV·LLM·알림을 부르지 않고, 감사 로그에는 사건 종류와 개수·id만,
  로그에는 예외 이름만 남긴다. 검증 오류(422)는 본문을 직접 검사해 칸 이름과 사유(loc·msg)만 돌려준다(input 없음).
  PostgreSQL text가 받지 않는 NUL도 저장 전에 422로 거른다.
- 판단은 덧붙이기만 한다(결정 5-2). 현재 판단 = 그 기록의 가장 최근 update(created_at, 같으면 id 순서의 마지막).
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.postgres import get_pg_session
from app.lib.jwt_auth import get_current_user_any
from app.models import Chat, EvidenceRun, JudgmentEntry, JudgmentUpdate
from app.models.journal import DECISIONS
from app.services.audit import audit
from app.services.evidence import records
from app.routes.evidence import get_passage_store
from app.services.journal import changes as chg
from app.services.journal import snapshot as snap

log = logging.getLogger("app.journal.api")

MEMO_MAX = 2000
UNIQUE_RUN = "uq_judgment_entries_user_run"
KST = timezone(timedelta(hours=9))  # 서머타임 없음. tzdata에 기대지 않는다(risk_guard와 같은 방식)
NOTICE = ("판단 일지는 내가 쓴 기록입니다. 답변과 배지는 AI가 만든 것으로, 배지는 검색된 공시 문단 기준 AI 판정"
          "(TypeSafe의 JEV 모델)이며 사실 여부를 보증하지 않습니다. 이 서비스는 투자 권유나 수익 예측을 하지 않으며, "
          "투자 판단과 그 결과는 본인에게 있습니다. 이 프로젝트는 TypeSafe와 제휴 관계가 아닙니다.")  # spec 결정 4-2


def require_enabled() -> None:
    if not settings.JOURNAL_ENABLED:
        raise HTTPException(404, "Not Found")


router = APIRouter(prefix="/api/journal", tags=["journal"], dependencies=[Depends(require_enabled)])


def today_kst() -> date:
    return datetime.now(KST).date()


# ── 스키마(본문은 직접 검사한다: 422에 메모가 되돌아가지 않게) ─────────────────────

class JudgmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")  # 스냅샷 등 서버가 정하는 칸은 받지 않는다

    decision: Literal[DECISIONS]  # type: ignore[valid-type]
    conviction: int = Field(ge=1, le=5, strict=True)
    memo: str = Field("", max_length=MEMO_MAX)
    relied_claims: list[int] = Field(default_factory=list, max_length=100)
    review_on: Optional[date] = None

    @field_validator("memo")
    @classmethod
    def _no_nul(cls, v: str) -> str:
        if "\x00" in v:  # PostgreSQL text는 NUL을 받지 않는다(그대로 두면 저장에서 500)
            raise ValueError("메모에 NUL 문자를 쓸 수 없습니다.")
        return v


class EntryCreate(JudgmentInput):
    run_id: uuid.UUID


def _invalid(errors: list[dict]) -> HTTPException:
    return HTTPException(422, detail=errors)


async def _parse(request: Request, model: type[BaseModel]) -> BaseModel:
    """본문을 읽어 검사한다. 실패하면 칸 이름과 사유만 담은 422(input·ctx 없음)."""
    try:
        raw = json.loads(await request.body() or b"null")
    except ValueError:
        raise _invalid([{"loc": ["body"], "msg": "JSON 본문이 아닙니다."}])
    try:
        return model.model_validate(raw)
    except ValidationError as e:
        raise _invalid([{"loc": ["body", *err["loc"]], "msg": err["msg"]}
                        for err in e.errors(include_url=False, include_context=False, include_input=False)])


def _check_relied(relied: list[int], snapshot: dict) -> list[int]:
    picked = sorted(set(relied))
    if not set(picked) <= snap.selectable_claims(snapshot):
        raise _invalid([{"loc": ["body", "relied_claims"],
                         "msg": "기댄 문장은 이 기록의 판정 문장(비주장 제외) 번호여야 합니다."}])
    return picked


def _constraint(exc: IntegrityError) -> str | None:
    """asyncpg 원 예외의 제약 이름. 예외 문자열(메모가 든 파라미터 포함)은 보지 않는다."""
    return getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)


# ── 헬퍼 ──────────────────────────────────────────────────────────────────────

def _uuid_or_404(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise HTTPException(404, "기록을 찾을 수 없습니다.")


async def _owned_entry(db: AsyncSession, entry_id: str, user: dict) -> JudgmentEntry:
    entry = (await db.execute(select(JudgmentEntry).where(JudgmentEntry.id == _uuid_or_404(entry_id),
                                                          JudgmentEntry.user_id == uuid.UUID(user["id"])))
             ).scalar_one_or_none()
    if entry is None:
        raise HTTPException(404, "기록을 찾을 수 없습니다.")
    return entry


async def _updates(db: AsyncSession, entry_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[JudgmentUpdate]]:
    out: dict[uuid.UUID, list[JudgmentUpdate]] = {i: [] for i in entry_ids}
    if entry_ids:
        rows = await db.execute(select(JudgmentUpdate).where(JudgmentUpdate.entry_id.in_(entry_ids))
                                .order_by(JudgmentUpdate.entry_id, JudgmentUpdate.created_at, JudgmentUpdate.id))
        for u in rows.scalars():
            out[u.entry_id].append(u)
    return out


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _update(u: JudgmentUpdate) -> dict:
    return {"id": str(u.id), "kind": u.kind, "decision": u.decision, "conviction": u.conviction, "memo": u.memo,
            "relied_claims": u.relied_claims, "review_on": _iso(u.review_on), "created_at": _iso(u.created_at)}


def _is_due(latest: JudgmentUpdate | None, today: date) -> bool:
    return bool(latest and latest.review_on and latest.review_on <= today)


def _entry_head(e: JudgmentEntry) -> dict:
    return {"id": str(e.id), "run_id": str(e.run_id), "company": e.company, "corp_code": e.corp_code,
            "snapshot_version": e.snapshot_version, "created_at": _iso(e.created_at)}


async def _detail(db: AsyncSession, e: JudgmentEntry) -> dict:
    ups = (await _updates(db, [e.id]))[e.id]
    source = await db.scalar(select(EvidenceRun.id).where(EvidenceRun.id == e.run_id,
                                                          EvidenceRun.user_id == e.user_id))
    return {**_entry_head(e), "snapshot": e.snapshot, "updates": [_update(u) for u in ups],
            "source_available": source is not None}


def _ai_marked(snapshot: dict) -> dict:
    """내보내기용: AI가 만든 칸(답변·문장 상태·확신도 라벨)에 ai_generated 표시를 단다(AUP 1.4, 결정 7-2)."""
    return {**snapshot, "answer": {"text": snapshot.get("answer"), "ai_generated": True},
            "claims": [{**c, "ai_generated": True} for c in snapshot.get("claims", [])]}


# ── 엔드포인트 ─────────────────────────────────────────────────────────────────

@router.post("", status_code=201, summary="판정 실행 하나에서 판단 기록 생성")
async def create_entry(request: Request, user=Depends(get_current_user_any),
                       db: AsyncSession = Depends(get_pg_session)):
    body: EntryCreate = await _parse(request, EntryCreate)  # type: ignore[assignment]
    uid = uuid.UUID(user["id"])
    run = (await db.execute(select(EvidenceRun).where(EvidenceRun.id == body.run_id, EvidenceRun.user_id == uid))
           ).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "판정 기록을 찾을 수 없습니다.")
    if await records.expire_stale(db, [run], records.now()):
        await db.commit()
    if run.status in records.ACTIVE:
        raise HTTPException(409, "판정이 끝난 뒤 기록할 수 있습니다.")
    existing = await db.scalar(select(JudgmentEntry.id).where(JudgmentEntry.user_id == uid,
                                                               JudgmentEntry.run_id == run.id))
    if existing is not None:
        return JSONResponse(status_code=409, content={"detail": "이 판정에는 이미 기록이 있습니다.",
                                                      "entry_id": str(existing)})
    chat = await db.get(Chat, run.chat_id)
    if chat is None:
        raise HTTPException(409, "원래 답변을 찾을 수 없어 기록할 수 없습니다.")
    claims = (await records.load_claims(db, [run.id]))[run.id]
    snapshot = snap.build_snapshot(run, claims, chat)
    relied = _check_relied(body.relied_claims, snapshot)

    run_id, at = run.id, records.now()  # rollback 뒤에는 run 속성을 읽을 수 없다(만료)
    entry = JudgmentEntry(id=uuid.uuid4(), user_id=uid, run_id=run.id, company=run.company, corp_code=run.corp_code,
                          snapshot=snapshot, snapshot_version=snap.SNAPSHOT_VERSION, created_at=at)
    db.add(entry)
    db.add(JudgmentUpdate(entry_id=entry.id, kind="initial", decision=body.decision, conviction=body.conviction,
                          memo=body.memo, relied_claims=relied, review_on=body.review_on, created_at=at))
    try:
        await db.commit()
    except IntegrityError as exc:  # 예외 문자열에는 메모가 든 파라미터가 있다 — 로그에 넣지 않는다
        await db.rollback()
        if _constraint(exc) != UNIQUE_RUN:
            log.error(json.dumps({"event": "journal_save_failed", "error": type(exc).__name__}))
            raise HTTPException(500, "기록을 저장하지 못했습니다.")
        # 존재 확인과 커밋 사이에 같은 실행의 기록이 먼저 저장됐다
        existing = await db.scalar(select(JudgmentEntry.id).where(JudgmentEntry.user_id == uid,
                                                                   JudgmentEntry.run_id == run_id))
        return JSONResponse(status_code=409, content={"detail": "이 판정에는 이미 기록이 있습니다.",
                                                      "entry_id": str(existing) if existing else None})
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        log.error(json.dumps({"event": "journal_save_failed", "error": type(exc).__name__}))
        raise HTTPException(500, "기록을 저장하지 못했습니다.")
    await audit(user["id"], user.get("client_id", ""), "journal.create", {"entry_id": str(entry.id)})
    return JSONResponse(status_code=201, content=await _detail(db, entry))


@router.get("", summary="내 판단 기록 목록")
async def list_entries(
    due: bool = Query(False, description="true면 다시 볼 날짜가 오늘(KST) 이하인 기록만"),
    corp_code: str = Query("", max_length=16),
    decision: Optional[Literal[DECISIONS]] = Query(None, description="최근 판단으로 거르기"),  # type: ignore[valid-type]
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user=Depends(get_current_user_any),
    db: AsyncSession = Depends(get_pg_session),
):
    """거르기는 최근 판단 기준이다. total은 거른 뒤 개수, due_count는 거르기와 무관하게 내 기록 전체에서 센다(일지 탭 점 표시)."""
    uid = uuid.UUID(user["id"])
    rows = (await db.execute(
        select(JudgmentEntry.id, JudgmentEntry.run_id, JudgmentEntry.company, JudgmentEntry.corp_code,
               JudgmentEntry.created_at, JudgmentEntry.snapshot["question"].astext,
               JudgmentEntry.snapshot["run"]["policy_version"].astext, JudgmentEntry.snapshot["run"]["status"].astext)
        .where(JudgmentEntry.user_id == uid)
        .order_by(JudgmentEntry.created_at.desc(), JudgmentEntry.id.desc()))).all()
    ups = await _updates(db, [r[0] for r in rows])
    today = today_kst()
    items = []
    for eid, run_id, company, corp, created, question, policy, run_status in rows:
        latest = ups[eid][-1] if ups[eid] else None
        items.append({
            "id": str(eid), "run_id": str(run_id), "company": company, "corp_code": corp, "question": question,
            "policy_version": policy, "run_status": run_status, "created_at": _iso(created),
            "current": ({"decision": latest.decision, "conviction": latest.conviction,
                         "review_on": _iso(latest.review_on), "created_at": _iso(latest.created_at)}
                        if latest else None),
            "update_count": len(ups[eid]), "due": _is_due(latest, today),
        })
    due_count = sum(i["due"] for i in items)
    picked = [i for i in items
              if (not due or i["due"]) and (not corp_code or i["corp_code"] == corp_code)
              and (decision is None or (i["current"] and i["current"]["decision"] == decision))]
    return {"items": picked[offset:offset + limit], "total": len(picked), "due_count": due_count,
            "limit": limit, "offset": offset}


@router.get("/export", summary="내 판단 기록 내보내기(JSON)")
async def export_entries(user=Depends(get_current_user_any), db: AsyncSession = Depends(get_pg_session)):
    entries = list((await db.execute(select(JudgmentEntry).where(JudgmentEntry.user_id == uuid.UUID(user["id"]))
                                     .order_by(JudgmentEntry.created_at, JudgmentEntry.id))).scalars())
    ups = await _updates(db, [e.id for e in entries])
    today = today_kst()
    out = {
        "notice": NOTICE, "exported_at": records.now().isoformat(), "count": len(entries),
        "entries": [{**_entry_head(e), "snapshot": _ai_marked(e.snapshot),
                     "updates": [{**_update(u), "ai_generated": False} for u in ups[e.id]]} for e in entries],
    }
    await audit(user["id"], user.get("client_id", ""), "journal.export", {"count": len(entries)})
    return JSONResponse(out, headers={
        "Content-Disposition": f'attachment; filename="lumina-journal-{today:%Y%m%d}.json"'})


@router.get("/{entry_id}", summary="판단 기록 상세")
async def get_entry(entry_id: str, user=Depends(get_current_user_any), db: AsyncSession = Depends(get_pg_session)):
    return await _detail(db, await _owned_entry(db, entry_id, user))


@router.get("/{entry_id}/changes", summary="판단 기록 이후 공시 변화(지금 적재된 문단과 비교)")
async def get_changes(entry_id: str, user=Depends(get_current_user_any), db: AsyncSession = Depends(get_pg_session),
                      passage_store=Depends(get_passage_store)):
    """외부 호출 없이 그 회사 문단의 Qdrant scroll 한 번만 쓴다. 비교 결과는 저장하지 않는다(결정 6-2).

    확인할 수 없으면(저장소 미연결·컬렉션 없음·연결 실패) `{"status": "unavailable"}` 하나만 돌려준다. 그 밖에는:

        {"status": "ok",
         "report":   {"status": "same" | "replaced" | "company_gone",
                      "snapshot_rcept_no": 스냅샷 rcept_no, "current_rcept_nos": [지금 rcept_no...]},
         "passages": [{"passage_idx": 스냅샷 passages 위치, "passage_id", "sha256", "status": "same" | "gone"}],
         "policy":   {"snapshot": 스냅샷 policy_version, "current": 현재 정책}}

    gone 문단의 대체 본문은 돌려주지 않는다(새 근거는 새 질문으로만 본다).
    """
    entry = await _owned_entry(db, entry_id, user)
    return await chg.compare(passage_store, entry.snapshot)


@router.post("/{entry_id}/updates", status_code=201, summary="다시 보기 기록 덧붙이기")
async def add_update(entry_id: str, request: Request, user=Depends(get_current_user_any),
                     db: AsyncSession = Depends(get_pg_session)):
    body: JudgmentInput = await _parse(request, JudgmentInput)  # type: ignore[assignment]
    entry = await _owned_entry(db, entry_id, user)
    relied = _check_relied(body.relied_claims, entry.snapshot)
    u = JudgmentUpdate(id=uuid.uuid4(), entry_id=entry.id, kind="revisit", decision=body.decision,
                       conviction=body.conviction, memo=body.memo, relied_claims=relied, review_on=body.review_on,
                       created_at=records.now())
    db.add(u)
    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        log.error(json.dumps({"event": "journal_update_save_failed", "error": type(exc).__name__}))
        raise HTTPException(500, "기록을 저장하지 못했습니다.")
    return JSONResponse(status_code=201, content=_update(u))


@router.delete("/{entry_id}", summary="판단 기록 삭제(update 포함)")
async def delete_entry(entry_id: str, user=Depends(get_current_user_any),
                       db: AsyncSession = Depends(get_pg_session)):
    entry = await _owned_entry(db, entry_id, user)
    await db.execute(delete(JudgmentEntry).where(JudgmentEntry.id == entry.id))  # update는 DB cascade
    await db.commit()
    await audit(user["id"], user.get("client_id", ""), "journal.delete", {"count": 1})
    return {"deleted": 1}


@router.delete("", summary="내 판단 기록 전부 삭제(confirm=delete-all 필요)")
async def delete_all(confirm: str = Query("", description="정확히 delete-all이어야 지운다"),
                     user=Depends(get_current_user_any), db: AsyncSession = Depends(get_pg_session)):
    if confirm != "delete-all":
        raise HTTPException(400, "전체 삭제는 confirm=delete-all이 필요합니다.")
    res = await db.execute(delete(JudgmentEntry).where(JudgmentEntry.user_id == uuid.UUID(user["id"]))
                           .returning(JudgmentEntry.id))
    count = len(res.all())
    await db.commit()
    await audit(user["id"], user.get("client_id", ""), "journal.delete", {"count": count})
    return {"deleted": count}
