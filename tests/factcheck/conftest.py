"""팩트체커 테스트 공용: 고정 자료(실제 공개 공시)를 T1 적재기 함수로 XBRL 계약 행으로 만든다.

- 정기 XBRL: `xbrl.facts_from_response`(OpenDART fnlttSinglAcntAll 응답 → 계약 행, report_type="periodic").
- 잠정실적: `parse.parse_prelim` + `parse.prelim_facts`(정정 공시 원문 → 계약 행, report_type="preliminary").
T2가 쓰는 계약을 테스트 전용 변환이 아니라 실제 적재 코드로 검사한다.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.factcheck import parse, xbrl

FIXTURES = Path(__file__).parent / "fixtures"
SAMSUNG = "00126380"
PRELIM_FILE = "prelim_samsung_2026Q2_correction.xml"
PRELIM_RCEPT = "20260730000001"  # 고정 자료 정정 공시의 접수번호(가정값, 행 출처 표시용)


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
