# app/services/evidence/runner.py
"""답변 하나의 근거 판정 실행기(A-2 spec 5·7절). 결과를 데이터 객체로 돌려주고 저장은 하지 않는다(P2).

순서: 문장 분해(claim_spans) → 비주장 규칙 → 개인정보 패턴이면 skipped → 숫자 확인·어휘 겹침 →
(정책이 켠 경우) 주체 확인 → (정책이 켠 경우) 1차 필터 구간 확정 → 나머지 주장을 ServiceJevClient로 판정(동시성·마감)
→ sys_decision(주체 확인 정책이면 subject_decision).
경로(subject.route_claim)와 JEV 판정(subject.decide_claim)은 A-3 평가(lab/evidence/a3)도 같은 함수를 쓴다.
판정 질문·state·SYS 규칙은 A-1 judge 모듈을 그대로 쓴다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any

from app.lib import jev
from app.lib.jev_service import Quota, QuotaUnavailable
from app.services.evidence import judge
from app.services.evidence.claims import claim_spans, is_not_claim
from app.services.evidence.lexical import lex_features, tier_route
from app.services.evidence.numbers import number_check
from app.services.evidence.privacy import has_pii
from app.services.evidence.subject import decide_claim, route_claim, subject_valid

log = logging.getLogger("app.evidence.runner")

# 실패로 보지 않는 미판정 사유: 정책상 판정하지 않은 것(다시 판정해도 같다)
_POLICY_REASONS = frozenset({"claim_cap"})


@dataclass(frozen=True)
class Policy:
    """판정 정책 묶음. 임계값·1차 필터 구간·실행 제한. 비주장 규칙은 claims 모듈 상수다."""

    version: str
    tau_s: float
    tau_c: float
    theta_low: float | None = None
    theta_high: float | None = None
    max_claims: int = 8
    concurrency: int = 3
    deadline_s: float = 8.0
    subject_check: bool = False  # A-3 실험: 주체 확인을 ✅ 필요조건으로(subject 모듈)


# 평가(P5) 전 잠정 정책: τ_s 0.70, τ_c 0.35, 1차 필터 끔(spec 6.4절).
# 비주장 규칙도 정책에 포함되므로(spec 5.2절) 규칙을 바꾸면 버전을 올린다. -2: 목록 머리말 규칙 추가.
A2_PROVISIONAL = Policy("a2-provisional-2", tau_s=0.70, tau_c=0.35)
# 이전 버전으로 저장된 실행의 확신도 라벨용(τ는 같고 비주장 규칙만 다르다)
A2_PROVISIONAL_1 = Policy("a2-provisional", tau_s=0.70, tau_c=0.35)
# 평가(P5) 결과 정책: lab/evidence/results/a2-check.json의 policy_a2_v1 그대로(spec 6.4 반영 규칙, 다시 고르지 않는다).
# H-prec 실패(✅ 예측 150건 미만) → τ_s 0.85 고정, H-low 미시험 → θ_low 없음, H-high 통과 → θ_high 0.95.
# 정밀도 목표는 확인하지 못했다(precision_target_confirmed=false). 위 잠정 정책 상수는 이력 표시·재판정용으로 남긴다.
A2_V1 = Policy("a2-v1", tau_s=0.85, tau_c=0.35, theta_low=None, theta_high=0.95)
DEFAULT_POLICY = A2_V1  # 새 판정(auto·retry)과 재판정(rejudge)이 쓰는 정책
# A-3 실험 정책(꺼짐): a2-v1과 같은 τ·θ에 주체 확인만 더한다(A-3 spec 4절). 사전등록 평가(prereg_a3.json)를 통과하고
# 노아가 따로 결정하기 전에는 기본값으로 쓰지 않는다. 문장 행에 subject_ok 열이 없으므로 저장 실행을 이 정책으로
# 재판정할 수 없다(rejudge가 needs_call로 둔다).
A3_SUBJECT = Policy("a3-subject-exp", tau_s=0.85, tau_c=0.35, theta_low=None, theta_high=0.95, subject_check=True)


@dataclass
class ClaimResult:
    """실행 안의 문장 하나. status: supported/contradicted/no_evidence/not_claim/unjudged."""

    idx: int
    text: str
    start: int
    end: int
    status: str
    route: str | None = None  # lex_low / lex_high / jev / rule_not_claim
    reason: str | None = None  # unjudged 사유
    source_idx: int | None = None
    s: list[float] | None = None
    c: list[float] | None = None
    lex: float | None = None
    number_ok: list[bool] | None = None
    subject_ok: list[bool] | None = None  # 주체 확인 정책에서만 채운다(저장하지 않는다)
    jev_request_key: str | None = None
    attempts: int = 0
    latency_ms: float = 0.0
    cached: bool = False
    input_tokens: int = 0


@dataclass
class RunResult:
    """판정 실행 1건. status: done/partial/failed/limited/skipped."""

    status: str
    error_code: str | None
    policy_version: str
    trigger: str
    claims: list[ClaimResult]
    jev_model: str = jev.MODEL
    question_sha: str = judge.QUESTION_SHA
    calls: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    duration_ms: float = 0.0
    counts: dict = field(default_factory=dict)


def _decide(c: ClaimResult, policy: Policy) -> None:
    """저장된 s·c·숫자 확인(·주체 확인)으로 SYS 규칙을 적용한다."""
    c.status, c.source_idx, _ = decide_claim(policy, c.s, c.c, c.number_ok, c.subject_ok)
    c.reason = None


def _counts(claims: list[ClaimResult]) -> dict:
    out = {"total": len(claims), "not_claim": 0, "lex_low": 0, "lex_high": 0, "jev": 0, "unjudged": 0}
    for c in claims:
        if c.status == "not_claim":
            out["not_claim"] += 1
        elif c.status == "unjudged":
            out["unjudged"] += 1
        elif c.route in out:
            out[c.route] += 1
    return out


def _finish(claims: list[ClaimResult], fatal: str | None) -> tuple[str, str | None]:
    """문장 상태로 실행 상태를 정한다. fatal이 있으면 failed."""
    if fatal:
        return "failed", fatal
    judged = [c for c in claims if c.status != "not_claim" and c.reason not in _POLICY_REASONS]
    failed = [c for c in judged if c.status == "unjudged"]
    if not failed:
        return "done", None
    return ("failed" if len(failed) == len(judged) else "partial"), failed[0].reason


class Runner:
    """판정 실행기. 사용자별 진행 중 실행을 1건으로 묶는다(같은 사용자의 다음 실행은 앞 실행이 끝난 뒤 시작)."""

    def __init__(self, client: Any, quota: Quota):
        self._client = client
        self._quota = quota
        self._user_locks: dict[str, asyncio.Lock] = {}

    async def run(self, *, company: str, answer: str, passages: list[str], user_id: str,
                  policy: Policy = DEFAULT_POLICY, trigger: str = "auto", run_id: str | None = None) -> RunResult:
        t0 = time.monotonic()
        claims = [ClaimResult(i, sp.text, sp.start, sp.end, "unjudged") for i, sp in enumerate(claim_spans(answer))]
        for c in claims:
            if is_not_claim(c.text):
                c.status, c.route = "not_claim", "rule_not_claim"
        targets = [c for c in claims if c.status != "not_claim"]

        if has_pii(answer):
            for c in targets:
                c.reason = "pii"
            return self._result("skipped", "pii", claims, policy, trigger, t0, run_id)

        for c in targets[policy.max_claims:]:
            c.reason = "claim_cap"
        targets = targets[:policy.max_claims]
        for c in targets:
            c.number_ok = [number_check(c.text, p) for p in passages]
            f = lex_features(c.text, passages, company)
            c.lex = f.lex
            if policy.subject_check:
                c.subject_ok = subject_valid(c.text, passages, company)
            route = route_claim(policy, f.lex, f.high_ok, f.best, c.subject_ok)
            if route == "lex_low":
                c.status, c.route = "no_evidence", "lex_low"
            elif route == "lex_high":
                c.status, c.route, c.source_idx = "supported", "lex_high", f.best
            else:
                c.route, c.reason = "jev", "deadline"  # 끝나기 전에 마감되면 이 사유가 남는다
        jev_claims = [c for c in targets if c.route == "jev"]
        if not jev_claims:
            status, code = _finish(claims, None)
            return self._result(status, code, claims, policy, trigger, t0, run_id)

        # ponytail: 단일 프로세스 전제, 워커를 늘리면 Redis 락으로
        lock = self._user_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            try:
                cap = await self._quota.check(user_id, len(jev_claims))
            except QuotaUnavailable:
                for c in jev_claims:
                    c.reason = "quota_unavailable"
                return self._result("failed", "quota_unavailable", claims, policy, trigger, t0, run_id)
            if cap:
                for c in jev_claims:
                    c.reason = cap
                return self._result("limited", cap, claims, policy, trigger, t0, run_id)
            fatal = await self._judge_all(company, passages, jev_claims, user_id, policy, run_id)
        status, code = _finish(claims, fatal)
        return self._result(status, code, claims, policy, trigger, t0, run_id)

    async def _judge_all(self, company: str, passages: list[str], jev_claims: list[ClaimResult], user_id: str,
                         policy: Policy, run_id: str | None) -> str | None:
        """동시성·마감 안에서 주장마다 JEV를 부른다. 401·403이면 실행을 실패시킬 오류 코드를 돌려준다.

        마감으로 취소된 주장도 이미 나간 시도 수와 센 토큰(취소 시도는 추정치)을 집계에 남긴다.
        """
        sem = asyncio.Semaphore(policy.concurrency)
        stop: dict[str, str | None] = {"reason": None, "fatal": None}
        questions = judge.build_questions(len(passages))

        async def one(c: ClaimResult) -> None:
            async with sem:
                if stop["reason"]:
                    c.reason = stop["reason"]
                    return
                state = judge.build_state(company, c.text, passages)
                usage: dict = {}
                try:
                    r = await self._client.ask(state, questions, user_id=user_id,
                                               log_ctx={"run_id": run_id, "claim_idx": c.idx}, usage=usage)
                except asyncio.CancelledError:
                    if usage.get("calls"):
                        c.jev_request_key = jev.request_key(state, questions)
                        c.attempts, c.input_tokens = usage["calls"], usage["tokens"]
                    raise
                except Exception as exc:  # noqa: BLE001 — 조용히 사라지지 않게 문장에 남긴다
                    log.error(json.dumps({"event": "claim_error", "run_id": run_id, "claim_idx": c.idx,
                                          "error": type(exc).__name__}))
                    c.reason = "error"
                    return
                c.jev_request_key, c.attempts, c.latency_ms, c.cached = r.key, r.attempts, r.latency_ms, r.cached
                c.input_tokens = r.input_tokens
                if not r.ok:
                    c.reason = r.error_code
                    if r.http_status in (401, 403):
                        stop["reason"] = stop["fatal"] = r.error_code
                    elif r.http_status == 429:
                        stop["reason"] = "rate_limited"
                    return
                c.s = [r.answers[f"p{j}"]["supports"] for j in range(1, len(passages) + 1)]
                c.c = [r.answers[f"p{j}"]["contradicts"] for j in range(1, len(passages) + 1)]
                _decide(c, policy)

        tasks = [asyncio.create_task(one(c)) for c in jev_claims]
        _, pending = await asyncio.wait(tasks, timeout=policy.deadline_s)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for c in jev_claims:
            if c.status != "unjudged":
                c.reason = None
        return stop["fatal"]

    def _result(self, status: str, code: str | None, claims: list[ClaimResult], policy: Policy, trigger: str,
                t0: float, run_id: str | None) -> RunResult:
        used = [c for c in claims if c.route == "jev" and c.jev_request_key]
        res = RunResult(status, code, policy.version, trigger, claims,
                        calls=sum(c.attempts for c in used if not c.cached),
                        cache_hits=sum(1 for c in used if c.cached),
                        input_tokens=sum(c.input_tokens for c in used),
                        duration_ms=round((time.monotonic() - t0) * 1000, 1), counts=_counts(claims))
        log.info(json.dumps({"run_id": run_id, "policy_version": policy.version, "status": status,
                             "error_code": code, "claims": res.counts, "calls": res.calls,
                             "cache_hits": res.cache_hits, "input_tokens": res.input_tokens,
                             "duration_ms": res.duration_ms}))
        return res


def rejudge(claims: list[ClaimResult], policy: Policy) -> RunResult:
    """저장된 s·c로 새 정책의 SYS 규칙만 다시 적용한다. JEV를 부르지 않는다(spec 7.4절).

    확률이 없는 lex_low·lex_high 주장은 새 정책에서도 같은 구간이면 유지, 아니면 unjudged(needs_call).
    주체 확인 정책인데 주체 확인 결과(subject_ok)가 없는 주장도 unjudged(needs_call)다(문단 없이 다시 계산할 수 없다).
    미판정 주장은 그대로 둔다(다시 판정 버튼으로만 호출한다).
    """
    out = [replace(c, s=list(c.s) if c.s else c.s, c=list(c.c) if c.c else c.c) for c in claims]
    for c in out:
        if c.status == "not_claim" or c.reason in _POLICY_REASONS:
            continue
        if policy.subject_check and c.subject_ok is None and c.status != "unjudged":
            c.status, c.reason, c.source_idx = "unjudged", "needs_call", None
            continue
        if c.route == "jev" and c.s is not None and c.c is not None:
            _decide(c, policy)
        elif c.route in ("lex_low", "lex_high"):
            # lex_high였다면 상단 조건(숫자·회사명, 주체 확인 정책이면 주체 확인도)은 통과한 주장이다
            if tier_route(c.lex, c.route == "lex_high", policy.theta_low, policy.theta_high) != c.route:
                c.status, c.reason, c.source_idx = "unjudged", "needs_call", None
    status, code = _finish(out, None)
    return RunResult(status, code, policy.version, "rejudge", out, counts=_counts(out))
