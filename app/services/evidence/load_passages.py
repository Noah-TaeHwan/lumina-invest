"""근거 문단 적재 CLI(A-2 spec P3): OpenDART 사업보고서 → 문단 → 임베딩 → Qdrant evidence_passages.

    python -m app.services.evidence.load_passages                       # 시드 15개사(graph_service._COMPANIES)
    python -m app.services.evidence.load_passages --corp 005930 --corp 00164779   # 종목코드(6자리)·고유번호(8자리)
    python -m app.services.evidence.load_passages --search 00126380 "주요 제품은?"  # 적재 확인: 상위 8개 문단 id

- 회사마다 dart.list_annual_reports → pick_annual_report(평가와 같은 기본값: 2025.12, 20261001까지) →
  download_document → passages.build_passages → store.load. 같은 rcept_no·sha256 문단은 임베딩을 건너뛴다.
- 키는 dart.load_dart_key()로만 읽는다(인자로 받지 않는다). 출력·오류 메시지에서 키 문자열을 지운다.
  httpx 요청 로그(URL에 키가 실린다)는 끈다.
- 한 회사가 실패해도 나머지를 계속한다. 끝에 회사별 요약을 출력하고, 실패가 있으면 종료 코드 1이다.
- 원문 XML·고유번호 목록은 --data-dir(기본 data/evidence_passages/, gitignore)에 둔다.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import httpx

from app.services.evidence import dart, passages, store as store_mod
from app.services.evidence.dart import Corp

DEFAULT_DATA_DIR = Path("data/evidence_passages")


@dataclass
class Outcome:
    corp: Corp
    status: str  # loaded | unchanged | no_report | failed
    rcept_no: str = ""
    result: store_mod.LoadResult | None = None
    error: str = ""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m app.services.evidence.load_passages",
                                 description="OpenDART 사업보고서 문단을 Qdrant evidence_passages에 적재한다")
    ap.add_argument("--corp", action="append", default=[],
                    help="종목코드(6자리) 또는 DART 고유번호(8자리). 여러 번 줄 수 있다. 없으면 시드 15개사")
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="원문 XML·고유번호 목록 보관 위치")
    ap.add_argument("--search", nargs=2, metavar=("CORP_CODE", "QUESTION"),
                    help="적재하지 않고 질문 하나의 상위 문단 id를 출력한다")
    ap.add_argument("-k", type=int, default=store_mod.K, help="--search에서 돌려줄 문단 수")
    return ap.parse_args(argv)


def seed_stock_codes() -> list[str]:
    """보일러플레이트 시드 15개사 종목코드(spec 결정 3-1: 처음 적재 대상)."""
    from app.services.graph_service import _COMPANIES

    return [c["symbol"].split(".")[0] for c in _COMPANIES]


def _scrub(text: str, key: str) -> str:
    text = " ".join(str(text).split())
    return text.replace(key, "***") if key else text


def corp_list(client: httpx.Client, key: str, data_dir: Path) -> list[Corp]:
    """상장사 고유번호 목록. data_dir/corpCode.xml이 있으면 다시 받지 않는다."""
    path = data_dir / "corpCode.xml"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".xml.part")
        tmp.write_bytes(dart.fetch_corp_codes(client, key))
        tmp.replace(path)
    return dart.parse_corp_codes(path.read_bytes())


def resolve(corps: list[Corp], wanted: list[str]) -> tuple[list[Corp], list[str]]:
    """종목코드·고유번호를 회사로 바꾼다. 못 찾은 값은 따로 돌려준다. 순서·중복 제거는 입력 순서를 따른다."""
    by = {c.stock_code: c for c in corps} | {c.corp_code: c for c in corps}
    found, missing = {}, []
    for w in wanted:
        c = by.get(w.strip())
        if c is None:
            missing.append(w)
        else:
            found.setdefault(c.corp_code, c)
    return list(found.values()), missing


async def load_one(s: store_mod.PassageStore, client: httpx.Client, key: str, corp: Corp, data_dir: Path) -> Outcome:
    try:
        rep = dart.pick_annual_report(dart.list_annual_reports(client, key, corp.corp_code))
        if rep is None:
            return Outcome(corp, "no_report")
        path = dart.download_document(client, key, rep["rcept_no"], data_dir / "docs", data_dir / "dart_ledger.jsonl")
        ps = passages.build_passages(corp.corp_code, rep["rcept_no"],
                                     path.read_text(encoding="utf-8", errors="ignore"))
        if not ps:
            raise ValueError("no passages")
        res = await s.load(corp, ps)
    except Exception as exc:  # noqa: BLE001 — 회사 하나의 실패가 나머지를 막지 않게
        return Outcome(corp, "failed", error=_scrub(f"{type(exc).__name__}: {exc}", key))
    status = "unchanged" if res.embedded == 0 and res.removed == 0 else "loaded"
    return Outcome(corp, status, rep["rcept_no"], res)


def _line(o: Outcome) -> str:
    head = f"{o.corp.corp_code} {o.corp.stock_code} {o.corp.corp_name}"
    if o.status == "no_report":
        return f"{head}  실패: 사업보고서 없음(2025.12)"
    if o.status == "failed":
        return f"{head}  실패: {o.error}"
    r = o.result
    tag = "변경 없음" if o.status == "unchanged" else "적재"
    return (f"{head}  {o.rcept_no}  문단 {r.passages}  임베딩 {r.embedded}  유지 {r.kept}  삭제 {r.removed}"
            f"  {tag}")


def summary(outcomes: list[Outcome]) -> str:
    ok = [o for o in outcomes if o.result is not None]
    n = {s: sum(o.status == s for o in outcomes) for s in ("loaded", "unchanged")}
    failed = len(outcomes) - len(ok)
    lines = [_line(o) for o in outcomes]
    lines.append(f"합계 {len(outcomes)}개사: 적재 {n['loaded']}, 변경 없음 {n['unchanged']}, 실패 {failed}"
                 f" · 문단 {sum(o.result.passages for o in ok)}")
    return "\n".join(lines)


async def run(args: argparse.Namespace, *, store: store_mod.PassageStore, client: httpx.Client, key: str,
              out: TextIO) -> int:
    if args.search:
        corp_code, question = args.search
        rows = await store.search(corp_code, question, k=args.k)
        print(f"# {corp_code} 상위 {len(rows)}개 문단", file=out)
        for i, r in enumerate(rows, 1):
            print(f"{i} {r['passage_id']} {r['rcept_no']} {r['section']} {r['idx']}", file=out)
        return 0
    try:
        corps, missing = resolve(corp_list(client, key, args.data_dir), args.corp or seed_stock_codes())
    except Exception as exc:  # noqa: BLE001
        print(f"고유번호 목록을 받지 못했다: {_scrub(f'{type(exc).__name__}: {exc}', key)}", file=out)
        return 1
    if missing:
        print(f"찾을 수 없는 회사: {', '.join(missing)}", file=out)
        return 2
    outcomes = []
    for corp in corps:
        outcomes.append(await load_one(store, client, key, corp, args.data_dir))
        print(_line(outcomes[-1]), file=out, flush=True)
    print("", file=out)
    print(summary(outcomes), file=out)
    return 0 if all(o.result is not None for o in outcomes) else 1


def _dart_client() -> httpx.Client:
    return httpx.Client(timeout=60)


def _default_store() -> store_mod.PassageStore:
    return store_mod.default_store()


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # 요청 URL(키 포함)을 로그에 남기지 않는다
    key = "" if args.search else dart.load_dart_key()
    client, s = _dart_client(), _default_store()

    async def go() -> int:
        try:
            return await run(args, store=s, client=client, key=key, out=sys.stdout)
        finally:
            await s.aclose()

    try:
        code = asyncio.run(go())
    finally:
        client.close()
    sys.exit(code)


if __name__ == "__main__":
    main()
