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
  연도 없는 분기를 한 해 당겨 해석한 문장(scope shifted)과 명시 기간 없이 '같은 분기·동기·당분기' 같은 상대 기간만
  있는 문장은 ⚠️·✅를 내지 않고 최대 ❔('period_ambiguous')다.
- 문장의 금액이 XBRL과 모두 맞으면(separate_only 제외) 고른 XBRL 행 값으로 만든 근거 한 줄을 판정 문단 맨 앞에 둔다
  (원문 문단 대신 — 원문은 열·연결/별도를 코드로 가를 수 없다). 저장소 추가 호출 없음. ✅는 여전히 JEV 지지일 때만.
- 해석된 기간 없이 상대 기간만 있는 문장은 앞쪽 문장 중 기간이 해석된 가장 가까운 문장의 기간이 정확히 하나면 그것을
  이어받아 판정하고 reason에 'period_inherited:YYYYQn'을 남긴다. 이어받을 수 없거나 연도를 당겨 푼 분기 문장,
  별도로만 맞은 문장(separate_only)은 검색·JEV 없이 ❔.
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
from decimal import Decimal
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


def _ambiguous(sc: scope.Scope) -> bool:
    """기간 해석을 확신할 수 없는 문장: 연도 없는 분기를 당겨 풀었거나(shifted), 해석된 기간 없이 '같은 분기' 같은
    상대 기간만 있다. 이런 문장은 ✅·⚠️를 내지 않는다(최대 ❔ period_ambiguous)."""
    return sc.ambiguous_period or any(m.shifted for m in sc.mentions)


def _inherited_period(sentence: str, own: Sequence[Sequence[scope.PeriodMention]], i: int) -> Period | None:
    """앞 문장 기간 상속: 문장 i에 해석된 기간이 없고 상대 기간 표현('같은 분기' 등)만 있으면, 앞쪽 문장 중 기간이
    해석된 가장 가까운 문장을 본다. 그 문장의 기간이 정확히 하나(당긴 해석 아님)면 그 기간, 아니면 None(❔ 그대로)."""
    if not scope.relative_only(sentence, own[i]):
        return None
    for prev in reversed(own[:i]):
        if not prev:
            continue
        periods = {m.period for m in prev}
        if len(periods) == 1 and not any(m.shifted for m in prev):
            return next(iter(periods))
        return None
    return None


def _period_text(label: str | None, cumulative: bool | None, instant: bool) -> str:
    """XBRL 근거 줄의 기간 표기: '2025년 2분기(3개월)'·'2025년 1~3분기(누적)'·'2025년 상반기(누적)'·'2025년 연간'·
    재무상태표면 '2025년 말'·'2026년 6월 말'·'2025년 3분기 말'."""
    try:
        p = Period.parse(label or "")
    except ValueError:
        return label or "기간 불명"
    y = f"{p.year}년"
    if p.kind == "year":
        return f"{y} 말" if instant else f"{y} 연간"
    if p.kind == "half":
        if instant:
            return f"{y} {6 * p.n}월 말"
        return f"{y} {'상' if p.n == 1 else '하'}반기" + ("(누적)" if p.n == 1 else "(6개월)")
    if instant:
        return f"{y} {p.n}분기 말"
    return f"{y} 1~{p.n}분기(누적)" if cumulative and p.n > 1 else f"{y} {p.n}분기(3개월)"


def _amount_text(amount, unit: str) -> str:
    """부호 있는 금액(백만원, 천 단위 쉼표) 또는 비율(소수 둘째 자리 %)."""
    if unit == "%":
        return f"{float(amount):.2f}%"
    d = (Decimal(int(amount)) / Decimal(10 ** 6)).normalize()
    return f"{d:,f}백만원" if d != d.to_integral_value() else f"{int(d):,}백만원"


XBRL_HEAD = "[재무제표(XBRL) 값]"  # 정기보고서(확정) 행
PRELIM_HEAD = "[잠정실적 공시 값]"  # 잠정실적 행 — 확정 재무제표 값처럼 보이지 않게
PRELIM_CORR_HEAD = "[잠정실적 정정 공시 값]"


def _line_head(it: xbrl_check.XbrlItem) -> str:
    """근거 줄 머리말: 잠정실적 행이면 잠정(정정) 공시 값, 정기보고서 행만 재무제표(XBRL) 값."""
    if it.report_type == "preliminary":
        return PRELIM_CORR_HEAD if it.is_correction else PRELIM_HEAD
    return XBRL_HEAD
XBRL_SECTION = "XBRL"


def xbrl_passages(xr: xbrl_check.XbrlResult, company: str) -> list[dict]:
    """XBRL과 모두 맞은 문장(xr.status == 'match')의 근거 문단(항목마다 한 줄, 여럿이면 ' / '로 이어 한 문단).
    separate_only가 섞이면 없다.
    머리말은 행의 보고서 종류를 따른다(정기 '[재무제표(XBRL) 값]', 잠정 '[잠정실적 (정정) 공시 값]').

    원문 문단은 열(당기·전기·누계)과 기준(연결·별도)을 코드로 가를 수 없어 숫자만 맞는 엉뚱한 문단을 고를 수 있다.
    고른 XBRL 행 값(회사·연결/별도·계정·기간·단독/누적·부호 있는 금액·접수번호)을 한 줄로 만들어 판정 문단 맨 앞에
    둔다. ✅는 여전히 JEV가 문장 전체를 지지할 때만이다."""
    if xr.status != "match" or any("separate_only" in (it.note or "") for it in xr.items):
        return []
    out = []
    for it in xr.items:
        fs = {"CFS": "연결", "OFS": "별도"}.get(it.fs_div or "", it.fs_div or "")
        instant = it.account_id in xbrl_check.INSTANT_ACCOUNTS
        text = (f"{_line_head(it)} {company} {fs} {it.account_nm} {_period_text(it.period, it.cumulative, instant)}: "
                f"{_amount_text(it.amount, it.unit)} — 접수번호 {it.rcept_no}")
        out.append({"passage_id": f"XBRL-{it.rcept_no}-{it.account_id}-{it.period}-{it.fs_div}",
                    "rcept_no": it.rcept_no, "report_nm": it.report_nm, "period": it.period,
                    "section": XBRL_SECTION, "text": text, "superseded": False,
                    "is_correction": bool(it.is_correction)})
    if len(out) > 1:  # 항목이 여럿이면 한 근거로 합친다(문장 숫자 전부가 한 문단에서 확인되게)
        first = out[0]
        out = [{**first, "passage_id": "+".join(p["passage_id"] for p in out),
                "text": " / ".join(p["text"] for p in out),
                "is_correction": any(p["is_correction"] for p in out)}]
    return out


def _without_periods(sentence: str, spans: Sequence[tuple[int, int]]) -> str:
    """기간 표현 구간(주장 기간과 비교 기준 '2024년 대비' 모두)을 공백으로 지운 문장(숫자 확인용)."""
    chars = list(sentence)
    for a, b in spans:
        chars[a:b] = " " * (b - a)
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
        """문장 하나: 문단 검색 → JEV 판정 → SYS 규칙. note는 force_check 때의 범위 밖 표시.

        기간을 확신할 수 없는 문장(상대 기간만 있거나 연도를 당겨 푼 분기, period_ambiguous)과 연결/별도 표시 없이
        별도로만 맞은 문장(separate_only)은 검색·JEV 없이 바로 ❔다(결과가 ❔로 정해져 있으니 익명 한도를 쓰지 않는다)."""
        xbrl = xr.primary()
        if _ambiguous(sc):  # 상대 기간만 있거나 연도를 당겨 푼 분기: 결과가 ❔로 정해져 있다
            return SentenceResult(idx, sentence, category, "no_evidence", [], xbrl, _join("period_ambiguous", note))
        if any("separate_only" in (it.note or "") for it in xr.items):
            return SentenceResult(idx, sentence, category, "no_evidence", [], xbrl, _join("separate_only", note))
        partial = xr.status in ("match", "partial")

        def done(status: str, evidence: list, reason: str | None) -> SentenceResult:
            return SentenceResult(idx, sentence, category, status, evidence, xbrl, _join(reason, note))

        async with req:
            passages = await self._search(corp_code, sentence, sc)
            lines = xbrl_passages(xr, self.names.display(corp_code))
            if lines:  # XBRL 근거 줄을 맨 앞에(전체 k 유지)
                passages = (lines + passages)[:self.k]
            if not passages:
                return done("no_evidence", [], "xbrl_partial" if partial else "no_passages")
            texts = [p.get("text") or "" for p in passages]
            async with self._global:
                out = await self.judge.judge(self.names.display(corp_code), sentence, texts, user_id=self.user_id,
                                             log_ctx={"stage": "judge", "claim_idx": idx})
        if out.judgement is None:
            return done("unjudged", [], out.error_code)
        claim = _without_periods(sentence, sc.period_spans)
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
        own = [scope.extract_periods(s, as_of_p) for s in sentences]
        for i, (s, t) in enumerate(zip(sentences, tri, strict=True)):
            if not t.check:
                yield SentenceResult(i, s, t.category, "skipped", [], None, t.reason)
                continue
            inherited = _inherited_period(s, own, i)
            inh = f"period_inherited:{inherited.label}" if inherited else None
            sc = scope.assess(s, corp_code, as_of=as_of_p, names=self.names, inherited=inherited)
            category, note = "checked", inh
            if sc.category != "checked":
                if not force_check:
                    yield SentenceResult(i, s, sc.category, "skipped", [], None, _join(sc.reason, inh))
                    continue
                category, note = sc.category, _join(sc.reason, inh)
                sc = scope.period_scope(s, as_of_p, inherited=inherited)
            if category == "other_company":  # 다른 회사 수치를 선택 회사 XBRL로 대조하지 않는다(force_check)
                xr = xbrl_check.XbrlResult("none", [])
            else:
                xr = xbrl_check.check(s, self.facts, corp_code=corp_code, as_of=as_of_p, names=self.names,
                                      mentions=sc.mentions)
            if xr.status == "mismatch" and not _ambiguous(sc):
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
