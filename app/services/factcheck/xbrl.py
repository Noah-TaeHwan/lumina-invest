# app/services/factcheck/xbrl.py
"""XBRL 재무 수치: OpenDART 단일회사 전체 재무제표(fnlttSinglAcntAll) 응답 → 확장 계약 행.

계약(설계 Outside Voice #1): 행 하나 = 한 회사·한 기간·한 계정·연결/별도 하나의 값.
`{corp_code, period, period_start, period_end, value_kind, cumulative, fs_div, account_id, account_nm, amount(원),
currency, unit, rcept_no, rcept_dt, is_correction}`에 출처 칸 `column`·`sj_div`·`reprt_code`·`bsns_year`와
`report_type`(정기 XBRL은 "periodic", 잠정실적은 parse.prelim_facts의 "preliminary"), `superseded`,
`rounding_unit`(금액이 반올림된 원 단위: XBRL 1, 잠정실적 조원 둘째 자리면 10^10)을 더한다.
한 응답 안에서 (account_id, 칸)이 겹치면 어느 값이 맞는지 모르므로 ValueError로 멈춘다.

- 기간 이름 `period`는 끝나는 시점으로 붙인다: 1년 "YYYY", 1~6월 "YYYYH1", 그 밖은 끝 분기 "YYYYQn".
  3분기 누적(1~9월)은 따로 이름이 없어 "YYYYQ3" + cumulative=True다. 그래서 값을 찾을 때는
  (period, cumulative)로, 확실히 하려면 period_start·period_end로 맞춘다.
- 손익(duration): 분기·반기보고서의 thstrm=그 분기 단독, thstrm_add=연초부터 누적(cumulative=True).
  1분기는 단독=누적이라 thstrm 한 행만 둔다. 사업보고서는 thstrm·frmtrm·bfefrmtrm = 당기·전기·전전기.
  분기·반기의 전년 동기 비교값(frmtrm_q·frmtrm_add)도 `column`으로 출처를 남겨 함께 둔다.
- 재무상태표(instant): period_start = period_end = 그 시점. 분기·반기의 frmtrm은 전기말(전년 12월 31일).
- 손익은 IS(손익계산서)를 쓰고, 그 계정이 IS에 없으면 CIS(포괄손익계산서)를 쓴다(SK하이닉스는 CIS만).
  지배기업 소유주지분 순이익이 IS·CIS에 없으면 자본변동표(SCE)의 당기순이익 행 중 '…|지배기업 소유주…' 합계
  구성요소(경로 두 단계, 하위 구성요소 아님)를 쓴다. 자본변동표 값은 연초부터 누적이라 분기·반기는 thstrm_add·
  frmtrm_add(누적) 칸으로 둔다(1분기는 단독=누적).
  현금흐름표(CF)·자본변동표(SCE)는 쓰지 않는다. account_detail이 '-'인 합계 행만 쓴다.
- 결산월은 12월로 가정한다(삼성전자·SK하이닉스). 응답에는 접수일·정정 여부가 없어 호출자가 공시 목록에서
  넘긴다. 접수일이 없으면 접수번호 앞 8자리(DART 접수일)를 쓴다.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import httpx

from app.services.evidence.dart import _json

# 계정 고정 목록(account_id → 표준 이름). 응답의 account_nm은 '반기순이익'처럼 보고서마다 달라 표준 이름을 쓴다.
ACCOUNTS: dict[str, str] = {
    "ifrs-full_Revenue": "매출액",
    "dart_OperatingIncomeLoss": "영업이익",
    "ifrs-full_ProfitLoss": "당기순이익",
    "ifrs-full_ProfitLossAttributableToOwnersOfParent": "지배기업 소유주지분 순이익",
    "ifrs-full_Assets": "자산총계",
    "ifrs-full_Liabilities": "부채총계",
    "ifrs-full_Equity": "자본총계",
}
INSTANT_ACCOUNTS = frozenset({"ifrs-full_Assets", "ifrs-full_Liabilities", "ifrs-full_Equity"})
REPORT_CODES = {"11013": 3, "11012": 6, "11014": 9, "11011": 12}  # 보고서 코드 → 끝나는 달
FS_DIVS = ("CFS", "OFS")
UNIT = "원"

# 보고서 종류별 칸: (칸 이름, 연도 차이, 종류) — q=분기 단독, ytd=연초부터 누적, fy=1년, inst=시점
_FLOW_COLS = {
    3: [("thstrm", 0, "q"), ("frmtrm_q", -1, "q")],
    6: [("thstrm", 0, "q"), ("thstrm_add", 0, "ytd"), ("frmtrm_q", -1, "q"), ("frmtrm_add", -1, "ytd")],
    9: [("thstrm", 0, "q"), ("thstrm_add", 0, "ytd"), ("frmtrm_q", -1, "q"), ("frmtrm_add", -1, "ytd")],
    12: [("thstrm", 0, "fy"), ("frmtrm", -1, "fy"), ("bfefrmtrm", -2, "fy")],
}
# 자본변동표(SCE) 칸: 응답 칸 이름 → (계약 칸 이름, 연도 차이, 종류, 응답 칸). 분기·반기 값은 연초부터 누적이다
_SCE_COLS = {
    3: [("thstrm", 0, "q", "thstrm"), ("frmtrm_q", -1, "q", "frmtrm_q")],
    6: [("thstrm_add", 0, "ytd", "thstrm"), ("frmtrm_add", -1, "ytd", "frmtrm_q")],
    9: [("thstrm_add", 0, "ytd", "thstrm"), ("frmtrm_add", -1, "ytd", "frmtrm_q")],
    12: [("thstrm", 0, "fy", "thstrm"), ("frmtrm", -1, "fy", "frmtrm"), ("bfefrmtrm", -2, "fy", "bfefrmtrm")],
}
_INSTANT_COLS = {3: [("thstrm", 0), ("frmtrm", -1)], 6: [("thstrm", 0), ("frmtrm", -1)],
                 9: [("thstrm", 0), ("frmtrm", -1)], 12: [("thstrm", 0), ("frmtrm", -1), ("bfefrmtrm", -2)]}


def _month_end(year: int, month: int) -> date:
    """그 달의 마지막 날."""
    nxt = date(year + month // 12, month % 12 + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1)


def period_label(start: date, end: date) -> str:
    """기간 이름: 1~12월(또는 12월 말 시점) "YYYY", 1~6월(또는 6월 말 시점) "YYYYH1", 그 밖은 끝 분기 "YYYYQn"."""
    if end.month == 12 and (start.month == 1 or start == end):
        return f"{end.year}"
    if end.month == 6 and (start.month == 1 or start == end):
        return f"{end.year}H1"
    return f"{end.year}Q{(end.month - 1) // 3 + 1}"


def report_period(bsns_year: str, reprt_code: str) -> tuple[str, str, str]:
    """보고서가 다루는 기간(연초부터): (period, 시작일, 종료일). 예: ("2026", "11012") → ("2026H1", 1/1, 6/30)."""
    y, m = int(bsns_year), REPORT_CODES[reprt_code]
    start, end = date(y, 1, 1), _month_end(y, m)
    return period_label(start, end), start.isoformat(), end.isoformat()


def _amount(raw) -> int | None:
    """'1,234'·'-5'를 정수 원으로. 빈 칸·'-'는 None."""
    s = str(raw or "").replace(",", "").strip()
    if s in ("", "-"):
        return None
    return int(s)


def _span(year: int, end_month: int, kind: str) -> tuple[date, date]:
    end = _month_end(year, end_month)
    if kind == "q":
        return date(year, end_month - 2, 1), end
    if kind == "inst":
        return end, end
    return date(year, 1, 1), end  # ytd·fy


OWNERS = "ifrs-full_ProfitLossAttributableToOwnersOfParent"
_OWNERS_DETAIL = re.compile(r"^[^|]+\|\s*지배기업\S*\s*소유주")  # '자본의 구성요소 [도메인]|지배기업 소유주지분' 등


def _sce_owner_rows(items: list[dict]) -> list[dict]:
    """자본변동표(SCE) 당기순이익 행 중 지배기업 소유주지분 합계 구성요소(경로 두 단계)를 지배주주 순이익 행으로."""
    out = []
    for r in items:
        detail = r.get("account_detail") or ""
        if r.get("sj_div") == "SCE" and r.get("account_id") == "ifrs-full_ProfitLoss" and \
                detail.count("|") == 1 and _OWNERS_DETAIL.match(detail):
            out.append({**r, "account_id": OWNERS, "_sce": True})
    return out[:1]


def _pick_rows(items: list[dict]) -> list[dict]:
    """고정 계정의 합계 행만 고른다: 재무상태표는 BS, 손익은 IS 우선·없으면 CIS, 지배주주 순이익은 그다음 SCE."""
    out: list[dict] = []
    for acc in ACCOUNTS:
        rows = [r for r in items if r.get("account_id") == acc and (r.get("account_detail") or "-") == "-"]
        if acc in INSTANT_ACCOUNTS:
            out += [r for r in rows if r.get("sj_div") == "BS"]
        else:
            got = [r for r in rows if r.get("sj_div") == "IS"] or [r for r in rows if r.get("sj_div") == "CIS"]
            out += got or (_sce_owner_rows(items) if acc == OWNERS else [])
    return out


def facts_from_response(resp: dict, *, fs_div: str, rcept_dt: str | None = None,
                        is_correction: bool = False) -> list[dict]:
    """fnlttSinglAcntAll 응답 하나(한 보고서·한 fs_div)를 계약 행 목록으로 바꾼다."""
    if fs_div not in FS_DIVS:
        raise ValueError(f"fs_div must be CFS or OFS: {fs_div}")
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for r in _pick_rows(resp.get("list") or []):
        end_month, year = REPORT_CODES[r["reprt_code"]], int(r["bsns_year"])
        instant = r["account_id"] in INSTANT_ACCOUNTS
        cols = ([(c, off, "inst") for c, off in _INSTANT_COLS[end_month]] if instant
                else _SCE_COLS[end_month] if r.get("_sce") else _FLOW_COLS[end_month])
        for col, off, kind, *src in cols:
            amount = _amount(r.get(f"{src[0] if src else col}_amount"))
            if amount is None:
                continue
            if (r["account_id"], col) in seen:
                raise ValueError(f"duplicate {r['account_id']} {col} in one response ({r['rcept_no']})")
            seen.add((r["account_id"], col))
            em = 12 if (instant and col != "thstrm") else end_month  # 시점 값의 전기·전전기 = 그해 말
            start, end = _span(year + off, em, kind)
            out.append({
                "corp_code": r["corp_code"], "period": period_label(start, end),
                "period_start": start.isoformat(), "period_end": end.isoformat(),
                "value_kind": "instant" if instant else "duration",
                "cumulative": kind == "ytd",
                "fs_div": fs_div, "account_id": r["account_id"], "account_nm": ACCOUNTS[r["account_id"]],
                "amount": amount, "currency": r.get("currency") or "KRW", "unit": UNIT,
                "rcept_no": r["rcept_no"], "rcept_dt": rcept_dt or r["rcept_no"][:8],
                "is_correction": bool(is_correction),
                "report_type": "periodic", "superseded": False, "rounding_unit": 1,
                "column": col, "sj_div": r["sj_div"], "reprt_code": r["reprt_code"], "bsns_year": r["bsns_year"],
            })
    return out


def fetch_accounts(client: httpx.Client, key: str, corp_code: str, bsns_year: str, reprt_code: str,
                   fs_div: str) -> dict:
    """OpenDART 단일회사 전체 재무제표 응답(JSON). 데이터 없음(013)은 list 없는 응답으로 돌려준다."""
    return _json(client, "fnlttSinglAcntAll.json", {"crtfc_key": key, "corp_code": corp_code,
                                                    "bsns_year": bsns_year, "reprt_code": reprt_code,
                                                    "fs_div": fs_div})


def save_facts(path: Path, rows: list[dict]) -> None:
    """계약 행 목록을 JSON으로 쓴다(임시 파일 뒤 바꿔 넣기)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    tmp.replace(path)


def load_facts(path: Path) -> list[dict]:
    """save_facts로 쓴 JSON을 읽는다."""
    return json.loads(Path(path).read_text())
