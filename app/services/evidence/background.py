# app/services/evidence/background.py
"""판정 실행의 앱 프로세스 안 백그라운드 작업(A-2 spec 결정 4-1).

- 답변·실행 행을 커밋한 뒤 start_run으로 asyncio 작업을 띄운다. 작업 참조는 모듈 집합에 두고 끝나면 뺀다
  (가비지 컬렉션으로 사라지지 않게).
- 작업은 요청 세션이 아니라 자기 DB 세션을 연다. 어떤 예외든 실행을 failed(internal)로 남긴다(새 세션으로).
- 시작(pending → running)과 결과 저장(running → 종결)은 조건부 UPDATE다. 그 사이 조회가 stale로 바꿨으면
  결과를 버리고 run_superseded만 남긴다(stale 실행이 되살아나 다시 판정 실행과 섞이지 않게).
- 실행기는 프로세스에 하나다(사용자별 진행 중 실행 1건 락이 인스턴스에 있다). 앱을 여러 프로세스로 띄우면
  시작 시 stale 처리가 다른 프로세스의 실행을 죽이므로 그 전에 작업 큐로 옮긴다(결정 4-1 이전 조건 ①).
- 로그에는 답변·문단 원문을 남기지 않는다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid

from app.config import settings
from sqlalchemy import update

from app.models import Chat, EvidenceRun
from app.services.evidence import records
from app.services.evidence.runner import DEFAULT_POLICY, Policy, Runner

log = logging.getLogger("app.evidence.background")

_TASKS: set[asyncio.Task] = set()
_runner: Runner | None = None
_jev = None  # 실행기가 쓰는 ServiceJevClient(종료 시 httpx 클라이언트를 닫는다)


def get_runner() -> Runner:
    """프로세스 단일 실행기. Redis 연결(app.lib.redis_cache)과 EVIDENCE_DAILY_* 한도로 처음 쓸 때 만든다."""
    global _runner, _jev
    if _runner is None:
        from app.lib.jev_service import Quota, QuotaLimits, ServiceJevClient
        from app.lib.redis_cache import get_redis

        redis = get_redis()
        quota = Quota(redis, QuotaLimits(user_calls=settings.EVIDENCE_DAILY_USER_CALLS,
                                         user_tokens=settings.EVIDENCE_DAILY_USER_TOKENS,
                                         global_tokens=settings.EVIDENCE_DAILY_GLOBAL_TOKENS))
        _jev = ServiceJevClient(redis, quota)
        _runner = Runner(_jev, quota)
    return _runner


async def close_runner() -> None:
    """lifespan 종료 시 부른다. 실행기의 JEV httpx 클라이언트를 닫고 단일 객체를 비운다."""
    global _runner, _jev
    jev_client, _runner, _jev = _jev, None, None
    if jev_client is not None:
        await jev_client.aclose()


def start_run(run_id: uuid.UUID, *, runner, session_factory, policy: Policy = DEFAULT_POLICY) -> asyncio.Task:
    """커밋된 pending 실행을 백그라운드에서 판정한다. 작업을 돌려준다(테스트·종료 처리에서 기다릴 수 있게)."""
    task = asyncio.create_task(execute_run(run_id, runner=runner, session_factory=session_factory, policy=policy),
                               name=f"evidence-run-{run_id}")
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


def _superseded(run_id: uuid.UUID, stage: str) -> None:
    log.warning(json.dumps({"event": "run_superseded", "run_id": str(run_id), "stage": stage}))


async def execute_run(run_id: uuid.UUID, *, runner, session_factory, policy: Policy = DEFAULT_POLICY) -> None:
    try:
        async with session_factory() as db:
            run = await db.get(EvidenceRun, run_id)
            if run is None:  # 그 사이 스레드가 지워졌다(cascade)
                return
            chat = await db.get(Chat, run.chat_id)
            started = records.now()
            res = await db.execute(update(EvidenceRun).where(EvidenceRun.id == run_id, EvidenceRun.status == "pending")
                                   .values(status="running", started_at=started).returning(EvidenceRun.id))
            if res.first() is None:
                await db.rollback()
                return _superseded(run_id, "start")
            await db.commit()
            result = await runner.run(company=run.company, answer=chat.answer,
                                      passages=[p["text"] for p in run.passages], user_id=str(run.user_id),
                                      policy=policy, trigger=run.trigger, run_id=str(run.id))
            if not await records.save_result(db, run, result):
                await db.rollback()
                return _superseded(run_id, "finish")
            await db.commit()
            log.info(json.dumps({
                "event": "run_saved", "run_id": str(run.id), "chat_id": str(run.chat_id),
                "policy_version": result.policy_version, "status": result.status, "error_code": result.error_code,
                "start_delay_ms": round((started - run.created_at).total_seconds() * 1000, 1),
                "duration_ms": round((records.now() - started).total_seconds() * 1000, 1)}))
    except Exception as exc:  # noqa: BLE001 — 조용히 사라지지 않게 실행에 남긴다
        log.error(json.dumps({"event": "run_error", "run_id": str(run_id), "error": type(exc).__name__}))
        try:
            async with session_factory() as db:
                await records.mark_failed(db, run_id, "internal")
                await db.commit()
        except Exception as exc2:  # noqa: BLE001
            log.error(json.dumps({"event": "run_error_unsaved", "run_id": str(run_id),
                                  "error": type(exc2).__name__}))


async def fail_stale_runs_on_startup() -> int:
    """lifespan에서 부른다. 앞 프로세스가 남긴 pending·running 실행을 failed(stale)로 바꾼다."""
    from app.database.postgres import get_session_factory

    async with get_session_factory()() as db:
        n = await records.fail_active_runs(db)
        await db.commit()
    if n:
        log.warning(json.dumps({"event": "stale_runs_failed", "count": n}))
    return n
