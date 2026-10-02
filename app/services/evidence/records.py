# app/services/evidence/records.py
"""판정 기록 저장(A-2 spec 결정 4-4): 실행 행 생성, 실행기 결과 → 행, stale 처리, API 직렬화.

- 실행은 답변 저장과 같은 트랜잭션에서 pending으로 만들고, 문장 행도 미리 넣는다(pending / not_claim).
  조회 화면이 판정 전에도 서버가 나눈 오프셋으로 문장을 감쌀 수 있게 하려는 것이다.
- 판정이 끝나면 문장 행을 실행기 결과로 바꿔 쓴다.
- stale: 앱 시작 시 pending·running 전부, 조회 시 생성 후 60초가 지난 pending·running을 failed(stale)로 바꾼다.
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.lib import jev
from app.models import EvidenceClaim, EvidenceRun
from app.services.evidence.claims import claim_spans, is_not_claim
from app.services.evidence.runner import A2_PROVISIONAL, Policy, RunResult

STALE_AFTER_S = 60
ACTIVE = ("pending", "running")
RETRYABLE = ("failed", "partial")
NO_RETRY_CODES = frozenset({"http_4xx", "no_api_key"})  # 키·권한 오류는 다시 판정해도 같다(spec 7.3)
POLL_INTERVAL_MS = 500
POLL_MARGIN_S = 4  # 마감 뒤 취소 정리·저장 여유
CLAIM_STATUSES = ("supported", "contradicted", "no_evidence", "not_claim", "unjudged", "pending")
PASSAGE_FIELDS = ("passage_id", "section", "idx", "sha256", "text")


def now() -> datetime:
    return datetime.now(timezone.utc)


def preview_claims(answer: str) -> list[dict]:
    """판정 전 문장 목록. 비주장 규칙에 걸린 문장은 not_claim, 나머지는 pending."""
    return [{"idx": i, "text": sp.text, "start": sp.start, "end": sp.end,
             "status": "not_claim" if is_not_claim(sp.text) else "pending"}
            for i, sp in enumerate(claim_spans(answer))]


def new_run(*, chat_id: uuid.UUID, conversation_id: uuid.UUID, user_id: uuid.UUID, answer: str, company: str,
            corp_code: str, passages: list[dict], generator_model: str, trigger: str = "auto",
            policy: Policy = A2_PROVISIONAL) -> EvidenceRun:
    """pending 실행 행과 문장 행을 만든다(세션에 넣고 커밋하는 것은 호출자). passages는 검색 결과 또는 이전 실행의 스냅샷."""
    run = EvidenceRun(
        id=uuid.uuid4(), chat_id=chat_id, conversation_id=conversation_id, user_id=user_id, status="pending",
        trigger=trigger, company=company, corp_code=corp_code,
        rcept_no=next((p["rcept_no"] for p in passages if p.get("rcept_no")), ""),
        passages=[{k: p.get(k) for k in PASSAGE_FIELDS} for p in passages],
        policy_version=policy.version, jev_model=jev.MODEL, generator_model=generator_model,
        calls=0, cache_hits=0, input_tokens=0, created_at=now(),
    )
    run.claims = [EvidenceClaim(idx=c["idx"], text=c["text"], start=c["start"], end=c["end"], status=c["status"],
                                route="rule_not_claim" if c["status"] == "not_claim" else None,
                                attempts=0, latency_ms=0.0, cached=False, input_tokens=0)
                  for c in preview_claims(answer)]
    return run


async def save_result(db: AsyncSession, run: EvidenceRun, result: RunResult) -> None:
    """실행기 결과로 실행 행을 확정하고 문장 행을 바꿔 쓴다(커밋은 호출자)."""
    await db.execute(delete(EvidenceClaim).where(EvidenceClaim.run_id == run.id))
    for c in result.claims:
        db.add(EvidenceClaim(
            run_id=run.id, idx=c.idx, text=c.text, start=c.start, end=c.end, status=c.status, route=c.route,
            reason=c.reason, source_idx=c.source_idx, s=c.s, c=c.c, lex=c.lex, number_ok=c.number_ok,
            jev_request_key=c.jev_request_key, attempts=c.attempts, latency_ms=c.latency_ms, cached=c.cached,
            input_tokens=c.input_tokens,
        ))
    run.status, run.error_code = result.status, result.error_code
    run.policy_version, run.jev_model = result.policy_version, result.jev_model
    run.calls, run.cache_hits, run.input_tokens = result.calls, result.cache_hits, result.input_tokens
    run.finished_at = now()


async def mark_failed(db: AsyncSession, run_id: uuid.UUID, code: str) -> None:
    await db.execute(update(EvidenceRun).where(EvidenceRun.id == run_id)
                     .values(status="failed", error_code=code, finished_at=now()))


async def fail_active_runs(db: AsyncSession) -> int:
    """앱 시작 시: 앞 프로세스가 남긴 pending·running 실행을 모두 failed(stale)로. 자동 재실행은 하지 않는다."""
    res = await db.execute(update(EvidenceRun).where(EvidenceRun.status.in_(ACTIVE))
                           .values(status="failed", error_code="stale", finished_at=now()))
    return res.rowcount or 0


def expire_stale(runs: list[EvidenceRun], at: datetime) -> int:
    """조회 시: 생성 후 60초가 지난 pending·running을 failed(stale)로 바꾼다(커밋은 호출자). 바꾼 수."""
    n = 0
    for run in runs:
        if run.status in ACTIVE and run.created_at <= at - timedelta(seconds=STALE_AFTER_S):
            run.status, run.error_code, run.finished_at = "failed", "stale", at
            n += 1
    return n


def poll_until_s(ahead: int, policy: Policy = A2_PROVISIONAL) -> int:
    """서버가 권하는 폴링 상한(실행 생성 시각 기준 초). 같은 사용자의 실행은 한 번에 하나씩 돌므로
    앞에 진행 중인 실행 수만큼 마감이 더해진다(두 번째 실행 최악 약 16초). stale 기준을 넘지 않는다."""
    return min(STALE_AFTER_S, math.ceil((ahead + 1) * policy.deadline_s + POLL_MARGIN_S))


async def runs_ahead(db: AsyncSession, run: EvidenceRun) -> int:
    """같은 사용자의 진행 중 실행 중 이 실행보다 먼저 만들어진 것의 수."""
    return await db.scalar(
        select(func.count()).select_from(EvidenceRun).where(
            EvidenceRun.user_id == run.user_id, EvidenceRun.status.in_(ACTIVE),
            EvidenceRun.created_at < run.created_at,
            EvidenceRun.created_at > now() - timedelta(seconds=STALE_AFTER_S))
    ) or 0


async def load_claims(db: AsyncSession, run_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[EvidenceClaim]]:
    out: dict[uuid.UUID, list[EvidenceClaim]] = {i: [] for i in run_ids}
    if run_ids:
        rows = await db.execute(select(EvidenceClaim).where(EvidenceClaim.run_id.in_(run_ids))
                                .order_by(EvidenceClaim.run_id, EvidenceClaim.idx))
        for c in rows.scalars():
            out[c.run_id].append(c)
    return out


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def counts(statuses) -> dict:
    out = dict.fromkeys(CLAIM_STATUSES, 0)
    for s in statuses:
        out[s] = out.get(s, 0) + 1
    return out


def serialize_claim(c: EvidenceClaim) -> dict:
    return {"idx": c.idx, "text": c.text, "start": c.start, "end": c.end, "status": c.status, "route": c.route,
            "reason": c.reason, "source_idx": c.source_idx, "s": c.s, "c": c.c, "lex": c.lex,
            "number_ok": c.number_ok, "cached": c.cached, "attempts": c.attempts, "latency_ms": c.latency_ms}


def serialize_run(run: EvidenceRun, claims: list[EvidenceClaim], poll_until: int | None = None) -> dict:
    """판정 실행 조회 응답. 진행 중이면 폴링 간격과 서버가 권하는 폴링 상한을 함께 준다."""
    active = run.status in ACTIVE
    return {
        "id": str(run.id), "chat_id": str(run.chat_id), "conversation_id": str(run.conversation_id),
        "status": run.status, "trigger": run.trigger, "error_code": run.error_code,
        "retryable": run.status in RETRYABLE and run.error_code not in NO_RETRY_CODES,
        "company": run.company, "corp_code": run.corp_code, "rcept_no": run.rcept_no, "passages": run.passages,
        "policy_version": run.policy_version, "jev_model": run.jev_model, "generator_model": run.generator_model,
        "calls": run.calls, "cache_hits": run.cache_hits, "input_tokens": run.input_tokens,
        "created_at": _iso(run.created_at), "started_at": _iso(run.started_at), "finished_at": _iso(run.finished_at),
        "counts": counts(c.status for c in claims),
        "claims": [serialize_claim(c) for c in claims],
        "poll_interval_ms": POLL_INTERVAL_MS if active else None,
        "poll_until_s": poll_until if active else None,
    }


def summarize(run: EvidenceRun, claim_counts: dict) -> dict:
    """메시지 직렬화(_serialize_message)에 붙이는 최신 실행 요약."""
    return {"id": str(run.id), "status": run.status, "trigger": run.trigger, "error_code": run.error_code,
            "policy_version": run.policy_version, "created_at": _iso(run.created_at), "counts": claim_counts}


async def latest_runs_for_chats(db: AsyncSession, chat_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict]:
    """메시지마다 최신 실행 요약. 쿼리 2번(DISTINCT ON + 문장 상태 집계)."""
    if not chat_ids:
        return {}
    rows = await db.execute(
        select(EvidenceRun).where(EvidenceRun.chat_id.in_(chat_ids))
        .order_by(EvidenceRun.chat_id, EvidenceRun.created_at.desc()).distinct(EvidenceRun.chat_id))
    runs = list(rows.scalars())
    if not runs:
        return {}
    if expire_stale(runs, now()):
        await db.commit()
    per_run: dict[uuid.UUID, dict] = {r.id: counts(()) for r in runs}
    agg = await db.execute(select(EvidenceClaim.run_id, EvidenceClaim.status, func.count())
                           .where(EvidenceClaim.run_id.in_(list(per_run))).group_by(EvidenceClaim.run_id,
                                                                                    EvidenceClaim.status))
    for run_id, status, n in agg:
        per_run[run_id][status] = n
    return {r.chat_id: summarize(r, per_run[r.id]) for r in runs}
