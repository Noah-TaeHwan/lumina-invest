"""팩트체커 판정 파이프라인(설계 '처리 흐름'·D3~D9, Codex #2·#4·#7). 함수 계약(B↔C):
`async def check(corp_code, text, *, as_of=None, force_check=False) -> AsyncIterator[SentenceResult]`.

순서: 문장 분리(evidence.claims.claim_spans) → 1단계 분류(triage, JEV 분류는 기본 꺼짐) → 범위 판별(scope: 다른 회사·
기준 시점 뒤 기간·주가·파생 지표) → XBRL 대조(xbrl_check) → 문단 검색(저장소 search, 주장 기간 ~ 2년 뒤 보고서) →
JEV 판정(제품 경로 ServiceJevClient 비동기 어댑터, evidence.judge.build_state·sys_decision 재사용, 숫자 확인은
evidence.numbers.number_check).

- ✅는 JEV 판정이 문장 전체를 지지할 때만. XBRL 불일치 → ⚠️(JEV를 부르지 않는다). XBRL 일치 + 판정 미지지 → ❔ +
  reason 'xbrl_partial'(숫자는 XBRL과 일치, 나머지는 공시에서 못 찾음).
- 점진 산출: 건너뛴 문장과 XBRL 불일치 문장을 먼저 내고, 판정 문장은 끝나는 대로 낸다(idx로 원래 순서를 복원한다).
- 동시성(D9): 요청당 JEV 동시 4, 서버 전체 동시 8(인스턴스 세마포어). 호출마다 시간 초과 → unjudged('timeout'),
  실행 마감(30초)에 남은 문장 → unjudged('busy').
- 기준 시점 as_of 기본값: 저장소 latest_period(잠정실적 포함) → 오늘까지 끝난 분기. XBRL 행의 최신 기간으로 정하지 않는다.
  연도 없는 분기를 한 해 당겨 해석한 문장(scope shifted)은 ⚠️·✅를 내지 않고 최대 ❔('period_ambiguous')다.
- 숫자 확인(number_check)은 문장에서 기간 표현을 지운 뒤 한다('2분기'의 2가 문단 숫자로 요구되지 않게).
- force_check(사용자의 '직접 검수 요청'): 1단계 분류와 '검수 안 함'을 건너뛰고 모든 문장을 대조한다. 범위 밖 판별 결과는
  category·reason에 그대로 남긴다.
- 문단 검색(임베딩 포함)도 요청당 동시성 안에서 돈다.
- 저장소 오류는 StoreUnavailable로 올린다(전체 실패 메시지). 로그에는 초안 문장·문단 원문을 남기지 않는다(D7).
"""
from __future__ import annotations

import asyncio
import copy
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
from app.services.factcheck import corp_names, scope, triage, xbrl_check
from app.services.factcheck.scope import CompanyIndex, Period

K = 8
PER_REQUEST_CONCURRENCY = 4
GLOBAL_CONCURRENCY = 8
JEV_TIMEOUT_S = 10.0  # 호출 하나(ServiceJevClient 재시도 포함)의 상한
DEADLINE_S = 30.0  # 검수 실행 하나의 판정 마감
EVIDENCE_KEYS = ("rcept_no", "report_nm", "period", "section", "text", "superseded", "is_correction")
HISTORY_SECTIONS = ("CORR",)  # 정정 전/후 이력 문단(T1 store.search 기본 제외와 같다)

log = logging.getLogger("app.factcheck.pipeline")


def _today() -> date:
    """기준 시점 기본값의 '오늘'(테스트에서 바꾼다)."""
    return date.today()


def _join(*reasons: str | None) -> str | None:
    return ",".join(r for r in reasons if r) or None


def _without_periods(sentence: str, mentions: Sequence[scope.PeriodMention]) -> str:
    """기간 표현 구간을 공백으로 지운 문장(숫자 확인용)."""
    chars = list(sentence)
    for m in mentions:
        chars[m.start:m.end] = " " * (m.end - m.start)
    return "".join(chars)


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
    names: 상장사명 사전(corp_code → 이름들) 또는 CompanyIndex. user_id: 한도·로그용 호출자 키(익명 키 해시 등) —
    요청마다 for_user로 바꾼 사본을 쓰면 서버 전체 동시성(global_limit)이 공유된다.
    """

    def __init__(self, *, store: Any, jev: Any, names: Mapping[str, Iterable[str]] | CompanyIndex,
                 facts: Sequence[Mapping] = (), user_id: str = "factcheck", tau_s: float = DEFAULT_POLICY.tau_s,
                 tau_c: float = DEFAULT_POLICY.tau_c, k: int = K, per_request: int = PER_REQUEST_CONCURRENCY,
                 global_limit: int | asyncio.Semaphore = GLOBAL_CONCURRENCY, jev_timeout_s: float = JEV_TIMEOUT_S,
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
        self._global = global_limit if isinstance(global_limit, asyncio.Semaphore) else asyncio.Semaphore(global_limit)

    def for_user(self, user_id: str) -> "FactcheckPipeline":
        """호출자 키만 바꾼 얕은 사본. 서버 전체 동시성 세마포어·사전·XBRL 행을 공유한다(요청마다 쓴다)."""
        p = copy.copy(self)
        p.user_id = user_id
        return p

    async def _as_of(self, corp_code: str, as_of: str | None) -> Period:
        """기준 시점: 인자 → 저장소 latest_period(잠정실적 포함) → 오늘까지 끝난 분기."""
        if as_of:
            return Period.parse(as_of)
        latest = getattr(self.store, "latest_period", None)
        if callable(latest):
            v = latest(corp_code)
            v = await v if inspect.isawaitable(v) else v
            if v:
                return Period.parse(v)
        return Period.parse(_today().isoformat())

    async def _search(self, corp_code: str, sentence: str, sc: scope.Scope) -> list[dict]:
        try:
            r = self.store.search(corp_code, sentence, periods=sc.search_periods, report_types=sc.report_types,
                                  k=self.k)
            rows = list(await r if inspect.isawaitable(r) else r)
        except Exception as exc:  # noqa: BLE001 — 저장소 오류는 실행 전체 실패로 올린다
            raise StoreUnavailable(type(exc).__name__) from exc
        # T1 store.search는 기본으로 대체된 문서·정정 이력 문단을 뺀다. 다른 저장소가 섞어 줘도 판정에 쓰지 않는다
        return [p for p in rows if not p.get("superseded") and p.get("section") not in HISTORY_SECTIONS]

    async def _judge_one(self, idx: int, sentence: str, category: str, note: str | None, sc: scope.Scope,
                         xr: xbrl_check.XbrlResult, corp_code: str, req: asyncio.Semaphore) -> SentenceResult:
        """문장 하나: 문단 검색 → JEV 판정 → SYS 규칙. note는 force_check 때의 범위 밖 표시."""
        xbrl = xr.primary()
        partial = xr.status in ("match", "partial")
        ambiguous = any(m.shifted for m in sc.mentions)

        def done(status: str, evidence: list, reason: str | None) -> SentenceResult:
            # 기간을 당겨 해석한 문장: ⚠️를 내지 않고, XBRL이 어긋났으면 판정이 지지해도 ✅로 올리지 않는다
            if ambiguous and (status in ("contradicted", "supported") and xr.status == "mismatch"
                              or status == "contradicted"):
                status, evidence, reason = "no_evidence", [], "period_ambiguous"
            return SentenceResult(idx, sentence, category, status, evidence, xbrl, _join(reason, note))

        async with req:
            passages = await self._search(corp_code, sentence, sc)
            if not passages:
                return done("no_evidence", [], "xbrl_partial" if partial else "no_passages")
            texts = [p.get("text") or "" for p in passages]
            async with self._global:
                out = await self.judge.judge(self.names.display(corp_code), sentence, texts, user_id=self.user_id,
                                             log_ctx={"stage": "judge", "claim_idx": idx})
        if out.judgement is None:
            return done("unjudged", [], out.error_code)
        claim = _without_periods(sentence, sc.mentions)
        valid = [number_check(claim, t) for t in texts]
        status, src, _ = judge.sys_decision(out.judgement, valid, self.tau_s, self.tau_c)
        if status == "no_evidence":
            return done(status, [], "xbrl_partial" if partial else None)
        return done(status, [_evidence(passages[src])], None)

    async def check(self, corp_code: str, text: str, *, as_of: str | None = None,
                    force_check: bool = False) -> AsyncIterator[SentenceResult]:
        """문장마다 결과를 끝나는 대로 낸다(건너뛴 문장·XBRL 불일치 먼저). 저장소 오류면 StoreUnavailable.
        force_check=True면 1단계 분류·'검수 안 함'을 건너뛰고 모든 문장을 대조한다(범위 밖 표시는 남긴다)."""
        as_of_p = await self._as_of(corp_code, as_of)
        sentences = [sp.text for sp in claim_spans(text)]
        company = self.names.display(corp_code)
        if force_check:
            tri = [triage.Triage(True, "checked", "forced") for _ in sentences]
        else:
            tri = await triage.triage_all(sentences, corp_code=corp_code, company=company, names=self.names,
                                          as_of=as_of_p, client=self.jev, enabled=self.jev_triage,
                                          user_id=self.user_id)
        todo: list[tuple[int, str, str, str | None, scope.Scope, xbrl_check.XbrlResult]] = []
        for i, (s, t) in enumerate(zip(sentences, tri, strict=True)):
            if not t.check:
                yield SentenceResult(i, s, t.category, "skipped", [], None, t.reason)
                continue
            sc = scope.assess(s, corp_code, as_of=as_of_p, names=self.names)
            category, note = "checked", None
            if sc.category != "checked":
                if not force_check:
                    yield SentenceResult(i, s, sc.category, "skipped", [], None, sc.reason)
                    continue
                category, note = sc.category, sc.reason
                sc = scope.period_scope(s, as_of_p)
            xr = xbrl_check.check(s, self.facts, corp_code=corp_code, as_of=as_of_p, names=self.names)
            if xr.status == "mismatch" and not any(m.shifted for m in sc.mentions):
                yield SentenceResult(i, s, category, "contradicted", [], xr.primary(), _join("xbrl_mismatch", note))
                continue
            todo.append((i, s, category, note, sc, xr))
        if not todo:
            return

        req = asyncio.Semaphore(self.per_request)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.deadline_s
        tasks = {asyncio.create_task(self._judge_one(i, s, cat, note, sc, xr, corp_code, req)): (i, s, cat, note, xr)
                 for i, s, cat, note, sc, xr in todo}
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
                for i, s, cat, note, xr in sorted(tasks.values(), key=lambda v: v[0]):
                    yield SentenceResult(i, s, cat, "unjudged", [], xr.primary(), _join("busy", note))
                tasks.clear()
        finally:
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)


def build_pipeline(*, store: Any, jev: Any, corp_entries: Sequence[Mapping] | None = None,
                   corp_names_path: str | None = None, facts: Sequence[Mapping] | None = None,
                   facts_path: str | None = None, **kw) -> FactcheckPipeline:
    """T1 산출물로 파이프라인을 만든다: 상장사명 사전(corp_names JSON 항목 또는 경로 → by_corp_code),
    XBRL 계약 행(목록 또는 xbrl.save_facts 경로), 저장소(FactcheckStore). 나머지 인자는 FactcheckPipeline으로."""
    from app.services.factcheck import xbrl  # 실행 시점 import(경로를 줄 때만 쓴다)
    entries = list(corp_entries) if corp_entries is not None else corp_names.load(corp_names_path) \
        if corp_names_path else []
    rows = list(facts) if facts is not None else xbrl.load_facts(facts_path) if facts_path else []
    return FactcheckPipeline(store=store, jev=jev, names=CompanyIndex.from_entries(entries), facts=rows, **kw)


_default: FactcheckPipeline | None = None


def configure(p: FactcheckPipeline | None) -> None:
    """모듈 수준 check가 쓸 파이프라인을 정한다(앱 시작 시 한 번). None이면 해제."""
    global _default
    _default = p


async def check(corp_code: str, text: str, *, as_of: str | None = None,
                force_check: bool = False) -> AsyncIterator[SentenceResult]:
    """함수 계약 그대로의 진입점. configure로 정한 파이프라인에 넘긴다."""
    if _default is None:
        raise RuntimeError("factcheck pipeline is not configured")
    async for r in _default.check(corp_code, text, as_of=as_of, force_check=force_check):
        yield r
