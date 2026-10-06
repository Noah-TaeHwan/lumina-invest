"""숫자 주장 ↔ XBRL 행 대조 테스트. 고정 자료는 실제 공개 공시(tests/factcheck/fixtures)를 계약 행으로 바꾼 것이다.

계정·기간(분기 단독/누적·시점)·연결/별도·정정>원본·확정>잠정, 단위 환산(evidence/numbers 재사용), 손실 부호, 영업이익률.
"""
from __future__ import annotations

import pytest

from app.services.factcheck import xbrl_check
from app.services.factcheck.scope import CompanyIndex, Period

SAMSUNG, HYNIX = "00126380", "00164779"
AS_OF = Period.parse("2026H1")
NAMES = CompanyIndex({SAMSUNG: ["삼성전자"], HYNIX: ["SK하이닉스"]})


def run(text, facts, corp=SAMSUNG):
    return xbrl_check.check(text, facts, corp_code=corp, as_of=AS_OF, names=NAMES)


@pytest.mark.parametrize("text, status", [
    # 2026년 2분기 단독 매출(연결) 171,499,470백만원
    ("2026년 2분기 매출은 171.5조원이다.", "match"),
    ("2026년 2분기 매출액은 171조 4,995억원이다.", "match"),
    ("2026년 2분기 매출은 171,499,470백만원이다.", "match"),
    ("2026년 2분기 매출은 172조원이다.", "mismatch"),
    # 단독 vs 누적: 305.4조는 상반기(누적) 값이지 2분기 단독 값이 아니다
    ("2026년 2분기 매출은 305.4조원이다.", "mismatch"),
    ("2026년 상반기 매출은 305.4조원이다.", "match"),
    ("2026년 2분기 누적 매출은 305.4조원이다.", "match"),
    ("2025년 3분기 매출은 86.1조원이다.", "match"),
    ("2025년 1~3분기 매출은 239.8조원이다.", "match"),
    ("2025년 3분기 매출은 239.8조원이다.", "mismatch"),
    # 연간·시점(재무상태표)
    ("2025년 매출은 333.6조원이다.", "match"),
    ("2025년 영업이익은 43.6조원이었다.", "match"),
    ("2025년 당기순이익은 45.2조원이다.", "match"),
    ("2025년 말 자산총계는 566.9조원이다.", "match"),
    ("2026년 상반기 말 총자산은 759.5조원이다.", "match"),
    ("2026년 6월 말 부채총계는 180.2조원이다.", "match"),
    ("2026년 상반기 말 자본총계는 600조원이다.", "mismatch"),
    # 상대 기간(as_of 2026H1)
    ("작년 매출은 333.6조원이다.", "match"),
    ("작년 매출은 300조원이다.", "mismatch"),
    ("재작년 매출은 300.9조원이다.", "match"),  # 2025 사업보고서의 전기 값
    # XBRL에 없는 기간: 4분기 단독은 정기보고서 XBRL에 없다
    ("2025년 4분기 매출은 93.8조원이다.", "unknown"),
    ("매출은 333.6조원이다.", "unknown"),  # 기간 불명
    # 계정 사전 밖·혼동 금지
    ("2025년 매출원가는 200조원이다.", "none"),
    ("2025년 매출총이익은 100조원이다.", "none"),
    ("2025년 유형자산은 200조원이다.", "none"),
    ("2025년 자본금은 9천억원이다.", "none"),
    ("2026년 2분기 매출은 전년 동기 대비 130% 증가했다.", "none"),  # 금액 없음
    # 제품·부문 매출은 회사 전체 계정이 아니다(오경보 방지)
    ("2026년 2분기 HBM 매출은 12조원이다.", "none"),
    ("2026년 2분기 DS부문 매출은 50조원이다.", "none"),
    ("2026년 2분기 메모리의 매출은 50조원이다.", "none"),
    ("2026년 2분기 DS부문매출은 50조원이다.", "none"),
    ("2026년 2분기 삼성전자의 매출은 171.5조원이다.", "match"),
    ("2026년 2분기 연결 매출은 171.5조원이다.", "match"),
    ("2026년 2분기 디스플레이 매출은 30조원이다.", "none"),  # '이'로 끝나는 명사는 조사가 아니다
    ("HBM 판매 호조로 2026년 2분기 매출은 171.5조원이다.", "match"),
])
def test_amount_claims(text, status, facts):
    assert run(text, facts).status == status


def test_consolidated_vs_separate(facts):
    # 2025년 매출: 연결 333.6조, 별도 238.0조
    r = run("2025년 연결 매출은 333.6조원이다.", facts)
    assert (r.status, r.items[0].fs_div) == ("match", "CFS")
    r = run("2025년 별도 매출은 238조원이다.", facts)
    assert (r.status, r.items[0].fs_div) == ("match", "OFS")
    r = run("2025년 연결 매출은 238조원이다.", facts)  # 연결이라고 적었으면 별도로 맞춰 주지 않는다
    assert r.status == "mismatch" and r.items[0].fs_div == "CFS"
    r = run("2025년 별도 매출은 333.6조원이다.", facts)
    assert r.status == "mismatch" and r.items[0].fs_div == "OFS"
    r = run("2025년 매출은 238조원이다.", facts)  # 표시 없음: 연결 우선, 별도로 일치하면 그 사실을 남긴다
    assert (r.status, r.items[0].fs_div, r.items[0].note) == ("match", "OFS", "separate_only")
    r = run("2025년 매출은 333.6조원이다.", facts)
    assert (r.items[0].fs_div, r.items[0].note) == ("CFS", None)


def test_mismatch_reports_disclosed_value(facts):
    r = run("2026년 2분기 매출은 172조원이다.", facts)
    it = r.items[0]
    assert (it.account_nm, it.period, it.fs_div, it.amount) == ("매출액", "2026Q2", "CFS", 171499470000000)
    assert r.primary() == {"account_nm": "매출액", "period": "2026Q2", "fs_div": "CFS", "amount": 171499470000000}


def test_multiple_amounts_and_partial(facts):
    assert run("2025년 매출 333.6조원, 영업이익 43.6조원을 기록했다.", facts).status == "match"
    r = run("2025년 매출 333.6조원, 영업이익 50조원을 기록했다.", facts)
    assert r.status == "mismatch" and r.primary()["account_nm"] == "영업이익"
    r = run("2025년 4분기 매출은 93.8조원, 2025년 영업이익은 43.6조원이다.", facts)
    assert r.status == "partial"
    assert run("2025년 매출 333.6조원, 2024년 매출 300.9조원이다.", facts).status == "match"


def test_loss_sign(facts):
    assert run("2025년 영업손실은 43.6조원이다.", facts).status == "mismatch"  # 실제는 영업이익(흑자)


def test_other_corp_rows_not_used(facts):
    assert run("2026년 2분기 매출은 79.3조원이다.", facts, corp=HYNIX).status == "match"  # SK하이닉스(CIS 행)
    assert run("2026년 2분기 매출은 79.3조원이다.", facts, corp=SAMSUNG).status == "mismatch"


def test_operating_margin(facts):
    # 2026년 2분기 연결 영업이익 89,492,412 / 매출 171,499,470 = 52.18%
    assert run("2026년 2분기 영업이익률은 52.2%다.", facts).status == "match"
    assert run("2026년 2분기 영업이익률은 52%다.", facts).status == "match"
    r = run("2026년 2분기 영업이익률은 45%다.", facts)
    assert r.status == "mismatch" and r.items[0].account_nm == "영업이익률"


def _fact(amount, rcept_no, rcept_dt, *, correction=False, report_type="preliminary"):
    return {"corp_code": SAMSUNG, "period": "2026Q2", "fs_div": "CFS", "account_id": "ifrs-full_Revenue",
            "account_nm": "매출액", "amount": amount, "rcept_no": rcept_no, "period_start": "2026-04-01",
            "period_end": "2026-06-30", "value_kind": "duration", "cumulative": False, "currency": "KRW",
            "unit": "원", "rcept_dt": rcept_dt, "is_correction": correction, "report_type": report_type}


def test_correction_beats_original_and_final_beats_preliminary():
    orig = _fact(171_000_000_000_000, "20260707000001", "20260707")
    corr = _fact(171_300_000_000_000, "20260730000001", "20260730", correction=True)
    final = _fact(171_499_470_000_000, "20260814003699", "20260814", report_type="half")
    q2 = Period(2026, "quarter", 2)
    pick = lambda rows: xbrl_check.select_fact(rows, SAMSUNG, "ifrs-full_Revenue", q2, "CFS")["rcept_no"]
    assert pick([orig, corr]) == "20260730000001"
    assert pick([corr, orig]) == "20260730000001"
    assert pick([orig, corr, final]) == "20260814003699"
    assert run("2026년 2분기 매출은 171.3조원이다.", [orig, corr]).status == "match"
    assert run("2026년 2분기 매출은 171.0조원이다.", [orig, corr]).status == "mismatch"
    assert run("2026년 2분기 매출은 171.3조원이다.", [orig, corr, final]).status == "mismatch"


def test_dates_compact_format_accepted():
    row = _fact(171_499_470_000_000, "x", "20260814", report_type="half")
    row.update(period_start="20260401", period_end="20260630", amount="171499470000000")
    assert run("2026년 2분기 매출은 171.5조원이다.", [row]).status == "match"
