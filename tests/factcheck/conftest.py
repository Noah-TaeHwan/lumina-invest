"""팩트체커 테스트 공용: 고정 XBRL 응답(OpenDART fnlttSinglAcntAll, 실제 공개 공시)을 데이터 계약 행으로 바꾼다.

실제 적재기(T1 factcheck/collect)를 대신하는 테스트 전용 변환이다. 계약(설계 Codex #1):
{corp_code, period, fs_div, account_id, account_nm, amount(원), rcept_no, period_start, period_end,
 value_kind∈{instant,duration}, cumulative, currency, unit, rcept_dt, is_correction} + report_type(가정, PR 본문 참고).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
_REPORT = {"11011": "periodic", "11012": "periodic", "11014": "periodic"}


def _row(r: dict, fs_div: str, amount: str, period: str, start: str | None, end: str, kind: str, cum: bool,
         report_type: str, column: str) -> dict:
    return {"corp_code": r["corp_code"], "period": period, "fs_div": fs_div, "account_id": r["account_id"],
            "account_nm": r["account_nm"], "amount": int(amount), "rcept_no": r["rcept_no"],
            "period_start": start, "period_end": end, "value_kind": kind, "cumulative": cum,
            "currency": r.get("currency", "KRW"), "unit": "원", "rcept_dt": r["rcept_no"][:8],
            "is_correction": False, "report_type": report_type, "column": column}


def convert(raw: dict, fs_div: str) -> list[dict]:
    """fnlttSinglAcntAll 응답 하나를 계약 행으로. 재무상태표(BS)와 손익(IS·CIS, 한 계정은 한 번)만."""
    out: list[dict] = []
    seen: set[tuple] = set()
    for r in raw["list"]:
        if r["sj_div"] not in ("BS", "IS", "CIS"):
            continue
        y, code = int(r["bsns_year"]), r["reprt_code"]
        rt = _REPORT[code]
        cand: list[tuple] = []  # (금액 칸, period, start, end, kind, cumulative)
        if r["sj_div"] == "BS":
            end = {"11011": f"{y}-12-31", "11012": f"{y}-06-30", "11014": f"{y}-09-30"}[code]
            label = {"11011": f"{y}", "11012": f"{y}H1", "11014": f"{y}Q3"}[code]
            cand.append(("thstrm_amount", label, None, end, "instant", False))
            cand.append(("frmtrm_amount", f"{y - 1}", None, f"{y - 1}-12-31", "instant", False))
            if code == "11011":
                cand.append(("bfefrmtrm_amount", f"{y - 2}", None, f"{y - 2}-12-31", "instant", False))
        elif code == "11011":
            for k, yy in (("thstrm_amount", y), ("frmtrm_amount", y - 1), ("bfefrmtrm_amount", y - 2)):
                cand.append((k, f"{yy}", f"{yy}-01-01", f"{yy}-12-31", "duration", True))
        else:
            q, qs, qe = (2, "04-01", "06-30") if code == "11012" else (3, "07-01", "09-30")
            cum_label = (lambda yy: f"{yy}H1") if code == "11012" else (lambda yy: f"{yy}Q3")
            cand += [("thstrm_amount", f"{y}Q{q}", f"{y}-{qs}", f"{y}-{qe}", "duration", False),
                     ("thstrm_add_amount", cum_label(y), f"{y}-01-01", f"{y}-{qe}", "duration", True),
                     ("frmtrm_q_amount", f"{y - 1}Q{q}", f"{y - 1}-{qs}", f"{y - 1}-{qe}", "duration", False),
                     ("frmtrm_add_amount", cum_label(y - 1), f"{y - 1}-01-01", f"{y - 1}-{qe}", "duration", True)]
        for key, label, start, end, kind, cum in cand:
            amount = (r.get(key) or "").replace(",", "")
            ident = (r["account_id"], start, end, fs_div)
            if not amount or ident in seen:
                continue
            seen.add(ident)
            out.append(_row(r, fs_div, amount, label, start, end, kind, cum, rt, key))
    return out


def load_facts(*names: str) -> list[dict]:
    """fixtures/xbrl_<name>_<CFS|OFS>.json 묶음을 계약 행 목록으로. 이름이 없으면 전부."""
    paths = sorted(FIXTURES.glob("xbrl_*.json"))
    rows: list[dict] = []
    for p in paths:
        stem = p.stem  # xbrl_samsung_2025_11011_CFS
        if names and not any(n in stem for n in names):
            continue
        rows += convert(json.loads(p.read_text(encoding="utf-8")), stem.rsplit("_", 1)[1])
    return rows


@pytest.fixture(scope="session")
def facts() -> list[dict]:
    return load_facts()
