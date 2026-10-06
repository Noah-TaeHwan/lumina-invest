# app/services/factcheck/collect.py
"""팩트체커 공시 수집 CLI: 삼성전자·SK하이닉스 최근 3년 정기보고서(사업·반기·분기)와 잠정실적 공정공시.

    python -m app.services.factcheck.collect [--out lab/data/factcheck] [--years 3] [--end YYYYMMDD]

- 목록: OpenDART list.json을 페이지 끝까지 돈다(정기공시 A, 거래소공시 I). 근거 모드의 `list_annual_reports`는
  첫 페이지만 보므로 쓰지 않고, 같은 호출 함수(`evidence.dart._json`)만 import해 쓴다.
- 정기보고서: 보고서명 '사업보고서 (2025.12)'·'반기보고서 (2026.06)'·'분기보고서 (2025.03)'에서 종류·기간을 읽고,
  기간마다 가장 늦게 낸 1건(정정 포함)만 고른다. '[첨부정정]'·'[첨부추가]'는 다른 후보가 없을 때만 고른다.
- 잠정실적: 보고서명에 '영업(잠정)실적'이 든 공시를 원 공시·정정 공시 모두 남긴다. 어느 값을 쓸지(정정 > 원본)는
  판정 쪽(T2)이 정하고, 여기서는 같은 회사·기간·연결/별도에서 더 늦은 공시가 있으면 superseded=True로 표시한다.
  당기 값은 XBRL 계약 행(report_type="preliminary")으로도 xbrl_facts.json에 넣는다(최신 분기는 정기보고서 전).
  정정 잠정실적을 읽지 못하면 원 공시 값이 현재값으로 남을 수 있어 CLI가 종료 코드 1로 알린다.
- 원문: 근거 모드의 `download_document`(zip 안 가장 큰 xml, 원장 SHA-256)를 그대로 쓴다.
- XBRL: 고른 정기보고서마다 fnlttSinglAcntAll을 연결(CFS)·별도(OFS)로 받아 계약 행으로 바꾼다(xbrl.py).
  응답은 그 보고서의 최신 정정본이라 --end 뒤에 접수된 행은 버린다(기준 시점에 없던 값).
- 산출(out 아래, lab/data/는 gitignore): docs/{rcept_no}.xml, ledger.jsonl, documents.json(문서 목록·메타),
  xbrl_facts.json, corp_names.json(상장사명 사전).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

import httpx

from app.services.evidence.dart import _json, download_document, fetch_corp_codes, load_dart_key
from app.services.factcheck import corp_names
from app.services.factcheck.parse import decode, parse_prelim, prelim_facts, prelim_fs_div
from app.services.factcheck.xbrl import FS_DIVS, facts_from_response, fetch_accounts, save_facts

DEFAULT_CORPS = ("00126380", "00164779")  # 삼성전자, SK하이닉스
DEFAULT_OUT = Path("lab/data/factcheck")
PRELIM_MARK = "영업(잠정)실적"
PAGE_COUNT = 100
_REGULAR = re.compile(r"(사업|반기|분기)보고서\s*\((\d{4})\.(\d{2})\)")
_PERIOD_REPORT = {"": "11011", "H1": "11012", "Q1": "11013", "Q3": "11014"}


def list_filings(client: httpx.Client, key: str, corp_code: str, bgn: str, end: str, pblntf_ty: str) -> list[dict]:
    """공시 목록을 마지막 페이지까지 모은다. 데이터가 없으면(013) 빈 목록."""
    out: list[dict] = []
    page = 1
    while True:
        data = _json(client, "list.json", {"crtfc_key": key, "corp_code": corp_code, "bgn_de": bgn, "end_de": end,
                                           "pblntf_ty": pblntf_ty, "page_no": page, "page_count": PAGE_COUNT})
        out += data.get("list") or []
        if page >= int(data.get("total_page") or 1):
            return out
        page += 1


def classify_regular(report_nm: str) -> tuple[str, str] | None:
    """정기보고서명 → (report_type, period). 예: '반기보고서 (2026.06)' → ('half', '2026H1'). 아니면 None."""
    m = _REGULAR.search(report_nm)
    if not m:
        return None
    kind, year, month = m.group(1), m.group(2), m.group(3)
    if kind == "사업" and month == "12":
        return "annual", year
    if kind == "반기" and month == "06":
        return "half", f"{year}H1"
    if kind == "분기" and month in ("03", "09"):
        return "quarter", f"{year}Q{int(month) // 3}"
    return None


def _is_correction(report_nm: str) -> bool:
    return "[기재정정]" in report_nm


def _meta(item: dict, report_type: str, period: str | None) -> dict:
    return {"corp_code": item["corp_code"], "corp_name": item.get("corp_name", ""), "rcept_no": item["rcept_no"],
            "report_type": report_type, "report_nm": item["report_nm"].strip(), "period": period,
            "rcept_dt": item["rcept_dt"], "is_correction": _is_correction(item["report_nm"]), "superseded": False}


def pick_regular(items: list[dict]) -> list[dict]:
    """정기공시 목록에서 기간마다 최종본 1건을 골라 문서 메타로. 접수 순서로 돌려준다."""
    by_period: dict[tuple[str, str], list[dict]] = {}
    for it in items:
        c = classify_regular(it["report_nm"])
        if c:
            by_period.setdefault(c, []).append(it)
    out = []
    for (rtype, period), cands in by_period.items():
        main = [c for c in cands if not c["report_nm"].lstrip().startswith("[첨부")] or cands
        out.append(_meta(max(main, key=lambda r: (r["rcept_dt"], r["rcept_no"])), rtype, period))
    return sorted(out, key=lambda d: (d["rcept_dt"], d["rcept_no"]))


def pick_prelim(items: list[dict]) -> list[dict]:
    """거래소공시 목록에서 영업(잠정)실적 공정공시만(원 공시·정정 모두). 기간은 원문을 읽은 뒤 채운다.

    보고서명에 '연결'이 들면 연결(CFS), 아니면 별도(OFS) 기준으로 fs_div를 단다.
    """
    out = [_meta(it, "preliminary", None) | {"fs_div": prelim_fs_div(it["report_nm"])}
           for it in items if PRELIM_MARK in it["report_nm"]]
    return sorted(out, key=lambda d: (d["rcept_dt"], d["rcept_no"]))


def mark_superseded(docs: list[dict]) -> None:
    """같은 회사·종류·기간·연결/별도에서 가장 늦은 공시만 superseded=False, 나머지는 True(제자리 수정)."""
    groups: dict[tuple, list[dict]] = {}
    for d in docs:
        groups.setdefault((d["corp_code"], d["report_type"], d["period"], d.get("fs_div")), []).append(d)
    for ds in groups.values():
        latest = max(ds, key=lambda d: (d["rcept_dt"], d["rcept_no"]))
        for d in ds:
            d["superseded"] = d is not latest


def _report_code(period: str) -> tuple[str, str]:
    """정기보고서 기간 → (사업연도, 보고서 코드). 예: '2026H1' → ('2026', '11012')."""
    return period[:4], _PERIOD_REPORT[period[4:]]


def collect(client: httpx.Client, key: str, corps, bgn: str, end: str, out_dir: Path) -> dict:
    """회사들의 문서 목록·원문·XBRL을 모아 out_dir에 쓴다. 요약 수를 돌려준다."""
    out_dir = Path(out_dir)
    docs_dir, ledger = out_dir / "docs", out_dir / "ledger.jsonl"
    manifest: list[dict] = []
    facts: list[dict] = []
    reports: dict[str, object] = {}  # 잠정실적 rcept_no → PrelimReport
    n_reg = n_pre = after_end = 0
    for corp in corps:
        listed = list_filings(client, key, corp, bgn, end, "A")
        regular = pick_regular(listed)
        prelim = pick_prelim(list_filings(client, key, corp, bgn, end, "I"))
        n_reg, n_pre = n_reg + len(regular), n_pre + len(prelim)
        for d in regular + prelim:
            path = download_document(client, key, d["rcept_no"], docs_dir, ledger)
            d["path"] = path.relative_to(out_dir).as_posix()
            if d["report_type"] == "preliminary":
                try:
                    reports[d["rcept_no"]] = parse_prelim(decode(path.read_bytes()))
                    d["period"] = reports[d["rcept_no"]].period
                except ValueError as exc:
                    d["parse_error"] = str(exc)
            manifest.append(d)
        by_rcept = {it["rcept_no"]: it for it in listed}
        for d in regular:
            year, code = _report_code(d["period"])
            for fs in FS_DIVS:
                resp = fetch_accounts(client, key, corp, year, code, fs)
                rows = resp.get("list") or []
                if not rows:
                    continue
                src = by_rcept.get(rows[0]["rcept_no"])  # XBRL이 가리키는 접수번호(고른 문서와 다를 수 있다)
                got = facts_from_response(resp, fs_div=fs, rcept_dt=src["rcept_dt"] if src else None,
                                          is_correction=_is_correction(src["report_nm"]) if src else False)
                kept = [f for f in got if f["rcept_dt"] <= end]  # --end 뒤에 낸 정정본 값은 기준 시점에 없었다
                after_end += len(got) - len(kept)
                facts += kept
    mark_superseded(manifest)
    for d in manifest:
        if d["rcept_no"] in reports:
            facts += prelim_facts(d["corp_code"], d["rcept_no"], reports[d["rcept_no"]], rcept_dt=d["rcept_dt"],
                                  is_correction=d["is_correction"], superseded=d["superseded"],
                                  fs_div=d.get("fs_div"))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "documents.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    save_facts(out_dir / "xbrl_facts.json", facts)
    return {"documents": len(manifest), "regular": n_reg, "preliminary": n_pre, "xbrl_rows": len(facts),
            "xbrl_after_end": after_end,
            "parse_errors": {d["rcept_no"]: d["parse_error"] for d in manifest if d.get("parse_error")},
            "correction_parse_errors": [d["rcept_no"] for d in manifest
                                        if d.get("parse_error") and d["is_correction"]]}


def collect_corp_names(client: httpx.Client, key: str, out_dir: Path) -> int:
    """상장사명 사전(corp_names.json)을 만든다. 항목 수를 돌려준다."""
    entries = corp_names.build(fetch_corp_codes(client, key))
    corp_names.save(Path(out_dir) / "corp_names.json", entries)
    return len(entries)


def _years_before(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year - years)
    except ValueError:  # 2월 29일
        return d.replace(year=d.year - years, day=28)


def main(argv: list[str] | None = None) -> int:
    """CLI 진입점. 키는 DART_API_KEY 또는 ~/.config/opendart/api_key(0600)."""
    ap = argparse.ArgumentParser(description="팩트체커 공시 수집(정기보고서·잠정실적·XBRL·상장사명 사전)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--corps", nargs="+", default=list(DEFAULT_CORPS))
    ap.add_argument("--years", type=int, default=3)
    ap.add_argument("--end", default=date.today().strftime("%Y%m%d"))
    ap.add_argument("--skip-corp-names", action="store_true")
    args = ap.parse_args(argv)
    key = load_dart_key()
    end = datetime.strptime(args.end, "%Y%m%d").date()
    bgn = _years_before(end, args.years).strftime("%Y%m%d")
    with httpx.Client(timeout=120) as client:
        summary = collect(client, key, args.corps, bgn, args.end, args.out)
        if not args.skip_corp_names:
            summary["corp_names"] = collect_corp_names(client, key, args.out)
    summary["range"] = [bgn, args.end]
    print(json.dumps(summary, ensure_ascii=False))
    for rno, err in summary["parse_errors"].items():
        kind = "정정 공시" if rno in summary["correction_parse_errors"] else "공시"
        print(f"{kind} {rno} 분해 실패: {err}", file=sys.stderr)
    if summary["correction_parse_errors"]:
        print("정정 잠정실적을 읽지 못해 원 공시 값이 현재값으로 남을 수 있다. 양식을 확인한 뒤 다시 수집하라.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
