# app/services/evidence/stats.py
"""관리자 판정 통계(A-2 spec 7.5절). 지표 수집기 없이 판정 기록 표에서 계산한다. 조회 기간은 최대 30일.

DB에 없는 값은 근사한다:
- JEV 지연은 문장 행의 latency_ms(마지막 시도)다. 첫 시도만의 지연은 따로 남기지 않는다.
- 첫 시도 실패율은 "재시도했거나 끝내 실패한 JEV 문장 / 실제 호출한 JEV 문장"이다.
- 일일 사용량은 판정 기록의 calls·input_tokens 합(KST 날짜)이다. 한도 판정의 진실 원천은 Redis 카운터다.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.lib.jev_service import KST
from app.models import EvidenceClaim, EvidenceRun
from app.services.evidence.records import now

MAX_DAYS = 30
_MS = 1000.0


def _pct(expr, q: float):
    return func.percentile_cont(q).within_group(expr)


def _round(v):
    return None if v is None else round(float(v), 1)


def _ms(col_end, col_start):
    return func.extract("epoch", col_end - col_start) * _MS


async def evidence_stats(db: AsyncSession, days: int) -> dict:
    since = now() - timedelta(days=days)
    in_window = EvidenceRun.created_at >= since
    kst_day = func.to_char(func.timezone("Asia/Seoul", EvidenceRun.created_at), "YYYY-MM-DD")

    runs = await db.scalar(select(func.count()).select_from(EvidenceRun).where(in_window)) or 0
    status = dict((await db.execute(select(EvidenceRun.status, func.count()).where(in_window)
                                    .group_by(EvidenceRun.status))).all())

    dur, delay = _ms(EvidenceRun.finished_at, EvidenceRun.started_at), _ms(EvidenceRun.started_at,
                                                                          EvidenceRun.created_at)
    timing = (await db.execute(select(_pct(dur, 0.5), _pct(dur, 0.95), _pct(delay, 0.5), _pct(delay, 0.95))
                               .where(in_window, EvidenceRun.finished_at.is_not(None),
                                      EvidenceRun.started_at.is_not(None)))).one()

    joined = select(EvidenceClaim).join(EvidenceRun, EvidenceClaim.run_id == EvidenceRun.id).where(in_window)
    claim_status = dict((await db.execute(joined.with_only_columns(EvidenceClaim.status, func.count())
                                          .group_by(EvidenceClaim.status))).all())
    routes = dict((await db.execute(joined.with_only_columns(EvidenceClaim.route, func.count())
                                    .where(EvidenceClaim.route.is_not(None)).group_by(EvidenceClaim.route))).all())
    reasons = dict((await db.execute(joined.with_only_columns(EvidenceClaim.reason, func.count())
                                     .where(EvidenceClaim.reason.is_not(None)).group_by(EvidenceClaim.reason))).all())

    called = and_(EvidenceClaim.route == "jev", EvidenceClaim.cached.is_(False), EvidenceClaim.attempts >= 1)
    jev = (await db.execute(joined.with_only_columns(
        _pct(EvidenceClaim.latency_ms, 0.5), _pct(EvidenceClaim.latency_ms, 0.95), _pct(EvidenceClaim.latency_ms, 0.99),
        func.count(),
        func.count(case((EvidenceClaim.attempts >= 2, 1), (EvidenceClaim.status == "unjudged", 1))),
        func.count(case((EvidenceClaim.attempts >= 2, 1))),
        func.count(case((and_(EvidenceClaim.attempts >= 2, EvidenceClaim.status != "unjudged"), 1))),
    ).where(called))).one()
    lat50, lat95, lat99, n_called, n_first_fail, n_retried, n_retry_ok = jev
    n_cached = (await db.scalar(joined.with_only_columns(func.count())
                                .where(EvidenceClaim.route == "jev", EvidenceClaim.cached.is_(True)))) or 0

    per_run = (select(EvidenceClaim.run_id, func.count().label("n"))
               .join(EvidenceRun, EvidenceClaim.run_id == EvidenceRun.id)
               .where(in_window, EvidenceClaim.status != "not_claim").group_by(EvidenceClaim.run_id).subquery())
    cpa50, cpa95 = (await db.execute(select(_pct(per_run.c.n, 0.5), _pct(per_run.c.n, 0.95)))).one()

    usage = (await db.execute(select(func.coalesce(func.sum(EvidenceRun.calls), 0),
                                     func.coalesce(func.sum(EvidenceRun.input_tokens), 0)).where(in_window))).one()
    daily = [{"day": d, "calls": int(c), "input_tokens": int(t)} for d, c, t in (await db.execute(
        select(kst_day, func.sum(EvidenceRun.calls), func.sum(EvidenceRun.input_tokens)).where(in_window)
        .group_by(kst_day).order_by(kst_day.desc()))).all()]
    top = [{"user_id": str(u), "calls": int(c), "input_tokens": int(t)} for u, c, t in (await db.execute(
        select(EvidenceRun.user_id, func.sum(EvidenceRun.calls).label("calls"), func.sum(EvidenceRun.input_tokens))
        .where(in_window).group_by(EvidenceRun.user_id).order_by(func.sum(EvidenceRun.calls).desc()).limit(5)
    )).all()]
    today = now().astimezone(KST).strftime("%Y-%m-%d")
    today_tokens = next((d["input_tokens"] for d in daily if d["day"] == today), 0)

    n_claims = sum(claim_status.values())
    rate = (lambda a, b: round(a / b, 4) if b else 0.0)
    first_fail_rate = rate(n_first_fail, n_called)
    timeouts, http_429 = reasons.get("timeout", 0), reasons.get("http_429", 0) + reasons.get("rate_limited", 0)
    run_p95, delay_p95 = _round(timing[1]), _round(timing[3])
    alerts = {
        "run_p95_over_1500ms": bool(run_p95 and run_p95 > 1500),
        "start_delay_p95_over_500ms": bool(delay_p95 and delay_p95 > 500),
        "jev_latency_p95_over_600ms": bool(lat95 and lat95 > 600),
        "jev_failure_rate_over_1pct": first_fail_rate > 0.01,
        "timeouts_over_1pct": rate(timeouts, n_called) > 0.01,
        "http_429_over_0": http_429 > 0,
        "failed_partial_over_5pct": rate(status.get("failed", 0) + status.get("partial", 0), runs) > 0.05,
        "unjudged_over_5pct": rate(claim_status.get("unjudged", 0), n_claims) > 0.05,
        "claims_p95_over_7": bool(cpa95 and cpa95 > 7),
        "global_tokens_over_80pct": today_tokens > 0.8 * settings.EVIDENCE_DAILY_GLOBAL_TOKENS,
    }
    return {
        "days": days, "since": since.isoformat(), "runs": runs,
        "status": status, "claim_status": claim_status, "routes": routes, "unjudged_reasons": reasons,
        "run_duration_ms": {"p50": _round(timing[0]), "p95": run_p95},
        "start_delay_ms": {"p50": _round(timing[2]), "p95": delay_p95},
        "jev_latency_ms": {"p50": _round(lat50), "p95": _round(lat95), "p99": _round(lat99)},
        "jev_calls_claims": n_called, "jev_first_attempt_failure_rate": first_fail_rate,
        "jev_retry_success_rate": rate(n_retry_ok, n_retried), "jev_timeouts": timeouts, "jev_http_429": http_429,
        "claims_per_answer": {"p50": _round(cpa50), "p95": _round(cpa95)},
        "calls": int(usage[0]), "input_tokens": int(usage[1]),
        "cache_hit_rate": rate(n_cached, n_cached + n_called),
        "daily": daily, "top_users": top, "today_global_tokens": today_tokens,
        "limits": {"user_calls": settings.EVIDENCE_DAILY_USER_CALLS, "user_tokens": settings.EVIDENCE_DAILY_USER_TOKENS,
                   "global_tokens": settings.EVIDENCE_DAILY_GLOBAL_TOKENS},
        "alerts": alerts,
    }
