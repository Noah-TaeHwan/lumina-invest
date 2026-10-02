# tests/evidence/test_dart.py
import io
import json
import zipfile

import httpx
import pytest

from app.services.evidence import dart

CORP_XML = """<?xml version="1.0" encoding="UTF-8"?><result>
<list><corp_code>00126380</corp_code><corp_name>삼성전자</corp_name><stock_code>005930</stock_code></list>
<list><corp_code>00999999</corp_code><corp_name>비상장</corp_name><stock_code> </stock_code></list>
</result>""".encode()


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _client(routes, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        status, content = routes[request.url.path]
        if isinstance(content, (dict, list)):
            return httpx.Response(status, json=content)
        return httpx.Response(status, content=content)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_parse_corp_codes_keeps_listed_only():
    assert dart.parse_corp_codes(CORP_XML) == [dart.Corp("00126380", "삼성전자", "005930")]


def test_fetch_corp_codes_unzips():
    c = _client({"/api/corpCode.xml": (200, _zip({"CORPCODE.xml": CORP_XML}))})
    assert dart.fetch_corp_codes(c, "K") == CORP_XML


def test_pick_annual_report_prefers_latest_correction_before_cutoff():
    items = [
        {"report_nm": "사업보고서 (2025.12)", "rcept_dt": "20260310", "rcept_no": "1"},
        {"report_nm": "[기재정정]사업보고서 (2025.12)", "rcept_dt": "20260601", "rcept_no": "2"},
        {"report_nm": "[기재정정]사업보고서 (2025.12)", "rcept_dt": "20261005", "rcept_no": "3"},
        {"report_nm": "반기보고서 (2026.06)", "rcept_dt": "20260814", "rcept_no": "4"},
    ]
    assert dart.pick_annual_report(items)["rcept_no"] == "2"
    assert dart.pick_annual_report(items[3:]) is None


def test_list_annual_reports_handles_no_data_and_errors():
    ok = _client({"/api/list.json": (200, {"status": "013", "message": "조회된 데이타가 없습니다."})})
    assert dart.list_annual_reports(ok, "K", "1") == []
    bad = _client({"/api/list.json": (200, {"status": "020", "message": "요청 제한"})})
    with pytest.raises(RuntimeError, match="020"):
        dart.list_annual_reports(bad, "K", "1")


def test_download_writes_largest_xml_and_ledger(tmp_path):
    doc = _zip({"a.xml": "<small/>", "b.xml": "<DOCUMENT>" + "x" * 100 + "</DOCUMENT>"})
    c = _client({"/api/document.xml": (200, doc)})
    p = dart.download_document(c, "K", "2026", tmp_path / "docs", tmp_path / "ledger.jsonl")
    assert p.read_text().startswith("<DOCUMENT>")
    rec = json.loads((tmp_path / "ledger.jsonl").read_text())
    assert rec["rcept_no"] == "2026" and len(rec["sha256"]) == 64
    again = _client({})
    assert dart.download_document(again, "K", "2026", tmp_path / "docs", tmp_path / "ledger.jsonl") == p


def test_download_rejects_non_zip_without_key(tmp_path):
    c = _client({"/api/document.xml": (200, {"status": "010", "message": "등록되지 않은 키"})})
    with pytest.raises(ValueError) as e:
        dart.download_document(c, "SECRET-KEY", "1", tmp_path, tmp_path / "l.jsonl")
    assert "SECRET-KEY" not in str(e.value)


def test_http_error_message_hides_key():
    c = _client({"/api/company.json": (500, {"x": 1})})
    with pytest.raises(RuntimeError) as e:
        dart.company_info(c, "SECRET-KEY", "1")
    assert "SECRET-KEY" not in str(e.value)
