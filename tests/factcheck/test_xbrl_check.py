"""숫자 주장 ↔ XBRL 행 대조 테스트. 고정 자료는 실제 공개 공시(tests/factcheck/fixtures)를 계약 행으로 바꾼 것이다.

계정·기간(분기 단독/누적·시점)·연결/별도·정정>원본·확정>잠정, 단위 환산(evidence/numbers 재사용), 손실 부호, 영업이익률.
"""
from __future__ import annotations

from decimal import Decimal

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
    assert r.primary() == {"account_nm": "매출액", "period": "2026Q2", "fs_div": "CFS", "amount": 171499470000000,
                           "unit": "원", "rcept_no": "20260814003699", "cumulative": False, "is_correction": False,
                           "column": "thstrm", "note": None}


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


# ---- 리뷰 반영(A3·A5·B5·C2·Codex9) ----

@pytest.mark.parametrize("text, status", [
    ("2025년 매출은 300.9조원에서 333.6조원으로 늘었다.", "match"),   # 'X에서 Y로': 뒤 금액이 주장 기간 값
    ("2025년 매출은 333.6조원에서 300조원으로 줄었다.", "mismatch"),
    ("2025년과 2024년 매출은 각각 333.6조원, 300.9조원이다.", "match"),  # '각각': 기간 순서대로
    ("2025년과 2024년 매출은 각각 333.6조원, 250조원이다.", "mismatch"),
    ("2025년 3분기 매출은 86.1조원, 전년 동기 매출은 79.1조원이다.", "match"),  # 전년 동기 = 2024Q3
    ("2025년 3분기 매출은 86.1조원, 전년 동기 매출은 70조원이다.", "mismatch"),
    ("2025년 매출은 238조원으로 연결 대상 회사가 늘었다.", "match"),  # '연결'은 계정 절 밖(별도로만 일치)
])
def test_review_amount_structures(text, status, facts):
    assert run(text, facts).status == status


def test_fs_hint_is_per_clause(facts):
    r = run("연결 자회사가 늘었지만 2025년 별도 매출은 238조원이다.", facts)
    assert (r.status, r.items[0].fs_div, r.items[0].note) == ("match", "OFS", None)
    r = run("2025년 연결 매출은 333.6조원, 별도 매출은 238조원이다.", facts)
    assert [(i.status, i.fs_div, i.note) for i in r.items] == [("match", "CFS", None), ("match", "OFS", None)]


def _row(account_id, amount, *, period="2025", start="2025-01-01", end="2025-12-31", rcept_no="r1",
         rcept_dt="20260310", column="thstrm_amount", report_type="periodic", correction=False, fs="CFS"):
    return {"corp_code": SAMSUNG, "period": period, "fs_div": fs, "account_id": account_id, "account_nm": "x",
            "amount": amount, "rcept_no": rcept_no, "period_start": start, "period_end": end,
            "value_kind": "duration", "cumulative": True, "currency": "KRW", "unit": "원", "rcept_dt": rcept_dt,
            "is_correction": correction, "report_type": report_type, "column": column}


NET, OWN = "ifrs-full_ProfitLoss", "ifrs-full_ProfitLossAttributableToOwnersOfParent"
T = 10 ** 12


@pytest.mark.parametrize("rows, text, status", [
    ([_row(NET, 100 * T), _row(OWN, 98 * T)], "2025년 당기순이익은 98조원이다.", "match"),   # 지배주주 값
    ([_row(NET, 100 * T), _row(OWN, 98 * T)], "2025년 당기순이익은 100조원이다.", "match"),
    ([_row(NET, 100 * T), _row(OWN, 98 * T)], "2025년 당기순이익은 50조원이다.", "mismatch"),
    ([_row(NET, 100 * T)], "2025년 당기순이익은 98조원이다.", "unknown"),   # 지배주주 값이 없어 판단 불가
    ([_row(NET, 100 * T)], "2025년 당기순이익은 100조원이다.", "match"),
    ([_row(NET, 100 * T), _row(OWN, 98 * T)], "2025년 지배기업 소유주지분 순이익은 100조원이다.", "mismatch"),
    ([_row(NET, 100 * T), _row(OWN, 98 * T)], "2025년 지배주주 순이익은 98조원이다.", "match"),
])
def test_net_income_candidates(rows, text, status):
    assert run(text, rows).status == status


def test_restated_value_any_candidate_matches():
    # 2024년 매출: 2024 사업보고서 원 보고값 300.0조, 2025 사업보고서 비교값(재작성) 300.9조
    rows = [_row("ifrs-full_Revenue", 300_000 * 10 ** 9, period="2024", start="2024-01-01", end="2024-12-31",
                 rcept_no="r2024", rcept_dt="20250310"),
            _row("ifrs-full_Revenue", 300_900 * 10 ** 9, period="2024", start="2024-01-01", end="2024-12-31",
                 rcept_no="r2025", rcept_dt="20260310", column="frmtrm_amount")]
    # 원 보고값과 맞으면 'restated_exists'(재작성 값이 따로 있음), 재작성 비교값과 맞으면 'restated'
    for claim, note in (("300조원", "restated_exists"), ("300.9조원", "restated")):
        r = run(f"2024년 매출은 {claim}이다.", rows)
        assert (r.status, r.items[0].note) == ("match", note), claim
    r = run("2024년 매출은 250조원이다.", rows)
    assert r.status == "mismatch" and r.items[0].rcept_no == "r2024"  # 당기 칸(thstrm) 값이 대표


def test_correction_flag_does_not_beat_later_receipt():
    q2 = Period(2026, "quarter", 2)
    a = _row("ifrs-full_Revenue", 1, period="2026Q2", start="2026-04-01", end="2026-06-30", rcept_no="a",
             rcept_dt="20260730", report_type="preliminary", correction=True)
    b = _row("ifrs-full_Revenue", 2, period="2026Q2", start="2026-04-01", end="2026-06-30", rcept_no="b",
             rcept_dt="20260801", report_type="preliminary")
    assert xbrl_check.select_fact([a, b], SAMSUNG, "ifrs-full_Revenue", q2, "CFS")["rcept_no"] == "b"


def test_margin_sign_and_unit():
    q2 = dict(period="2026Q2", start="2026-04-01", end="2026-06-30")
    rows = [_row("dart_OperatingIncomeLoss", -20 * T, **q2), _row("ifrs-full_Revenue", 100 * T, **q2)]
    assert run("2026년 2분기 영업이익률은 20%다.", rows).status == "mismatch"   # 실제는 -20%
    assert run("2026년 2분기 영업이익률은 -20%다.", rows).status == "match"
    assert run("2026년 2분기 영업이익률은 20% 적자다.", rows).status == "match"
    r = run("2026년 2분기 영업이익률은 -20%다.", rows)
    assert r.primary()["unit"] == "%" and r.primary()["amount"] == -20.0


# ---- T1 연동: 정기·잠정 계약 행(report_type periodic/preliminary), 반올림 단위 ----

def _without_half(facts):
    return [r for r in facts if not (r["corp_code"] == SAMSUNG and r["bsns_year"] == "2026")]


def test_preliminary_rows_check_latest_quarter(facts):
    # 반기보고서 전(잠정실적만 있는 최신 분기): 정정 후 잠정값 171.50조로 대조
    from conftest import prelim_rows
    rows = _without_half(facts) + prelim_rows()
    r = run("2026년 2분기 매출은 171.5조원이다.", rows)
    assert r.status == "match" and r.items[0].rcept_no == "20260730000001"
    assert run("2026년 2분기 매출은 171.0조원이다.", rows).status == "mismatch"   # 정정 전 값
    assert run("2026년 상반기 영업이익은 146.73조원이다.", rows).status == "match"  # 누계실적
    # 반기보고서(확정)가 들어오면 확정 값이 대표
    r = run("2026년 2분기 매출은 171.5조원이다.", facts + prelim_rows())
    assert r.status == "match" and r.items[0].rcept_no == "20260814003699"


def test_preliminary_rounding_unit():
    # 잠정실적은 조원 둘째 자리(10^10원)로 반올림된 값 — 더 자세한 주장은 그 단위로 맞춘다
    from conftest import prelim_rows
    rows = prelim_rows()
    assert run("2026년 2분기 매출은 171.499조원이다.", rows).status == "match"
    assert run("2026년 2분기 매출은 171조 4,990억원이다.", rows).status == "match"
    assert run("2026년 2분기 매출은 171.44조원이다.", rows).status == "mismatch"


def test_superseded_rows_ignored(facts):
    from conftest import prelim_rows
    old = [dict(r, amount=r["amount"] - 10 ** 12, superseded=True, rcept_no="old") for r in prelim_rows()]
    rows = _without_half(facts) + prelim_rows() + old
    assert run("2026년 2분기 매출은 170.5조원이다.", rows).status == "mismatch"


# ---- PR #56 후속 3번: 여러 계정 '각각'·계정 뒤 기간·별도 힌트 전파·superseded만 ----

@pytest.mark.parametrize("text, status", [
    ("2025년 매출과 영업이익은 각각 333.6조원, 43.6조원이다.", "match"),
    ("2025년 매출과 영업이익은 각각 333.6조원, 50조원이다.", "mismatch"),
    ("2025년 매출과 영업이익은 333.6조원, 43.6조원이다.", "match"),
    ("2025년 매출과 영업이익은 각각 333.6조원이다.", "none"),          # 계정 2개·금액 1개: 짝을 모른다
    ("2024년 매출 300.9조원, 매출은 2025년 333.6조원이다.", "match"),  # 계정 뒤·금액 앞 기간
    ("매출은 2025년 333.6조원이다.", "match"),
    ("2025년 별도 매출은 238조원, 영업이익은 43.6조원이다.", "match"),  # '별도'는 다음 절로 번지지 않는다
    ("2025년 매출이 늘었고 영업이익은 43.6조원이다.", "match"),  # 이어지지 않은 계정은 묶지 않는다
    ("2025년 매출, 영업이익은 각각 333.6조원, 43.6조원이다.", "match"),
])
def test_followup_amount_structures(text, status, facts):
    assert run(text, facts).status == status


def test_only_superseded_rows_is_unknown():
    row = _row("ifrs-full_Revenue", 300 * T, period="2025")
    row["superseded"] = True
    assert run("2025년 매출은 100조원이다.", [row]).status == "unknown"


def test_preliminary_negative_mwon_loss():
    # SK하이닉스 2023년 3분기 잠정 영업손실 -1,791,961백만원(구양식 고정 자료)
    from app.services.factcheck import parse
    from conftest import FIXTURES
    rep = parse.parse_prelim(parse.decode((FIXTURES / "prelim_skhynix_2023Q3_mwon_loss.xml").read_bytes()))
    rows = parse.prelim_facts(HYNIX, "20231026000001", rep)
    r = xbrl_check.check("2023년 3분기 영업손실은 1조 7,920억원이다.", rows, corp_code=HYNIX, as_of=AS_OF,
                         names=NAMES)
    assert r.status == "match"
    r = xbrl_check.check("2023년 3분기 영업이익은 1.8조원이다.", rows, corp_code=HYNIX, as_of=AS_OF, names=NAMES)
    assert r.status == "mismatch"


@pytest.mark.parametrize("text", ["2025년 영업이익은 −43.6조원이다.", "2025년 영업이익은 △43.6조원이다."])
def test_unicode_minus_is_negative_claim(text, facts):
    assert run(text, facts).status == "mismatch"   # 실제는 +43.6조


def test_items_record_claim_period(facts):
    r = run("2025년 1분기 및 2분기 매출은 333.6조원이다.", facts)
    assert all(it.claim_period is not None for it in r.items)


# ---- 근사 표시어('약·대략·가량·여·정도·안팎·수준')가 붙은 반올림 숫자 ----

HY_Q1 = dict(period="2026Q1", start="2026-01-01", end="2026-03-31")
HY_OP = [dict(_row("dart_OperatingIncomeLoss", 37_610_000_000_000, **HY_Q1), corp_code=HYNIX)]
SS_Q2 = dict(period="2026Q2", start="2026-04-01", end="2026-06-30")
SS_OP = [_row("dart_OperatingIncomeLoss", 89_490_000_000_000, **SS_Q2),
         _row("ifrs-full_Revenue", 1_234_000_000_000, **SS_Q2)]


@pytest.mark.parametrize("text, status", [
    ("SK하이닉스의 2026년 1분기 연결 영업이익은 약 40조원이다.", "match"),     # 37.61조 → 10조 단위로 40
    ("SK하이닉스의 2026년 1분기 연결 영업이익은 40조원 가량이다.", "match"),
    ("SK하이닉스의 2026년 1분기 연결 영업이익은 40조원 정도다.", "match"),
    ("SK하이닉스의 2026년 1분기 연결 영업이익은 40조원 안팎이다.", "match"),
    ("SK하이닉스의 2026년 1분기 연결 영업이익은 40조원 수준이다.", "match"),
    ("SK하이닉스의 2026년 1분기 연결 영업이익은 대략 40조원이다.", "match"),
    ("SK하이닉스의 2026년 1분기 연결 영업이익은 약 50조원이다.", "mismatch"),   # 실제로 틀린 값
    ("SK하이닉스의 2026년 1분기 연결 영업이익은 40조원이다.", "mismatch"),      # 표시어 없음: 지금 그대로(1조 단위)
    ("SK하이닉스의 2026년 1분기 연결 영업이익은 약 37.6조원이다.", "match"),    # 소수 있음: 지금 규칙
    ("SK하이닉스의 2026년 1분기 연결 영업이익은 약 40.0조원이다.", "mismatch"),  # 소수까지 적었으면 그 정밀도
])
def test_approximate_amount_precision(text, status):
    assert xbrl_check.check(text, HY_OP, corp_code=HYNIX, as_of=AS_OF, names=NAMES).status == status


@pytest.mark.parametrize("text, status", [
    ("2026년 2분기 영업이익은 약 90조원이다.", "match"),       # 89.49조
    ("2026년 2분기 영업이익은 90조원이다.", "mismatch"),
    ("2026년 2분기 매출은 약 1,200억원이다.", "mismatch"),     # 1조 2,340억 ≠ 1,200억(100억 단위)
    ("2026년 2분기 매출은 약 1조 2,300억원이다.", "match"),     # 100억 단위
    ("2026년 2분기 매출은 약 1조 2,000억원이다.", "match"),     # 1,000억 단위
])
def test_approximate_amount_units(text, status):
    assert run(text, SS_OP).status == status


@pytest.mark.parametrize("text, amount, status", [
    # 유효숫자 한 자리라 반올림 단위만으로는 너무 느슨한 경우: 상대오차 10% 상한으로 막는다
    ("2026년 2분기 영업이익은 약 100조원이다.", 54 * T, "mismatch"),            # 100조 단위로는 1이 같지만 85% 차이
    ("2026년 2분기 영업이익은 약 10조원이다.", 5_100_000_000_000, "mismatch"),   # 96% 차이
    ("2026년 2분기 영업이익은 약 2,000억원이다.", 150_000_000_000, "mismatch"),  # 33% 차이
    ("2026년 2분기 영업이익은 약 50조원이다.", 45 * T, "mismatch"),             # 11% 차이
    ("2026년 2분기 영업이익은 약 90조원이다.", 89_490_000_000_000, "match"),     # 0.6%
    ("2026년 2분기 영업이익은 약 40조원이다.", 37_610_000_000_000, "match"),     # 6.4%
    ("2026년 2분기 영업이익은 약 100조원이다.", 91 * T, "match"),               # 9.9%: 상한 안
    ("2026년 2분기 영업이익은 약 100조원이다.", 90 * T, "mismatch"),            # 11.1%: 상한 밖(분모는 실제값)
    ("2026년 2분기 영업이익은 54조원이다.", 54 * T, "match"),                   # 표시어 없음: 그대로
    ("2026년 2분기 영업이익은 100조원이다.", 54 * T, "mismatch"),
])
def test_approximate_amount_relative_cap(text, amount, status):
    assert xbrl_check.APPROX_REL_TOL == Decimal("0.10")
    assert run(text, [_row("dart_OperatingIncomeLoss", amount, **SS_Q2)]).status == status


# ---- 'A에서 B로' + 비교 기준 기간('전 분기·전년 동기·전년·직전 분기·전기'): A=비교 기준 기간, B=주장 기간 ----

def _hy(amount, period, start, end):
    return dict(_row("dart_OperatingIncomeLoss", amount, period=period, start=start, end=end), corp_code=HYNIX)


HY_ROWS = [_hy(37_610_000_000_000, "2026Q1", "2026-01-01", "2026-03-31"),
           _hy(60_540_000_000_000, "2026Q2", "2026-04-01", "2026-06-30"),
           _hy(9_210_000_000_000, "2025Q2", "2025-04-01", "2025-06-30")]
HY_HEAD = "SK하이닉스의 2026년 2분기 연결 영업이익은 "


@pytest.mark.parametrize("tail, status, periods", [
    ("전 분기 37.6조원에서 60.5조원으로 늘었다.", "match", ["2026Q1", "2026Q2"]),
    ("직전 분기 37.6조원에서 60.5조원으로 늘었다.", "match", ["2026Q1", "2026Q2"]),
    ("전기 37.6조원에서 60.5조원으로 늘었다.", "match", ["2026Q1", "2026Q2"]),
    ("전년 동기 9.2조원에서 60.5조원으로 늘었다.", "match", ["2025Q2", "2026Q2"]),
    ("전 분기 32.1조원에서 60.5조원으로 늘었다.", "mismatch", ["2026Q1", "2026Q2"]),   # 앞 금액만 틀림
    ("전 분기 37.6조원에서 70.5조원으로 늘었다.", "mismatch", ["2026Q1", "2026Q2"]),   # 뒤 금액만 틀림
    ("전년 동기 7.1조원에서 60.5조원으로 늘었다.", "mismatch", ["2025Q2", "2026Q2"]),
    ("전년 동기 9.2조원에서 70.5조원으로 늘었다.", "mismatch", ["2025Q2", "2026Q2"]),
    ("37.6조원에서 60.5조원으로 늘었다.", "match", ["2026Q2"]),                        # 비교 기준 표현 없음: 그대로 B만
])
def test_from_to_with_comparison_base(tail, status, periods):
    r = run(HY_HEAD + tail, HY_ROWS, corp=HYNIX)
    assert r.status == status, tail
    assert [it.period for it in r.items] == periods, tail


def test_from_to_comparison_base_uses_claim_period_not_as_of():
    # '전년'은 기준 시점이 아니라 주장 기간(2025년)의 전년(2024년)
    rows = [_row("dart_OperatingIncomeLoss", 43_600_000_000_000), _row("dart_OperatingIncomeLoss", 32_700_000_000_000,
                                                                       period="2024", start="2024-01-01",
                                                                       end="2024-12-31")]
    assert run("2025년 영업이익은 전년 32.7조원에서 43.6조원으로 늘었다.", rows).status == "match"
    r = run("2025년 영업이익은 전년 30.1조원에서 43.6조원으로 늘었다.", rows)
    assert r.status == "mismatch" and [it.period for it in r.items] == ["2024", "2025"]


@pytest.mark.parametrize("tail", [
    "전년 37.6조원에서 60.5조원으로 늘었다.",        # 분기 주장의 '전년': 전년 동기인지 전년 연간인지 불확실
    "전 분기 37.6조원에서 2026년 2분기 60.5조원으로 늘었다.",
])
def test_from_to_uncertain_base_is_not_mismatch(tail):
    r = run(HY_HEAD + tail, HY_ROWS, corp=HYNIX)
    assert r.status != "mismatch", tail


@pytest.mark.parametrize("claim, kind, want", [
    (Period(2026, "quarter", 1), "prev_q", "2025Q4"),
    (Period(2026, "quarter", 2), "prev", "2026Q1"),
    (Period(2026, "half", 1), "prev", "2025H2"),
    (Period(2026, "half", 2), "prev", "2026H1"),
    (Period(2025, "year"), "prev", "2024"),
    (Period(2025, "year"), "prev_y", "2024"),
    (Period(2026, "quarter", 2), "yoy", "2025Q2"),
    (Period(2026, "half", 1), "yoy", "2025H1"),
    (Period(2026, "quarter", 2), "prev_y", None),  # 분기 주장의 '전년': 불확실
    (Period(2026, "half", 1), "prev_q", None),
    (Period(2025, "year"), "prev_q", None),
    (Period(2025, "quarter", 3, cumulative=True), "prev_q", None),  # 누적의 앞 분기: 불확실
])
def test_base_period(claim, kind, want):
    got = xbrl_check._base_period(claim, kind)
    assert (got.label if got else None) == want


def test_base_period_yoy_keeps_cumulative():
    got = xbrl_check._base_period(Period(2025, "quarter", 3, cumulative=True), "yoy")
    assert (got.label, got.cumulative) == ("2024Q3", True)


# ---- 흔한 낱말 상장사명은 계정 앞말에서도 같은 규칙(법인 표시·선택 회사일 때만 회사 이름 앞말) ----

DAESANG, TAEYANG = "00000401", "00000402"
CW_IDX = CompanyIndex({SAMSUNG: ["삼성전자"], HYNIX: ["SK하이닉스"], DAESANG: ["대상"], TAEYANG: ["태양"]})


@pytest.mark.parametrize("text, corp, n", [
    ("2025년 2분기 태양 매출액은 3.2조원이다.", SAMSUNG, 0),           # 부문·지역 값: 대조하지 않는다
    ("삼성전자의 2025년 2분기 대상 매출액은 30.2조원이다.", SAMSUNG, 0),
    ("2025년 2분기 기업 고객 대상 매출액은 30.2조원이다.", SAMSUNG, 0),
    ("2025년 2분기 국내 매출액은 30.2조원이다.", SAMSUNG, 0),           # 사전에 없는 낱말: 그대로
    ("(주)대상의 2025년 2분기 매출액은 30.2조원이다.", SAMSUNG, 1),      # 법인 표시: 회사 이름 앞말
    ("㈜대상의 매출액은 30.2조원이다.", SAMSUNG, 1),
    ("주식회사 대상의 매출액은 30.2조원이다.", SAMSUNG, 1),
    ("대상의 매출액은 30.2조원이다.", DAESANG, 1),                     # 선택 회사
    ("2025년 2분기 대상 매출액은 30.2조원이다.", DAESANG, 1),
])
def test_common_word_name_as_account_modifier(text, corp, n):
    assert len(xbrl_check.amount_claims(text, CW_IDX, corp)) == n, text


@pytest.mark.parametrize("text", [
    "2025년 2분기 태양 매출액은 3.2조원이다.",
    "삼성전자의 2025년 2분기 대상 매출액은 30.2조원이다.",
    "2025년 2분기 기업 고객 대상 매출액은 30.2조원이다.",
])
def test_common_word_modifier_not_contradicted(text):
    rows = [_row("ifrs-full_Revenue", 74_566_317_000_000, period="2025Q2", start="2025-04-01", end="2025-06-30")]
    r = xbrl_check.check(text, rows, corp_code=SAMSUNG, as_of=AS_OF, names=CW_IDX)
    assert r.status != "mismatch", text


def test_selected_common_word_company_still_compared():
    rows = [dict(_row("ifrs-full_Revenue", 4_200_000_000_000, period="2025Q2", start="2025-04-01",
                      end="2025-06-30"), corp_code=DAESANG)]
    r = xbrl_check.check("대상의 2025년 2분기 매출액은 30.2조원이다.", rows, corp_code=DAESANG, as_of=AS_OF, names=CW_IDX)
    assert r.status == "mismatch"


def test_from_to_own_period_on_b_compares_b_only():
    r = run(HY_HEAD + "전 분기 37.6조원에서 2026년 1분기 60.5조원으로 늘었다.", HY_ROWS, corp=HYNIX)
    assert [it.period for it in r.items] == ["2026Q1"] and r.status == "mismatch"


def test_from_to_mixed_fs_pair_items():
    # A는 별도 Q1과만, B는 연결 Q2와만 맞음: 한 재무제표로 둘 다 맞지 않으니 A는 연결 불일치, B는 연결 일치로 남긴다
    q1 = {"period": "2025Q1", "start": "2025-01-01", "end": "2025-03-31"}
    q2 = {"period": "2025Q2", "start": "2025-04-01", "end": "2025-06-30"}
    rows = [_row("dart_OperatingIncomeLoss", 6_685_000_000_000, **q1),
            _row("dart_OperatingIncomeLoss", 1_000_000_000_000, fs="OFS", **q1),
            _row("dart_OperatingIncomeLoss", 4_676_057_000_000, **q2),
            _row("dart_OperatingIncomeLoss", 1_190_832_000_000, fs="OFS", **q2)]
    r = run("2025년 2분기 영업이익은 전 분기 1.0조원에서 4.7조원으로 늘었다.", rows)
    assert r.status == "mismatch"
    assert [(it.status, it.fs_div, it.period) for it in r.items] == [("mismatch", "CFS", "2025Q1"),
                                                                     ("match", "CFS", "2025Q2")]
