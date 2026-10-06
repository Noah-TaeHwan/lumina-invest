"""팩트체커 판정 파이프라인(설계 '처리 흐름'·D3~D9, Codex #2·#4·#7). 함수 계약(B↔C):
`async def check(corp_code, text, *, as_of=None) -> AsyncIterator[SentenceResult]`.

순서: 문장 분리(evidence.claims.claim_spans) → 1단계 분류(triage, JEV 분류는 기본 꺼짐) → 범위 판별(scope: 다른 회사·
기준 시점 뒤 기간·주가·파생 지표) → XBRL 대조(xbrl_check) → 문단 검색(저장소 search, 주장 기간 ~ 2년 뒤 보고서) →
JEV 판정(제품 경로 ServiceJevClient 비동기 어댑터, evidence.judge.build_state·sys_decision 재사용, 숫자 확인은
evidence.numbers.number_check).

- ✅는 JEV 판정이 문장 전체를 지지할 때만. XBRL 불일치 → ⚠️(JEV를 부르지 않는다). XBRL 일치 + 판정 미지지 → ❔ +
  reason 'xbrl_partial'(숫자는 XBRL과 일치, 나머지는 공시에서 못 찾음).
- 점진 산출: 건너뛴 문장과 XBRL 불일치 문장을 먼저 내고, 판정 문장은 끝나는 대로 낸다(idx로 원래 순서를 복원한다).
- 동시성(D9): 요청당 JEV 동시 4, 서버 전체 동시 8(인스턴스 세마포어). 호출마다 시간 초과 → unjudged('timeout'),
  실행 마감(30초)에 남은 문장 → unjudged('busy').
- 저장소 오류는 StoreUnavailable로 올린다(전체 실패 메시지). 로그에는 초안 문장·문단 원문을 남기지 않는다(D7).
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.services.evidence import judge
from app.services.evidence.claims import claim_spans
from app.services.evidence.numbers import number_check
from app.services.evidence.runner import DEFAULT_POLICY
from app.services.factcheck import scope, triage, xbrl_check
from app.services.factcheck.scope import CompanyIndex, Period

K = 8
PER_REQUEST_CONCURRENCY = 4
GLOBAL_CONCURRENCY = 8
JEV_TIMEOUT_S = 10.0  # 호출 하나(ServiceJevClient 재시도 포함)의 상한
DEADLINE_S = 30.0  # 검수 실행 하나의 판정 마감
EVIDENCE_KEYS = ("rcept_no", "report_nm", "period", "section", "text")

log = logging.getLogger("app.factcheck.pipeline")


class StoreUnavailable(RuntimeError):
    """문단 저장소(Qdrant 등)를 쓸 수 없다. 검수 실행 전체를 실패로 보인다."""


@dataclass
class SentenceResult:
    """문장 하나의 결과(함수 계약). category: checked / opinion / out_of_scope / other_company / derived,
    status: supported / contradicted / no_evidence / unjudged / skipped. evidence 항목은
    {rcept_no, report_nm, period, section, text}, xbrl은 {account_nm, period, fs_div, amount} 또는 None."""

    idx: int
    text: str
    category: str
    status: str
    evidence: list[dict] = field(default_factory=list)
    xbrl: dict | None = None
    reason: str | None = None


@dataclass
class JudgeOutcome:
    """JEV 판정 어댑터 결과. judgement가 None이면 error_code가 실패 사유다."""

    judgement: judge.Judgement | None
    error_code: str | None = None


class JevJudge:
    """제품 경로 비동기 JEV 어댑터(Codex #4). client는 ServiceJevClient(또는 같은 ask 모양).

    evidence.judge의 state·질문을 그대로 써서 근거 모드와 같은 요청이면 같은 캐시 키가 된다. 동기 judge_claim은 쓰지 않는다.
    """

    def __init__(self, client: Any, *, timeout_s: float = JEV_TIMEOUT_S):
        self.client = client
        self.timeout_s = timeout_s

    async def judge(self, company: str, claim: str, passages: Sequence[str], *, user_id: str,
                    log_ctx: dict | None = None) -> JudgeOutcome:
        """문단 n개 묶음으로 JEV 1회. 시간 초과는 'timeout', 실패 응답은 그 error_code, 그 밖 예외는 'error'."""
        state = judge.build_state(company, claim, list(passages))
        questions = judge.build_questions(len(passages))
        try:
            r = await asyncio.wait_for(self.client.ask(state, questions, user_id=user_id, log_ctx=log_ctx),
                                       self.timeout_s)
        except asyncio.TimeoutError:
            return JudgeOutcome(None, "timeout")
        except Exception as exc:  # noqa: BLE001 — 조용히 사라지지 않게 문장에 남긴다
            log.error(json.dumps({**(log_ctx or {}), "event": "jev_error", "error": type(exc).__name__}))
            return JudgeOutcome(None, "error")
        if not r.ok:
            return JudgeOutcome(None, r.error_code or "error")
        n = len(passages)
        s = [r.answers[f"p{j}"]["supports"] for j in range(1, n + 1)]
        c = [r.answers[f"p{j}"]["contradicts"] for j in range(1, n + 1)]
        return JudgeOutcome(judge.Judgement(s, c, True, 1))


def _evidence(p: Mapping) -> dict:
    return {k: p.get(k) for k in EVIDENCE_KEYS}


class FactcheckPipeline:
    """판정 파이프라인. 프로세스에 하나 두고(서버 전체 동시성 공유) 검수 실행마다 check를 부른다.

    store: search(corp_code, query, *, periods, report_types, k) → 문단 payload 목록(동기·비동기 모두 받는다),
           선택적으로 latest_period(corp_code) → 'YYYYQn' 등(기준 시점 기본값).
    jev: ServiceJevClient(ask(state, questions, *, user_id, log_ctx)). facts: XBRL 계약 행 목록.
    names: 상장사명 사전(corp_code → 이름들) 또는 CompanyIndex. user_id: 한도·로그용 호출자 키(익명 키 해시 등).
    """

    def __init__(self, *, store: Any, jev: Any, names: Mapping[str, Iterable[str]] | CompanyIndex,
                 facts: Sequence[Mapping] = (), user_id: str = "factcheck", tau_s: float = DEFAULT_POLICY.tau_s,
                 tau_c: float = DEFAULT_POLICY.tau_c, k: int = K, per_request: int = PER_REQUEST_CONCURRENCY,
                 global_limit: int = GLOBAL_CONCURRENCY, jev_timeout_s: float = JEV_TIMEOUT_S,
                 deadline_s: float = DEADLINE_S, jev_triage: bool = triage.JEV_TRIAGE_ENABLED):
        self.store = store
        self.jev = jev
        self.judge = JevJudge(jev, timeout_s=jev_timeout_s)
        self.names = names if isinstance(names, CompanyIndex) else CompanyIndex(names)
        self.facts = list(facts)
        self.user_id = user_id
        self.tau_s, self.tau_c, self.k = tau_s, tau_c, k
        self.per_request = per_request
        self.deadline_s = deadline_s
        self.jev_triage = jev_triage
        self._global = asyncio.Semaphore(global_limit)

    async def _as_of(self, corp_code: str, as_of: str | None) -> Period:
        """기준 시점: 인자 → 저장소 latest_period → 그 회사 XBRL 행의 가장 늦은 기간 → 오늘까지 끝난 분기."""
        if as_of:
            return Period.parse(as_of)
        latest = getattr(self.store, "latest_period", None)
        if callable(latest):
            v = latest(corp_code)
            v = await v if inspect.isawaitable(v) else v
            if v:
                return Period.parse(v)
        rows = [r for r in self.facts if r.get("corp_code") == corp_code and r.get("period")]
        if rows:
            best = max(rows, key=lambda r: (Period.parse(r["period"]).end, r["period"]))
            return Period.parse(best["period"])
        return Period.parse(date.today().isoformat())

    async def _search(self, corp_code: str, sentence: str, sc: scope.Scope) -> list[dict]:
        try:
            r = self.store.search(corp_code, sentence, periods=sc.search_periods, report_types=sc.report_types,
                                  k=self.k)
            return list(await r if inspect.isawaitable(r) else r)
        except Exception as exc:  # noqa: BLE001 — 저장소 오류는 실행 전체 실패로 올린다
            raise StoreUnavailable(type(exc).__name__) from exc

    async def _judge_one(self, idx: int, sentence: str, sc: scope.Scope, xr: xbrl_check.XbrlResult,
                         corp_code: str, req: asyncio.Semaphore) -> SentenceResult:
        xbrl = xr.primary()
        partial = xr.status in ("match", "partial")
        passages = await self._search(corp_code, sentence, sc)
        if not passages:
            return SentenceResult(idx, sentence, "checked", "no_evidence", [], xbrl,
                                  "xbrl_partial" if partial else "no_passages")
        texts = [p.get("text") or "" for p in passages]
        async with req, self._global:
            out = await self.judge.judge(self.names.display(corp_code), sentence, texts, user_id=self.user_id,
                                         log_ctx={"stage": "judge", "claim_idx": idx})
        if out.judgement is None:
            return SentenceResult(idx, sentence, "checked", "unjudged", [], xbrl, out.error_code)
        valid = [number_check(sentence, t) for t in texts]
        status, src, _ = judge.sys_decision(out.judgement, valid, self.tau_s, self.tau_c)
        if status == "no_evidence":
            return SentenceResult(idx, sentence, "checked", status, [], xbrl, "xbrl_partial" if partial else None)
        return SentenceResult(idx, sentence, "checked", status, [_evidence(passages[src])], xbrl, None)

    async def check(self, corp_code: str, text: str, *, as_of: str | None = None) -> AsyncIterator[SentenceResult]:
        """문장마다 결과를 끝나는 대로 낸다(건너뛴 문장·XBRL 불일치 먼저). 저장소 오류면 StoreUnavailable."""
        as_of_p = await self._as_of(corp_code, as_of)
        sentences = [sp.text for sp in claim_spans(text)]
        company = self.names.display(corp_code)
        tri = await triage.triage_all(sentences, corp_code=corp_code, company=company, names=self.names,
                                      as_of=as_of_p, client=self.jev, enabled=self.jev_triage, user_id=self.user_id)
        todo: list[tuple[int, str, scope.Scope, xbrl_check.XbrlResult]] = []
        for i, (s, t) in enumerate(zip(sentences, tri, strict=True)):
            if not t.check:
                yield SentenceResult(i, s, t.category, "skipped", [], None, t.reason)
                continue
            sc = scope.assess(s, corp_code, as_of=as_of_p, names=self.names)
            if sc.category != "checked":
                yield SentenceResult(i, s, sc.category, "skipped", [], None, sc.reason)
                continue
            xr = xbrl_check.check(s, self.facts, corp_code=corp_code, as_of=as_of_p, names=self.names)
            if xr.status == "mismatch":
                yield SentenceResult(i, s, "checked", "contradicted", [], xr.primary(), "xbrl_mismatch")
                continue
            todo.append((i, s, sc, xr))
        if not todo:
            return

        req = asyncio.Semaphore(self.per_request)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.deadline_s
        tasks = {asyncio.create_task(self._judge_one(i, s, sc, xr, corp_code, req)): (i, s, xr)
                 for i, s, sc, xr in todo}
        try:
            while tasks:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                done, _ = await asyncio.wait(tasks, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
                for t in sorted(done, key=lambda x: tasks[x][0]):
                    tasks.pop(t)
                    yield t.result()  # StoreUnavailable은 여기서 올라가고 finally가 나머지를 취소한다
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
                for i, s, xr in sorted(tasks.values()):
                    yield SentenceResult(i, s, "checked", "unjudged", [], xr.primary(), "busy")
                tasks.clear()
        finally:
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)


_default: FactcheckPipeline | None = None


def configure(p: FactcheckPipeline | None) -> None:
    """모듈 수준 check가 쓸 파이프라인을 정한다(앱 시작 시 한 번). None이면 해제."""
    global _default
    _default = p


async def check(corp_code: str, text: str, *, as_of: str | None = None) -> AsyncIterator[SentenceResult]:
    """함수 계약 그대로의 진입점. configure로 정한 파이프라인에 넘긴다."""
    if _default is None:
        raise RuntimeError("factcheck pipeline is not configured")
    async for r in _default.check(corp_code, text, as_of=as_of):
        yield r
