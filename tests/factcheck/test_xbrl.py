# tests/factcheck/test_xbrl.py
"""XBRL 재무 수치(fnlttSinglAcntAll) → 확장 계약 행.

고정 자료는 실제 공개 공시 응답(핵심 계정만, tests/factcheck/fixtures/xbrl_*.json)이다. 외부 호출 없음.
분기·반기 손익은 thstrm=분기 단독, thstrm_add=누적, 사업보고서는 당기·전기·전전기, 재무상태표는 시점 값이다.
"""
import json
from pathlib import Path

import httpx

from app.services.factcheck import xbrl

FIX = Path(__file__).parent / "fixtures"


def _resp(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def _rows(name: str, fs_div: str = "CFS", **kw) -> list[dict]:
    return xbrl.facts_from_response(_resp(name), fs_div=fs_div, **kw)


def _one(rows, account_id, period, cumulative=False, value_kind=None):
    hit = [r for r in rows if r["account_id"] == account_id and r["period"] == period
           and r["cumulative"] == cumulative and (value_kind is None or r["value_kind"] == value_kind)]
    assert len(hit) == 1, hit
    return hit[0]


def test_half_report_splits_quarter_standalone_and_cumulative():
    rows = _rows("xbrl_samsung_2026_11012_CFS.json")
    q2 = _one(rows, "ifrs-full_Revenue", "2026Q2", cumulative=False)
    assert q2["amount"] == 171_499_470_000_000
    assert (q2["period_start"], q2["period_end"], q2["value_kind"]) == ("2026-04-01", "2026-06-30", "duration")
    assert q2["column"] == "thstrm"
    h1 = _one(rows, "ifrs-full_Revenue", "2026H1", cumulative=True)
    assert h1["amount"] == 305_372_914_000_000
    assert (h1["period_start"], h1["period_end"]) == ("2026-01-01", "2026-06-30")
    assert h1["column"] == "thstrm_add"
    # 전년 동기 비교값(단독·누적)도 칸 출처와 함께 남긴다
    assert _one(rows, "ifrs-full_Revenue", "2025Q2")["amount"] == 74_566_317_000_000
    assert _one(rows, "ifrs-full_Revenue", "2025H1", cumulative=True)["column"] == "frmtrm_add"


def test_q3_report_cumulative_is_nine_months_with_same_period_label():
    rows = _rows("xbrl_samsung_2025_11014_CFS.json")
    q3 = _one(rows, "dart_OperatingIncomeLoss", "2025Q3", cumulative=False)
    assert q3["amount"] == 12_166_062_000_000
    assert (q3["period_start"], q3["period_end"]) == ("2025-07-01", "2025-09-30")
    m9 = _one(rows, "dart_OperatingIncomeLoss", "2025Q3", cumulative=True)
    assert m9["amount"] == 23_527_391_000_000
    assert (m9["period_start"], m9["period_end"]) == ("2025-01-01", "2025-09-30")


def test_annual_report_current_prior_and_two_years_back():
    rows = _rows("xbrl_samsung_2025_11011_CFS.json", rcept_dt="20260310", is_correction=True)
    rev = {r["period"]: r for r in rows if r["account_id"] == "ifrs-full_Revenue"}
    assert {p: r["amount"] for p, r in rev.items()} == {
        "2025": 333_605_938_000_000, "2024": 300_870_903_000_000, "2023": 258_935_494_000_000}
    assert [rev[p]["column"] for p in ("2025", "2024", "2023")] == ["thstrm", "frmtrm", "bfefrmtrm"]
    r = rev["2024"]
    assert (r["period_start"], r["period_end"], r["cumulative"]) == ("2024-01-01", "2024-12-31", False)
    assert (r["rcept_dt"], r["is_correction"], r["rcept_no"]) == ("20260310", True, "20260310002820")
    # 빈 thstrm_add_amount('')는 행을 만들지 않는다
    assert not [x for x in rows if x["column"] == "thstrm_add"]


def test_balance_sheet_is_instant_at_period_end():
    rows = _rows("xbrl_samsung_2026_11012_CFS.json")
    a = _one(rows, "ifrs-full_Assets", "2026H1")
    assert a["amount"] == 759_480_516_000_000
    assert (a["value_kind"], a["period_start"], a["period_end"], a["cumulative"]) == (
        "instant", "2026-06-30", "2026-06-30", False)
    prev = _one(rows, "ifrs-full_Assets", "2025")  # 전기말
    assert (prev["value_kind"], prev["period_end"], prev["amount"]) == ("instant", "2025-12-31", 566_942_110_000_000)


def test_contract_fields_and_defaults():
    rows = _rows("xbrl_skhynix_2026_11012_OFS.json", fs_div="OFS")
    need = {"corp_code", "period", "period_start", "period_end", "value_kind", "cumulative", "fs_div", "account_id",
            "account_nm", "amount", "currency", "unit", "rcept_no", "rcept_dt", "is_correction"}
    for r in rows:
        assert need <= r.keys()
        assert (r["fs_div"], r["currency"], r["unit"], r["corp_code"]) == ("OFS", "KRW", "원", "00164779")
        assert isinstance(r["amount"], int)
        assert r["rcept_dt"] == r["rcept_no"][:8]  # 목록 정보가 없으면 접수번호 앞 8자리(접수일)
        assert r["is_correction"] is False


def test_only_fixed_accounts_no_sce_or_cf_and_no_duplicates():
    rows = _rows("xbrl_samsung_2026_11012_CFS.json")
    assert {r["account_id"] for r in rows} <= set(xbrl.ACCOUNTS)
    assert {r["sj_div"] for r in rows} <= {"IS", "CIS", "BS"}
    # 자본변동표(SCE) 구성요소 값이 섞이지 않는다
    assert 897_514_000_000 not in {r["amount"] for r in rows}
    keys = [(r["account_id"], r["period"], r["cumulative"], r["value_kind"], r["column"]) for r in rows]
    assert len(keys) == len(set(keys))
    # 삼성전자 순이익은 IS·CIS 둘 다 있지만 IS 한 벌만
    assert _one(rows, "ifrs-full_ProfitLoss", "2026Q2")["sj_div"] == "IS"
    assert _one(rows, "ifrs-full_ProfitLoss", "2026Q2")["account_nm"] == xbrl.ACCOUNTS["ifrs-full_ProfitLoss"]


def test_cis_fallback_and_negative_amount():
    rows = _rows("xbrl_skhynix_2025_11011_OFS.json", fs_div="OFS")
    op = _one(rows, "dart_OperatingIncomeLoss", "2023")
    assert (op["sj_div"], op["amount"]) == ("CIS", -4_672_124_000_000)
    assert _one(rows, "ifrs-full_Revenue", "2025")["amount"] == 86_852_117_000_000


def test_report_period_mapping():
    assert xbrl.report_period("2026", "11013") == ("2026Q1", "2026-01-01", "2026-03-31")
    assert xbrl.report_period("2026", "11012") == ("2026H1", "2026-01-01", "2026-06-30")
    assert xbrl.report_period("2025", "11014") == ("2025Q3", "2025-01-01", "2025-09-30")
    assert xbrl.report_period("2025", "11011") == ("2025", "2025-01-01", "2025-12-31")


def test_q1_cumulative_is_not_duplicated():
    resp = {"status": "000", "list": [{
        "rcept_no": "20260515000001", "reprt_code": "11013", "bsns_year": "2026", "corp_code": "00126380",
        "sj_div": "IS", "account_id": "ifrs-full_Revenue", "account_nm": "매출액", "account_detail": "-",
        "thstrm_amount": "100", "thstrm_add_amount": "100", "frmtrm_q_amount": "90", "frmtrm_add_amount": "90",
        "currency": "KRW"}]}
    rows = xbrl.facts_from_response(resp, fs_div="CFS")
    assert sorted((r["period"], r["cumulative"], r["amount"]) for r in rows) == [
        ("2025Q1", False, 90), ("2026Q1", False, 100)]


def test_save_load_roundtrip(tmp_path):
    rows = _rows("xbrl_skhynix_2025_11014_CFS.json")
    path = tmp_path / "xbrl_facts.json"
    xbrl.save_facts(path, rows)
    assert xbrl.load_facts(path) == rows


def test_fetch_sends_params_and_treats_013_as_empty():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.params["fs_div"] == "OFS":
            return httpx.Response(200, json={"status": "013", "message": "조회된 데이타가 없습니다."})
        return httpx.Response(200, json=_resp("xbrl_samsung_2026_11012_CFS.json"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    got = xbrl.fetch_accounts(client, "KEY", "00126380", "2026", "11012", "CFS")
    assert got["status"] == "000"
    assert xbrl.fetch_accounts(client, "KEY", "00126380", "2026", "11012", "OFS").get("list", []) == []
    p = seen[0].url.params
    assert seen[0].url.path.endswith("/fnlttSinglAcntAll.json")
    assert (p["corp_code"], p["bsns_year"], p["reprt_code"], p["fs_div"], p["crtfc_key"]) == (
        "00126380", "2026", "11012", "CFS", "KEY")


def test_periodic_rows_are_marked_and_not_superseded():
    rows = _rows("xbrl_samsung_2026_11012_CFS.json")
    assert {r["report_type"] for r in rows} == {"periodic"}
    assert {r["superseded"] for r in rows} == {False}
    assert {r["rounding_unit"] for r in rows} == {1}


def test_duplicate_account_column_in_one_response_raises():
    resp = _resp("xbrl_samsung_2026_11012_CFS.json")
    dup = next(r for r in resp["list"] if r["account_id"] == "ifrs-full_Revenue")
    resp["list"].append(dup | {"thstrm_amount": "1"})
    import pytest
    with pytest.raises(ValueError, match="ifrs-full_Revenue"):
        xbrl.facts_from_response(resp, fs_div="CFS")
