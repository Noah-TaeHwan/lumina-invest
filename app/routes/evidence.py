"""근거 모드 채팅 API(A-2 spec 결정 4-1·4-2·4-4).

  POST /api/evidence/chat                   – 공시 문단 검색 → 답변 생성 → 저장 → 판정 작업 등록
  GET  /api/evidence/runs/{run_id}          – 판정 실행과 문장 목록(소유자만, 아니면 404)
  POST /api/evidence/runs/{run_id}/retry    – 다시 판정(failed·partial만), 새 실행(trigger=retry)
  GET  /api/conversations/{cid}/evidence    – 스레드의 판정 실행 타임라인(기본 메시지별 최신, all=true면 전체)
  GET  /api/evidence/companies              – 회사 선택 후보(문단이 적재된 회사만, q가 있으면 KRX 검색과 교집합)
  GET  /api/evidence/notice                 – 외부 전송 고지를 확인했는가(spec 8절, Redis user_state)
  POST /api/evidence/notice                 – 고지 확인 기록(처음 모드를 켤 때 화면이 부른다)

- EVIDENCE_CHAT_ENABLED가 꺼져 있으면 모든 경로가 404다(로그인 여부와 무관).
- 판정은 답변·실행 행을 커밋한 뒤 앱 프로세스 안 asyncio 작업으로 돈다. 저장에 실패하면 판정을 시작하지 않고 500.
- 문단 검색은 주입 가능한 함수 하나(get_passage_search)다. 문단 저장소(P3, Qdrant evidence_passages)가
  연결되기 전에는 None이라 503을 낸다. 연결 뒤 Qdrant·컬렉션이 없으면 검색이 예외를 내고 역시 503이다.
  검색 함수: async (corp_code, question) -> 문단 dict 8개
  (passage_id, rcept_no, section, idx, sha256, text). 회사 목록(get_company_list)도 같은 저장소에서 온다.
  앱 시작 시 store.wire()가 set_passage_store로 저장소 하나를 연결한다(EVIDENCE_CHAT_ENABLED일 때).
- 근거 모드는 이력 없는 단발형이다(generate_answer는 이력을 받지 않는다). 기존 /api/chat·에이전트는 건드리지 않는다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Awaitable, Callable, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import postgres
from app.database.postgres import get_pg_session
from app.lib.guardrails import check_guardrails
from app.lib.jwt_auth import get_current_user_any
from app.lib.llm_client import get_llm_client
from app.lib.user_state import get_user_state, set_active_conversation, update_user_state
from app.models import Chat, Conversation, EvidenceRun
from app.routes.conversations import _assert_owner
from app.services.conversation_threads import get_or_create_conversation
from app.services.evidence import background, records
from app.services.evidence.generate import generate_answer

log = logging.getLogger("app.evidence.api")

PassageSearch = Callable[[str, str], Awaitable[list[dict]]]
CompanyList = Callable[[], Awaitable[list[dict]]]
KrxSearch = Callable[..., Awaitable[list[dict]]]
KRX_SEARCH_LIMIT = 50
GENERATE_TIMEOUT_S = 60.0  # spec 7.1: 측정 전 잠정값. P5에서 생성 지연 p95의 2배로 다시 정한다
CITATION_FIELDS = ("passage_id", "rcept_no", "section", "idx", "sha256")
NOTICE_VERSION = "a2-notice-v1"  # 고지 문구가 바뀌면 올린다(다시 확인을 받는다)


def require_enabled() -> None:
    if not settings.EVIDENCE_CHAT_ENABLED:
        raise HTTPException(404, "Not Found")


router = APIRouter(prefix="/api", tags=["evidence"], dependencies=[Depends(require_enabled)])


# ── 주입 지점(테스트·P3에서 바꾼다) ────────────────────────────────────────────

_passage_store = None  # search(corp_code, question)·companies()를 가진 저장소(store.PassageStore)


def set_passage_store(store) -> None:
    """P3에서 Qdrant evidence_passages 저장소를 연결한다(None이면 끊는다)."""
    global _passage_store
    _passage_store = store


def get_passage_search() -> PassageSearch | None:
    return _passage_store.search if _passage_store is not None else None


def get_company_list() -> CompanyList | None:
    return _passage_store.companies if _passage_store is not None else None


def get_krx_search() -> KrxSearch:
    """KRX 종목 검색(krx_companies.search_companies). bs4 등을 끌어오므로 필요할 때 import한다."""
    async def search(q: str, limit: int = 10) -> list[dict]:
        from app.services.krx_companies import search_companies
        return await search_companies(q, limit)
    return search


def get_runner():
    """프로세스 단일 실행기. Redis가 없으면 None — 실행을 failed(quota_unavailable)로 남긴다(spec 7.3)."""
    try:
        return background.get_runner()
    except RuntimeError:
        return None


def get_session_factory():
    return postgres.get_session_factory()


# ── 스키마 ─────────────────────────────────────────────────────────────────────

class EvidenceChatBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    company: str = Field(min_length=1, max_length=200)  # 회사명(판정 state에 들어간다)
    corp_code: str = Field(min_length=1, max_length=16)  # DART 고유번호(문단 검색 필터)
    conversation_id: Optional[str] = None


# ── 헬퍼 ──────────────────────────────────────────────────────────────────────

def _uuid_or_404(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise HTTPException(404, "판정 기록을 찾을 수 없습니다.")


async def _owned_run(db: AsyncSession, run_id: str, user: dict) -> EvidenceRun:
    run = (await db.execute(select(EvidenceRun).where(EvidenceRun.id == _uuid_or_404(run_id),
                                                      EvidenceRun.user_id == uuid.UUID(user["id"])))
           ).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "판정 기록을 찾을 수 없습니다.")
    return run


async def _launch(db: AsyncSession, run: EvidenceRun, runner, factory) -> None:
    if runner is None:  # 실행·pending 문장을 판정 불가(quota_unavailable)로
        await records.mark_failed(db, run.id, "quota_unavailable")
        await db.commit()
        log.error(json.dumps({"event": "runner_unavailable", "run_id": str(run.id)}))
        return
    background.start_run(run.id, runner=runner, session_factory=factory)


async def _started(db: AsyncSession, run: EvidenceRun, answer: str) -> dict:
    return {"chat_id": str(run.chat_id), "conversation_id": str(run.conversation_id), "run_id": str(run.id),
            "answer": answer, "claims": records.preview_claims(answer),
            "poll_interval_ms": records.POLL_INTERVAL_MS,
            "poll_until_s": records.poll_until_s(await records.runs_ahead(db, run))}


# ── 엔드포인트 ─────────────────────────────────────────────────────────────────

@router.post("/evidence/chat", summary="공시 근거 모드 채팅(답변 먼저, 판정은 폴링)")
async def evidence_chat(
    body: EvidenceChatBody,
    user=Depends(get_current_user_any),
    db: AsyncSession = Depends(get_pg_session),
    search: PassageSearch | None = Depends(get_passage_search),
    llm=Depends(get_llm_client),
    runner=Depends(get_runner),
    factory=Depends(get_session_factory),
):
    if search is None:
        raise HTTPException(503, "공시 문단 저장소가 아직 준비되지 않았습니다")
    blocked, message = check_guardrails(body.question)
    if blocked:
        raise HTTPException(400, message)
    user_id = user["id"]
    conversation_id = await get_or_create_conversation(db, user_id, body.conversation_id)

    try:
        passages = await search(body.corp_code, body.question)
    except Exception as exc:  # noqa: BLE001
        log.error(json.dumps({"event": "passage_search_failed", "error": type(exc).__name__}))
        raise HTTPException(503, "공시 문단 검색에 실패했습니다.")
    if not passages:
        raise HTTPException(422, "선택한 회사의 공시 문단이 없습니다.")

    model = settings.EVIDENCE_LLM_MODEL
    try:
        answer = await asyncio.wait_for(
            generate_answer(llm, model, body.company, body.question, [p["text"] for p in passages]),
            GENERATE_TIMEOUT_S)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        raise HTTPException(504, "답변 생성 시간이 초과되었습니다.")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(503, f"LLM 모델({model})을 찾을 수 없습니다. (ollama pull {model})")
        raise HTTPException(503, f"LLM 오류: {e.response.status_code}")
    except httpx.ConnectError:
        raise HTTPException(503, "LLM 서버에 연결할 수 없습니다.")

    # 답변·스레드 통계·판정 실행을 한 트랜잭션으로. 실패하면 판정을 시작하지 않는다(spec 10절)
    try:
        chat = Chat(id=uuid.uuid4(), user_id=uuid.UUID(user_id), client_id=user.get("client_id", ""),
                    conversation_id=uuid.UUID(conversation_id), question=body.question, answer=answer, steps=[],
                    citations=[{k: p.get(k) for k in CITATION_FIELDS} for p in passages])
        db.add(chat)
        conv = await db.get(Conversation, uuid.UUID(conversation_id))
        if conv:
            conv.message_count += 1
        run = records.new_run(chat_id=chat.id, conversation_id=chat.conversation_id, user_id=chat.user_id,
                              answer=answer, company=body.company, corp_code=body.corp_code, passages=passages,
                              generator_model=model)
        db.add(run)
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        log.error(json.dumps({"event": "evidence_chat_save_failed", "error": type(exc).__name__}))
        raise HTTPException(500, "답변을 저장하지 못해 근거 판정을 시작하지 않았습니다.")

    try:
        await set_active_conversation(user_id, conversation_id)
    except Exception:
        pass
    await _launch(db, run, runner, factory)
    return await _started(db, run, answer)


@router.get("/evidence/companies", summary="회사 선택 후보(문단이 적재된 회사)")
async def evidence_companies(
    q: str = Query("", max_length=100, description="회사명 또는 종목코드. 비우면 적재된 회사 전체"),
    user=Depends(get_current_user_any),
    companies: CompanyList | None = Depends(get_company_list),
    krx: KrxSearch = Depends(get_krx_search),
):
    if companies is None:
        raise HTTPException(503, "공시 문단 저장소가 아직 준비되지 않았습니다")
    try:
        loaded = await companies()  # 저장소가 회사명 순으로 준다
    except Exception as exc:  # noqa: BLE001
        log.error(json.dumps({"event": "company_list_failed", "error": type(exc).__name__}))
        raise HTTPException(503, "공시 문단 저장소를 조회하지 못했습니다.")
    q = q.strip()
    if not q:
        return {"companies": loaded}
    # 자동완성은 기존 KRX 검색 순서를 따르고 적재된 회사만 남긴다(spec 결정 3-1).
    # KRX 목록을 못 받으면 적재 목록에서 이름 부분일치·종목코드 앞부분으로 찾는다
    try:
        hits = await krx(q, KRX_SEARCH_LIMIT)
    except Exception as exc:  # noqa: BLE001
        log.warning(json.dumps({"event": "krx_search_failed", "error": type(exc).__name__}))
        hits = []
    by_stock = {c["stock_code"]: c for c in loaded}
    picked = [by_stock[h["symbol"].split(".")[0]] for h in hits if h.get("symbol", "").split(".")[0] in by_stock]
    if not picked:  # KRX 실패·결과 없음·적재 회사가 KRX 상위 결과 밖
        picked = [c for c in loaded if q in c["corp_name"] or c["stock_code"].startswith(q)]
    return {"companies": list({c["corp_code"]: c for c in picked}.values())}


@router.get("/evidence/notice", summary="외부 전송 고지 확인 여부")
async def get_notice(user=Depends(get_current_user_any)):
    # Redis를 못 읽으면 확인하지 않은 것으로 본다(고지를 한 번 더 보여 주는 쪽이 안전하다)
    try:
        state = await get_user_state(user["id"])
    except Exception as exc:  # noqa: BLE001
        log.warning(json.dumps({"event": "notice_state_read_failed", "error": type(exc).__name__}))
        state = {}
    return {"acknowledged": state.get("evidence_notice_ack") == NOTICE_VERSION, "version": NOTICE_VERSION}


@router.post("/evidence/notice", summary="외부 전송 고지 확인 기록")
async def ack_notice(user=Depends(get_current_user_any)):
    try:
        await update_user_state(user["id"], {"evidence_notice_ack": NOTICE_VERSION,
                                             "evidence_notice_ack_at": records.now().isoformat()})
    except Exception as exc:  # noqa: BLE001
        log.error(json.dumps({"event": "notice_state_write_failed", "error": type(exc).__name__}))
        raise HTTPException(503, "고지 확인을 저장하지 못했습니다.")
    return {"acknowledged": True, "version": NOTICE_VERSION}


@router.get("/evidence/runs/{run_id}", summary="판정 실행 조회(폴링)")
async def get_run(run_id: str, user=Depends(get_current_user_any), db: AsyncSession = Depends(get_pg_session)):
    run = await _owned_run(db, run_id, user)
    if await records.expire_stale(db, [run], records.now()):
        await db.commit()
    claims = (await records.load_claims(db, [run.id]))[run.id]
    ahead = await records.runs_ahead(db, run) if run.status in records.ACTIVE else 0
    latest = run.id in await records.latest_run_ids(db, [run.chat_id])
    return records.serialize_run(run, claims, records.poll_until_s(ahead), latest=latest)


@router.post("/evidence/runs/{run_id}/retry", status_code=201, summary="다시 판정(failed·partial)")
async def retry_run(
    run_id: str,
    user=Depends(get_current_user_any),
    db: AsyncSession = Depends(get_pg_session),
    runner=Depends(get_runner),
    factory=Depends(get_session_factory),
):
    run = await _owned_run(db, run_id, user)
    if await records.expire_stale(db, [run], records.now()):
        await db.commit()
    if run.status not in records.RETRYABLE or run.error_code in records.NO_RETRY_CODES:
        raise HTTPException(409, "이 판정은 다시 실행할 수 없습니다.")
    # 그 메시지의 최신 실행만 다시 판정한다. 진행 중인 새 실행이 있으면 그것이 최신이라 여기서 함께 걸린다
    if run.id not in await records.latest_run_ids(db, [run.chat_id]):
        raise HTTPException(409, "이 답변에는 더 최근 판정이 있습니다.")
    chat = await db.get(Chat, run.chat_id)
    try:
        new = records.new_run(chat_id=run.chat_id, conversation_id=run.conversation_id, user_id=run.user_id,
                              answer=chat.answer, company=run.company, corp_code=run.corp_code,
                              passages=[{**p, "rcept_no": run.rcept_no} for p in run.passages],
                              generator_model=run.generator_model, trigger="retry")
        db.add(new)
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        log.error(json.dumps({"event": "evidence_retry_save_failed", "error": type(exc).__name__}))
        raise HTTPException(500, "판정 실행을 저장하지 못했습니다.")
    await _launch(db, new, runner, factory)
    return await _started(db, new, chat.answer)


@router.get("/conversations/{cid}/evidence", summary="스레드의 판정 실행 타임라인")
async def conversation_evidence(
    cid: str,
    all_runs: bool = Query(False, alias="all", description="true면 메시지별 최신이 아니라 전체 실행"),
    user=Depends(get_current_user_any),
    db: AsyncSession = Depends(get_pg_session),
):
    conv = await _assert_owner(db, cid, user["id"])
    stmt = select(EvidenceRun).where(EvidenceRun.conversation_id == conv.id,
                                     EvidenceRun.user_id == uuid.UUID(user["id"]))
    if not all_runs:
        stmt = stmt.order_by(EvidenceRun.chat_id, EvidenceRun.created_at.desc()).distinct(EvidenceRun.chat_id)
    runs = sorted((await db.execute(stmt)).scalars(), key=lambda r: r.created_at)
    if await records.expire_stale(db, runs, records.now()):
        await db.commit()
    claims = await records.load_claims(db, [r.id for r in runs])
    latest = await records.latest_run_ids(db, list({r.chat_id for r in runs}))
    out = []
    for r in runs:
        ahead = await records.runs_ahead(db, r) if r.status in records.ACTIVE else 0
        out.append(records.serialize_run(r, claims[r.id], records.poll_until_s(ahead), latest=r.id in latest))
    return {"conversation_id": str(conv.id), "runs": out}
