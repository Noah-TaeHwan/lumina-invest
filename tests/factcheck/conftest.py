"""팩트체커 테스트 공용.

- 고정 자료(실제 공개 공시)를 T1 적재기 함수로 XBRL 계약 행으로 만든다(T1·T2).
  - 정기 XBRL: `xbrl.facts_from_response`(OpenDART fnlttSinglAcntAll 응답 → 계약 행, report_type="periodic").
  - 잠정실적: `parse.parse_prelim` + `parse.prelim_facts`(정정 공시 원문 → 계약 행, report_type="preliminary").
  T2가 쓰는 계약을 테스트 전용 변환이 아니라 실제 적재 코드로 검사한다.
- DB 픽스처(T3): 빈 PostgreSQL에 마이그레이션 적용·팩트체커 표(한도·예약·솔트) 비우기. tests/evidence/conftest.py와 같은
  환경변수(EVIDENCE_TEST_DATABASE_URL)를 쓰고, 없으면 그 테스트만 건너뛴다. T3 가짜 객체는 fc_support.py에 있다.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from app.services.factcheck import parse, xbrl
from tests.evidence.conftest import PG_ENV, alembic_upgrade

FIXTURES = Path(__file__).parent / "fixtures"
SAMSUNG = "00126380"
PRELIM_FILE = "prelim_samsung_2026Q2_correction.xml"
PRELIM_RCEPT = "20260730000001"  # 고정 자료 정정 공시의 접수번호(가정값, 행 출처 표시용)
FC_TABLES = "factcheck_quota, factcheck_reservations, factcheck_salt"


def load_facts(*names: str) -> list[dict]:
    """fixtures/xbrl_<name>_<CFS|OFS>.json 묶음을 계약 행 목록으로. 이름이 없으면 전부."""
    rows: list[dict] = []
    for p in sorted(FIXTURES.glob("xbrl_*.json")):
        if names and not any(n in p.stem for n in names):
            continue
        rows += xbrl.facts_from_response(json.loads(p.read_text(encoding="utf-8")), fs_div=p.stem.rsplit("_", 1)[1])
    return rows


def prelim_rows() -> list[dict]:
    """삼성전자 2026년 2분기 잠정실적(정정 후 값) 계약 행."""
    rep = parse.parse_prelim(parse.decode((FIXTURES / PRELIM_FILE).read_bytes()))
    return parse.prelim_facts(SAMSUNG, PRELIM_RCEPT, rep, rcept_dt="20260730", is_correction=True)


@pytest.fixture(scope="session")
def facts() -> list[dict]:
    return load_facts()


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
    """팩트체커 표(한도·예약·솔트)를 비운 DB URL."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def wipe():
        engine = create_async_engine(fc_pg_migrated)
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {FC_TABLES}"))
        await engine.dispose()

    asyncio.run(wipe())
    return fc_pg_migrated
