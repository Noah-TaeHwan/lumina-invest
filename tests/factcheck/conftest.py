# tests/factcheck/conftest.py
"""팩트체커(T3) 테스트 공용: 빈 PostgreSQL에 마이그레이션 적용·factcheck_quota 비우기, 가짜 파이프라인.

DB 픽스처는 tests/evidence/conftest.py와 같은 환경변수(EVIDENCE_TEST_DATABASE_URL)를 쓰고, 없으면 그 테스트만 건너뛴다.
가짜 파이프라인은 설계 문서의 함수 계약 `check(corp_code, text, *, as_of) -> AsyncIterator[SentenceResult]`과 같은 모양이다
(T2 pipeline.check가 아직 없다).
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field

import pytest

from tests.evidence.conftest import PG_ENV, alembic_upgrade


@pytest.fixture(scope="session")
def fc_pg_migrated():
    """빈 PostgreSQL에 0001→head를 적용한 URL. 환경변수가 없으면 건너뛴다."""
    url = os.environ.get(PG_ENV)
    if not url:
        pytest.skip(f"{PG_ENV}가 없어 팩트체커 DB 테스트를 건너뛴다")
    alembic_upgrade(url)
    return url


@pytest.fixture
def fc_pg(fc_pg_migrated):
    """factcheck_quota를 비운 DB URL."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def wipe():
        engine = create_async_engine(fc_pg_migrated)
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE factcheck_quota"))
        await engine.dispose()

    asyncio.run(wipe())
    return fc_pg_migrated


@dataclass
class FakeResult:
    """계약의 SentenceResult와 같은 필드(+ 선택 input_tokens)."""

    idx: int
    text: str
    category: str = "checked"
    status: str = "supported"
    evidence: list = field(default_factory=list)
    xbrl: dict | None = None
    reason: str | None = None
    input_tokens: int | None = None


class FakeChecker:
    """pipeline.check 대역. 문장(claim_spans)마다 결과 하나를 내고, gate가 있으면 문장마다 그 이벤트를 기다린다."""

    def __init__(self, *, statuses=None, delay: float = 0.0, tokens: int | None = 1000, exc: Exception | None = None,
                 gate: asyncio.Event | None = None):
        self.statuses = statuses or {}
        self.delay, self.tokens, self.exc, self.gate = delay, tokens, exc, gate
        self.calls: list[tuple[str, str, dict]] = []

    async def __call__(self, corp_code: str, text: str, *, as_of: str | None = None, **kw):
        from app.services.evidence.claims import claim_spans, is_not_claim

        self.calls.append((corp_code, text, {"as_of": as_of, **kw}))
        for i, sp in enumerate(claim_spans(text)):
            if self.gate is not None:
                await self.gate.wait()
                self.gate.clear()
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.exc is not None and i == 1:
                raise self.exc
            skipped = is_not_claim(sp.text) and not kw.get("force")
            status = "skipped" if skipped else self.statuses.get(i, "supported")
            yield FakeResult(idx=i, text=sp.text, category="opinion" if skipped else "checked", status=status,
                             evidence=[] if skipped else [{"rcept_no": "20260814000123", "report_nm": "반기보고서 (2026.06)",
                                                           "period": "2026H1", "section": "II. 사업의 내용",
                                                           "text": "회사는 메모리 반도체를 생산한다."}],
                             reason="의견·전망" if skipped else None,
                             input_tokens=None if (skipped or self.tokens is None) else self.tokens)
