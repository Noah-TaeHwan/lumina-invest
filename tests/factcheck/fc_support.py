# tests/factcheck/fc_support.py
"""팩트체커 T3 테스트 공용 가짜 객체. conftest.py(DB 픽스처)와 나눠 T2의 conftest와 겹치는 부분을 줄인다.

- FakeServiceJev: ServiceJevClient.ask와 같은 시그니처·usage 집계. 유료 호출 횟수를 센다.
- FakePipeline: T2 FactcheckPipeline과 같은 공개 모양 — for_user(user_id) 사본, 그리고
  `check(self, corp_code, text, *, as_of=None, force_check=False)`(**kw 없음: 시그니처가 어긋나면 TypeError로 드러난다).
  검수할 문장마다 self.jev.ask를 한 번 부른다(실제 파이프라인처럼 계량 래퍼를 거친다).
"""
from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field

from app.lib import jev
from app.lib.jev_service import ServiceJevResult


class FakeServiceJev:
    """유료 JEV 대역. tokens=None이면 usage에 토큰을 안 적는다, exc가 있으면 그 예외, delay는 응답 지연(초).
    attempts·ok로 재시도 실패를 흉내 낸다. calls는 실제로 나간(돈이 드는) 호출 수다."""

    def __init__(self, *, tokens: int | None = 1000, exc: BaseException | None = None, delay: float = 0.0,
                 ok: bool = True, attempts: int = 1, cached: bool = False):
        self.tokens, self.exc, self.delay = tokens, exc, delay
        self.ok, self.attempts, self.cached = ok, attempts, cached
        self.calls = 0
        self.users: list[str] = []

    async def ask(self, state, questions, *, user_id, log_ctx=None, usage=None):
        usage = usage if usage is not None else {}
        usage.setdefault("calls", 0)
        usage.setdefault("tokens", 0)
        key = jev.request_key(state, questions)
        if self.cached:
            return ServiceJevResult(key, True, {q: {"supports": 0.9, "contradicts": 0.05, "says_nothing": 0.05}
                                                for q in questions}, 0.0, 0, 0, None, None, None, cached=True)
        self.calls += 1
        self.users.append(user_id)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        usage["calls"] += self.attempts
        if isinstance(self.tokens, int):
            usage["tokens"] += self.tokens
        elif self.tokens is not None:
            usage["tokens"] = self.tokens  # 비정상 보고(문자열·실수)를 그대로 흉내 낸다
        answers = {q: {"supports": 0.9, "contradicts": 0.05, "says_nothing": 0.05} for q in questions} if self.ok else None
        return ServiceJevResult(key, self.ok, answers, 1.0, self.tokens if isinstance(self.tokens, int) else 0,
                                self.attempts, None if self.ok else "HTTP 503", None if self.ok else "http_5xx",
                                200 if self.ok else 503)


@dataclass
class SentenceResult:
    """T2 계약의 SentenceResult와 같은 필드."""

    idx: int
    text: str
    category: str
    status: str
    evidence: list[dict] = field(default_factory=list)
    xbrl: dict | None = None
    reason: str | None = None


EVID = {"rcept_no": "20260814000123", "report_nm": "반기보고서 (2026.06)", "period": "2026H1",
        "section": "II. 사업의 내용", "text": "회사는 메모리 반도체를 생산한다."}


class FakePipeline:
    """T2 FactcheckPipeline 대역. 문장(claim_spans)마다 결과 하나, 비주장 문장은 건너뛴다(force_check면 검수).

    gate가 있으면 문장마다 그 이벤트를 기다린다. exc는 두 번째 문장에서 올린다. 호출 기록(calls·users)은 사본끼리 공유한다.
    """

    def __init__(self, jev_client, *, statuses=None, exc: Exception | None = None,
                 gate: asyncio.Event | None = None, block: asyncio.Event | None = None):
        self.jev = jev_client
        self.user_id = "factcheck"
        self.statuses = statuses or {}
        self.exc, self.gate, self.block = exc, gate, block
        self.calls: list[tuple] = []
        self.users: list[str] = []

    def for_user(self, user_id: str) -> "FakePipeline":
        """호출자 키만 바꾼 얕은 사본(T2와 같다)."""
        self.users.append(user_id)
        p = copy.copy(self)
        p.user_id = user_id
        return p

    async def check(self, corp_code: str, text: str, *, as_of: str | None = None, force_check: bool = False):
        from app.services.evidence.claims import claim_spans, is_not_claim

        self.calls.append((corp_code, text, as_of, force_check, self.user_id))
        for i, sp in enumerate(claim_spans(text)):
            if self.gate is not None:
                await self.gate.wait()
                self.gate.clear()
            if self.block is not None:
                await self.block.wait()
            if self.exc is not None and i == 1:
                raise self.exc
            if is_not_claim(sp.text) and not force_check:
                yield SentenceResult(i, sp.text, "opinion", "skipped", [], None, "의견·전망")
                continue
            r = await self.jev.ask(f"회사: {corp_code}\n주장: {sp.text}", {"p1": {"type": "choice"}},
                                   user_id=self.user_id, log_ctx={"stage": "judge", "claim_idx": i})
            if not r.ok:
                yield SentenceResult(i, sp.text, "checked", "unjudged", [], None, r.error_code)
                continue
            yield SentenceResult(i, sp.text, "checked", self.statuses.get(i, "supported"), [dict(EVID)], None, None)
