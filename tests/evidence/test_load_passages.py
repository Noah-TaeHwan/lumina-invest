# tests/evidence/test_load_passages.py
"""문단 적재 CLI(P3): DART 목록 → 사업보고서 선택 → 원문 → 문단 → 임베딩 → Qdrant.

DART는 httpx.MockTransport로, Qdrant는 메모리 모드로, 임베딩은 가짜로 대신한다(외부 호출 없음).
실제 dart.* 함수가 돌므로 키가 쿼리에만 실리고 출력·오류 메시지에 남지 않는지 함께 본다.
"""
import asyncio
import io
import json
import zipfile

import httpx
import pytest
from qdrant_client import AsyncQdrantClient

from app.services.evidence import load_passages as lp
from app.services.evidence import store
from app.services.evidence.dart import Corp
from tests.evidence.test_passage_store import FakeEmbed

KEY = "SECRETKEY1234567890"
SAMSUNG = Corp("00126380", "삼성전자", "005930")
HYNIX = Corp("00164779", "SK하이닉스", "000660")
NAVER = Corp("00266961", "NAVER", "035420")

CORP_XML = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?><result>" + "".join(
    f"<list><corp_code>{c.corp_code}</corp_code><corp_name>{c.corp_name}</corp_name>"
    f"<stock_code>{c.stock_code}</stock_code></list>" for c in (SAMSUNG, HYNIX, NAVER)) + "</result>").encode()


def _doc(extra: str = "") -> str:
    return f"""<DOCUMENT><BODY>
<SECTION-1><TITLE>I. 회사의 개요</TITLE>
<SECTION-2><TITLE>1. 회사의 개요</TITLE><P>당사는 반도체를 만듭니다.{extra}</P></SECTION-2>
</SECTION-1>
<SECTION-1><TITLE>II. 사업의 내용</TITLE>
<SECTION-2><TITLE>1. 사업의 개요</TITLE><P>주요 제품은 메모리입니다.</P></SECTION-2>
<SECTION-2><TITLE>2. 주요 제품</TITLE><P>DRAM 매출 비중이 가장 큽니다.</P></SECTION-2>
</SECTION-1></BODY></DOCUMENT>"""


def _zip(name: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, data)
    return buf.getvalue()


def _report(rcept_no: str, nm: str = "사업보고서 (2025.12)", dt: str = "20260312") -> dict:
    return {"report_nm": nm, "rcept_dt": dt, "rcept_no": rcept_no}


class Dart:
    """회사별 list.json 응답과 접수번호별 원문을 정해 두는 가짜 OpenDART."""

    def __init__(self):
        self.lists: dict[str, tuple[int, dict]] = {}
        self.docs: dict[str, tuple[int, bytes]] = {}
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, params = request.url.path, request.url.params
        if path == "/api/corpCode.xml":
            return httpx.Response(200, content=_zip("CORPCODE.xml", CORP_XML))
        if path == "/api/list.json":
            status, body = self.lists.get(params["corp_code"], (200, {"status": "013", "message": "없음"}))
            return httpx.Response(status, json=body)
        if path == "/api/document.xml":
            status, body = self.docs[params["rcept_no"]]
            return httpx.Response(status, content=body)
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def annual(self, corp: Corp, rcept_no: str, xml: str | None = None, **kw) -> None:
        self.lists[corp.corp_code] = (200, {"status": "000", "list": [_report(rcept_no, **kw)]})
        self.docs[rcept_no] = (200, _zip(f"{rcept_no}.xml", (xml or _doc()).encode()))


def _run(argv, dart: Dart, embed=None, qdrant=None, tmp_path=None):
    s = store.PassageStore(qdrant or AsyncQdrantClient(location=":memory:"), embed or FakeEmbed())
    out = io.StringIO()
    args = lp.parse_args([*argv, "--data-dir", str(tmp_path)])
    code = asyncio.run(lp.run(args, store=s, client=dart.client(), key=KEY, out=out))
    return code, out.getvalue(), s


def test_loads_companies_and_prints_summary(tmp_path):
    dart = Dart()
    dart.annual(SAMSUNG, "20260312000123")
    dart.annual(HYNIX, "20260310000777")
    code, out, s = _run(["--corp", "005930", "--corp", HYNIX.corp_code], dart, tmp_path=tmp_path)
    assert code == 0
    assert "삼성전자" in out and "SK하이닉스" in out
    assert "합계 2개사" in out
    counts = {c["corp_code"]: c["passages"] for c in asyncio.run(s.companies())}
    assert counts == {SAMSUNG.corp_code: 3, HYNIX.corp_code: 3}
    assert KEY not in out
    # 키는 쿼리 파라미터로만 나갔다
    assert all(r.url.params.get("crtfc_key") == KEY for r in dart.requests)


def test_summary_line_per_company(tmp_path):
    dart = Dart()
    dart.annual(SAMSUNG, "20260312000123")
    code, out, _ = _run(["--corp", "005930"], dart, tmp_path=tmp_path)
    line = next(x for x in out.splitlines() if SAMSUNG.corp_code in x)
    assert "20260312000123" in line and "문단 3" in line and "임베딩 3" in line


def test_reload_skips_same_report_without_embedding(tmp_path):
    dart = Dart()
    dart.annual(SAMSUNG, "20260312000123")
    embed = FakeEmbed()
    qdrant = AsyncQdrantClient(location=":memory:")
    _run(["--corp", "005930"], dart, embed=embed, qdrant=qdrant, tmp_path=tmp_path)
    n = len(embed.calls)
    code, out, s = _run(["--corp", "005930"], dart, embed=embed, qdrant=qdrant, tmp_path=tmp_path)
    assert code == 0
    assert len(embed.calls) == n
    assert "변경 없음" in out
    assert asyncio.run(s.count(SAMSUNG.corp_code)) == 3


def test_corrected_report_replaces_points_without_duplicates(tmp_path):
    dart = Dart()
    dart.annual(SAMSUNG, "20260312000123")
    qdrant = AsyncQdrantClient(location=":memory:")
    _run(["--corp", "005930"], dart, qdrant=qdrant, tmp_path=tmp_path)
    dart.annual(SAMSUNG, "20260601000999", xml=_doc(" 정정 문장."), nm="[기재정정]사업보고서 (2025.12)", dt="20260601")
    code, out, s = _run(["--corp", "005930"], dart, qdrant=qdrant, tmp_path=tmp_path)
    assert code == 0
    rows = asyncio.run(s.search(SAMSUNG.corp_code, "q"))
    assert len(rows) == 3 and {r["rcept_no"] for r in rows} == {"20260601000999"}
    assert asyncio.run(s.count(SAMSUNG.corp_code)) == 3


def test_dart_http_failure_is_reported_without_key_and_others_continue(tmp_path):
    dart = Dart()
    dart.lists[SAMSUNG.corp_code] = (500, {"error": "boom"})
    dart.annual(HYNIX, "20260310000777")
    code, out, s = _run(["--corp", "005930", "--corp", "000660"], dart, tmp_path=tmp_path)
    assert code == 1
    assert KEY not in out
    line = next(x for x in out.splitlines() if SAMSUNG.corp_code in x)
    assert "실패" in line and "HTTPStatusError" in line
    assert asyncio.run(s.count(HYNIX.corp_code)) == 3


def test_dart_status_error_message_is_scrubbed(tmp_path):
    dart = Dart()
    # DART가 메시지에 키를 되돌려 주는 경우에도 출력에 남지 않는다
    dart.lists[SAMSUNG.corp_code] = (200, {"status": "010", "message": f"등록되지 않은 키 {KEY}"})
    code, out, _ = _run(["--corp", "005930"], dart, tmp_path=tmp_path)
    assert code == 1
    assert KEY not in out and "010" in out


def test_no_annual_report_is_reported(tmp_path):
    dart = Dart()
    dart.lists[SAMSUNG.corp_code] = (200, {"status": "000", "list": [_report("1", nm="반기보고서 (2026.06)")]})
    code, out, s = _run(["--corp", "005930"], dart, tmp_path=tmp_path)
    assert code == 1
    assert "사업보고서 없음" in next(x for x in out.splitlines() if SAMSUNG.corp_code in x)
    assert asyncio.run(s.exists()) is False


def test_report_without_sections_is_reported(tmp_path):
    dart = Dart()
    dart.annual(SAMSUNG, "20260312000123", xml="<DOCUMENT><BODY><P>빈 보고서</P></BODY></DOCUMENT>")
    code, out, s = _run(["--corp", "005930"], dart, tmp_path=tmp_path)
    assert code == 1
    line = next(x for x in out.splitlines() if SAMSUNG.corp_code in x)
    assert "실패" in line and "missing sections" in line
    assert asyncio.run(s.exists()) is False


def test_bad_document_zip_is_reported(tmp_path):
    dart = Dart()
    dart.annual(SAMSUNG, "20260312000123")
    dart.docs["20260312000123"] = (200, json.dumps({"status": "014", "message": "파일 없음"}).encode())
    code, out, _ = _run(["--corp", "005930"], dart, tmp_path=tmp_path)
    assert code == 1
    assert "실패" in next(x for x in out.splitlines() if SAMSUNG.corp_code in x)
    assert KEY not in out


def test_embed_failure_is_reported_without_key(tmp_path):
    dart = Dart()
    dart.annual(SAMSUNG, "20260312000123")

    async def down(text):
        raise httpx.ConnectError(f"cannot reach ollama {KEY}")

    code, out, s = _run(["--corp", "005930"], dart, embed=down, tmp_path=tmp_path)
    assert code == 1
    assert KEY not in out and "ConnectError" in out


def test_unknown_corp_fails_before_any_load(tmp_path):
    dart = Dart()
    code, out, s = _run(["--corp", "999999"], dart, tmp_path=tmp_path)
    assert code == 2
    assert "999999" in out
    assert not [r for r in dart.requests if r.url.path == "/api/list.json"]


def test_default_corps_are_seed_companies(monkeypatch):
    import sys
    import types

    if "neo4j" not in sys.modules:  # graph_service가 import하는 드라이버(테스트 명령에는 없다)
        stub = types.ModuleType("neo4j")
        stub.AsyncGraphDatabase = stub.AsyncDriver = object
        monkeypatch.setitem(sys.modules, "neo4j", stub)
    seeds = lp.seed_stock_codes()
    assert len(seeds) == 15 and "005930" in seeds and "096770" in seeds
    assert all(len(s) == 6 and s.isdigit() for s in seeds)


def test_corp_code_list_is_cached_in_data_dir(tmp_path):
    dart = Dart()
    dart.annual(SAMSUNG, "20260312000123")
    _run(["--corp", "005930"], dart, tmp_path=tmp_path)
    _run(["--corp", "005930"], dart, tmp_path=tmp_path)
    assert sum(r.url.path == "/api/corpCode.xml" for r in dart.requests) == 1
    assert (tmp_path / "corpCode.xml").exists()


def test_search_mode_prints_top_passage_ids(tmp_path):
    dart = Dart()
    dart.annual(SAMSUNG, "20260312000123")
    qdrant = AsyncQdrantClient(location=":memory:")
    _run(["--corp", "005930"], dart, qdrant=qdrant, tmp_path=tmp_path)
    dart.requests.clear()
    code, out, _ = _run(["--search", SAMSUNG.corp_code, "주요 제품은?"], dart, qdrant=qdrant, tmp_path=tmp_path)
    assert code == 0
    ids = [x.split()[1] for x in out.splitlines() if x.strip() and x.split()[0].isdigit()]
    assert sorted(ids) == sorted(f"{SAMSUNG.corp_code}-{s}" for s in ("I1-0000", "II-0000", "II-0001"))
    assert dart.requests == []  # 검색 모드는 DART를 부르지 않는다


def test_main_reads_key_from_dart_loader_and_never_prints_it(monkeypatch, tmp_path, capsys):
    """main은 키를 인자로 받지 않는다(셸 기록·ps에 남지 않게). load_dart_key 값은 출력에 없다."""
    monkeypatch.setattr(lp.dart, "load_dart_key", lambda: KEY)
    dart = Dart()
    dart.lists[SAMSUNG.corp_code] = (500, {})
    monkeypatch.setattr(lp, "_dart_client", dart.client)
    monkeypatch.setattr(lp, "_default_store", lambda: store.PassageStore(AsyncQdrantClient(location=":memory:"),
                                                                         FakeEmbed()))
    with pytest.raises(SystemExit) as exc:
        lp.main(["--corp", "005930", "--data-dir", str(tmp_path)])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert KEY not in captured.out + captured.err
    with pytest.raises(SystemExit):
        lp.parse_args(["--key", KEY])
