# app/services/evidence/records.py
"""판정 기록 저장(A-2 spec 결정 4-4): 실행 행 생성, 실행기 결과 → 행, stale 처리, API 직렬화.

- 실행은 답변 저장과 같은 트랜잭션에서 pending으로 만들고, 문장 행도 미리 넣는다(pending / not_claim).
  조회 화면이 판정 전에도 서버가 나눈 오프셋으로 문장을 감쌀 수 있게 하려는 것이다.
- 판정이 끝나면 문장 행을 실행기 결과로 바꿔 쓴다.
- stale: 앱 시작 시 pending·running 전부, 조회 시 생성 후 60초가 지난 pending·running을 failed(stale)로 바꾼다.
- 실패 처리는 fail_runs 한 곳에서 한다: 실행을 failed(code)로, 그 실행의 pending 문장을 unjudged(reason=code)로.
- 판정 결과 저장은 실행이 아직 running일 때만 한다(조건부 UPDATE). stale로 바뀐 실행은 되살리지 않는다.
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
POLICIES = {p.version: p for p in (A2_PROVISIONAL,)}  # 확신도 라벨에 쓰는 정책별 τ_s(새 정책을 넣으면 여기에도)
CONFIDENCE_BAND = 0.15  # spec 3.3: τ_s 이상 0.15 구간 안이면 "보통"


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


async def save_result(db: AsyncSession, run: EvidenceRun, result: RunResult) -> bool:
    """실행이 아직 running이면 결과로 확정하고 문장 행을 바꿔 쓴다(커밋은 호출자).

    그 사이 stale 등으로 failed가 됐으면 아무것도 쓰지 않고 False를 돌려준다(다시 판정 실행과 섞이지 않게).
    """
    at = now()
    res = await db.execute(
        update(EvidenceRun).where(EvidenceRun.id == run.id, EvidenceRun.status == "running")
        .values(status=result.status, error_code=result.error_code, policy_version=result.policy_version,
                jev_model=result.jev_model, calls=result.calls, cache_hits=result.cache_hits,
                input_tokens=result.input_tokens, finished_at=at)
        .returning(EvidenceRun.id))
    if res.first() is None:
        return False
    await db.execute(delete(EvidenceClaim).where(EvidenceClaim.run_id == run.id))
    for c in result.claims:
        db.add(EvidenceClaim(
            run_id=run.id, idx=c.idx, text=c.text, start=c.start, end=c.end, status=c.status, route=c.route,
            reason=c.reason, source_idx=c.source_idx, s=c.s, c=c.c, lex=c.lex, number_ok=c.number_ok,
            jev_request_key=c.jev_request_key, attempts=c.attempts, latency_ms=c.latency_ms, cached=c.cached,
            input_tokens=c.input_tokens,
        ))
    return True


async def fail_runs(db: AsyncSession, where: list, code: str, at: datetime | None = None) -> list[uuid.UUID]:
    """조건에 맞는 진행 중(pending·running) 실행을 failed(code)로 바꾸고, 그 실행의 pending 문장을
    unjudged(reason=code)로 바꾼다. 바꾼 실행 id 목록(커밋은 호출자). 실패 처리는 모두 이 함수를 거친다."""
    at = at or now()
    res = await db.execute(update(EvidenceRun).where(EvidenceRun.status.in_(ACTIVE), *where)
                           .values(status="failed", error_code=code, finished_at=at).returning(EvidenceRun.id))
    ids = [r[0] for r in res]
    if ids:
        await db.execute(update(EvidenceClaim)
                         .where(EvidenceClaim.run_id.in_(ids), EvidenceClaim.status == "pending")
                         .values(status="unjudged", reason=code))
    return ids


async def mark_failed(db: AsyncSession, run_id: uuid.UUID, code: str) -> bool:
    return bool(await fail_runs(db, [EvidenceRun.id == run_id], code))


async def fail_active_runs(db: AsyncSession) -> int:
    """앱 시작 시: 앞 프로세스가 남긴 pending·running 실행을 모두 failed(stale)로. 자동 재실행은 하지 않는다."""
    return len(await fail_runs(db, [], "stale"))


async def expire_stale(db: AsyncSession, runs: list[EvidenceRun], at: datetime) -> int:
    """조회 시: 생성 후 60초가 지난 pending·running을 failed(stale)로 바꾼다(커밋은 호출자). 바꾼 수."""
    old = [r for r in runs if r.status in ACTIVE and r.created_at <= at - timedelta(seconds=STALE_AFTER_S)]
    if not old:
        return 0
    ids = set(await fail_runs(db, [EvidenceRun.id.in_([r.id for r in old])], "stale", at))
    for r in old:  # 이미 불러온 객체도 맞춰 둔다
        if r.id in ids:
            r.status, r.error_code, r.finished_at = "failed", "stale", at
    return len(ids)


async def latest_run_ids(db: AsyncSession, chat_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """메시지마다 가장 최근 실행의 id. 다시 판정은 최신 실행에만 허용한다."""
    if not chat_ids:
        return set()
    rows = await db.execute(select(EvidenceRun.id).where(EvidenceRun.chat_id.in_(chat_ids))
                            .order_by(EvidenceRun.chat_id, EvidenceRun.created_at.desc())
                            .distinct(EvidenceRun.chat_id))
    return {r[0] for r in rows}


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


def confidence_label(c: EvidenceClaim, policy_version: str) -> str | None:
    """✅ 문장의 확신도 "높음"/"보통"(spec 3.3). 화면은 확률 숫자 대신 이 라벨만 보인다.
    ✅가 아니거나, 근거 문단 확률이 없거나(lex 경로), 모르는 정책이면 None."""
    policy = POLICIES.get(policy_version)
    if c.status != "supported" or policy is None or not c.s or c.source_idx is None \
            or not 0 <= c.source_idx < len(c.s):
        return None
    return "보통" if c.s[c.source_idx] < policy.tau_s + CONFIDENCE_BAND else "높음"


def serialize_claim(c: EvidenceClaim, policy_version: str | None = None) -> dict:
    return {"idx": c.idx, "text": c.text, "start": c.start, "end": c.end, "status": c.status, "route": c.route,
            "reason": c.reason, "source_idx": c.source_idx, "s": c.s, "c": c.c, "lex": c.lex,
            "number_ok": c.number_ok, "cached": c.cached, "attempts": c.attempts, "latency_ms": c.latency_ms,
            "confidence": confidence_label(c, policy_version)}


def serialize_run(run: EvidenceRun, claims: list[EvidenceClaim], poll_until: int | None = None,
                  latest: bool = True) -> dict:
    """판정 실행 조회 응답. 진행 중이면 폴링 간격과 서버가 권하는 폴링 상한을 함께 준다.
    latest: 이 실행이 그 메시지의 최신 실행인가(다시 판정은 최신 실행에만)."""
    active = run.status in ACTIVE
    return {
        "id": str(run.id), "chat_id": str(run.chat_id), "conversation_id": str(run.conversation_id),
        "status": run.status, "trigger": run.trigger, "error_code": run.error_code,
        "retryable": latest and run.status in RETRYABLE and run.error_code not in NO_RETRY_CODES,
        "company": run.company, "corp_code": run.corp_code, "rcept_no": run.rcept_no, "passages": run.passages,
        "policy_version": run.policy_version, "jev_model": run.jev_model, "generator_model": run.generator_model,
        "calls": run.calls, "cache_hits": run.cache_hits, "input_tokens": run.input_tokens,
        "created_at": _iso(run.created_at), "started_at": _iso(run.started_at), "finished_at": _iso(run.finished_at),
        "counts": counts(c.status for c in claims),
        "claims": [serialize_claim(c, run.policy_version) for c in claims],
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
    if await expire_stale(db, runs, now()):
        await db.commit()
    per_run: dict[uuid.UUID, dict] = {r.id: counts(()) for r in runs}
    agg = await db.execute(select(EvidenceClaim.run_id, EvidenceClaim.status, func.count())
                           .where(EvidenceClaim.run_id.in_(list(per_run))).group_by(EvidenceClaim.run_id,
                                                                                    EvidenceClaim.status))
    for run_id, status, n in agg:
        per_run[run_id][status] = n
    return {r.chat_id: summarize(r, per_run[r.id]) for r in runs}
