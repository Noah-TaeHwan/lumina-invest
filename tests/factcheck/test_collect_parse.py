# tests/factcheck/test_collect_parse.py
"""공시 수집(목록 전체 순회·정기보고서/잠정실적 선별·원문·XBRL·회사명 사전)과 문단 분해.

고정 자료는 실제 공개 공시다: 삼성전자 2026년 2분기 잠정실적 정정 공시(xforms HTML), 2026년 반기보고서
II·III절 발췌. OpenDART는 httpx.MockTransport 가짜로 대신한다(외부 호출 없음).
"""
import io
import json
import zipfile
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.services.factcheck import collect, corp_names, parse

FIX = Path(__file__).parent / "fixtures"
SAMSUNG, HYNIX = "00126380", "00164779"
PRELIM_RCEPT = "20260730800123"


def _fix(name: str) -> str:
    return (FIX / name).read_bytes().decode("utf-8")


# ── 잠정실적(xforms HTML) ──────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def prelim():
    return parse.parse_prelim(_fix("prelim_samsung_2026Q2_correction.xml"))


def test_prelim_period_unit_and_correction_meta(prelim):
    assert prelim.period == "2026Q2"
    assert (prelim.period_start, prelim.period_end) == ("2026-04-01", "2026-06-30")
    assert prelim.unit == "조원"
    assert prelim.is_correction is True
    assert prelim.original_date == "20260707"  # 정정 대상 원 공시 제출일


def test_prelim_figures_use_corrected_values(prelim):
    fig = {(f.account, f.basis): f for f in prelim.figures}
    rev = fig[("매출액", "당해실적")]
    assert (rev.current, rev.prior_q, rev.qoq_pct, rev.prior_y, rev.yoy_pct) == (
        Decimal("171.50"), Decimal("133.87"), Decimal("28.11"), Decimal("74.57"), Decimal("130.00"))
    op_cum = fig[("영업이익", "누계실적")]
    assert (op_cum.current, op_cum.prior_q, op_cum.prior_y, op_cum.yoy_pct) == (
        Decimal("146.73"), None, Decimal("11.36"), Decimal("1191.45"))
    # 값이 전부 '-'인 계정(법인세비용차감전·당기순이익 등)은 남기지 않는다
    assert {f.account for f in prelim.figures} == {"매출액", "영업이익"}


def test_prelim_amount_in_won_is_exact(prelim):
    assert parse.to_won(Decimal("171.50"), "조원") == 171_500_000_000_000
    assert parse.to_won(Decimal("304.87"), "조원") == 304_870_000_000_000
    assert parse.to_won(Decimal("1.5"), "억원") == 150_000_000
    assert parse.to_won(Decimal("2"), "백만원") == 2_000_000


def test_prelim_corrections_before_after(prelim):
    corr = {(c.group, c.item, c.basis): (c.before, c.after) for c in prelim.corrections}
    assert corr[("당기실적", "매출액", "당해실적")] == (Decimal("171.00"), Decimal("171.50"))
    assert corr[("당기실적", "영업이익", "누계실적")] == (Decimal("146.63"), Decimal("146.73"))
    assert corr[("전기대비증감율(%)", "영업이익", "당해실적")] == (Decimal("56.21"), Decimal("56.37"))
    assert corr[("전년동기대비증감율(%)", "영업이익", "당해실적")] == (Decimal("1810.26"), Decimal("1813.83"))
    assert len(corr) == 10  # 당기실적 4 + 전기대비 2 + 전년동기대비 4(텍스트 항목 '기타 투자판단'은 제외)


def test_prelim_passages_keep_stale_values_out_of_main_section(prelim):
    ps = parse.prelim_passages(SAMSUNG, PRELIM_RCEPT, prelim)
    main = [p for p in ps if p.section == "PRELIM"]
    corr = [p for p in ps if p.section == "CORR"]
    assert main and corr
    body = " ".join(p.text for p in main)
    assert "171.50" in body and "단위 조원" in body and "2026-04-01~2026-06-30" in body
    assert "171.00" not in body and "1,810.26" not in body and "1810.26" not in body
    assert "171.00" in corr[0].text and "171.50" in corr[0].text and "정정" in corr[0].text
    assert ps[0].id == f"{SAMSUNG}-{PRELIM_RCEPT}-PRELIM-0"
    assert [p.idx for p in main] == list(range(len(main)))


def test_prelim_original_filing_is_not_correction():
    html = _fix("prelim_samsung_2026Q2_correction.xml")
    start = html.index('<div id="LIB_LC000"')  # 정정신고 블록을 통째로 잘라 원 공시처럼 만든다
    end = html.index('<div class="xforms_title">')
    orig = parse.parse_prelim(html[:start] + html[end:])
    assert orig.is_correction is False and orig.corrections == [] and orig.original_date is None
    assert orig.period == "2026Q2"


def test_decode_falls_back_to_cp949():
    assert parse.decode("매출액".encode("cp949")) == "매출액"
    assert parse.decode("매출액".encode("utf-8")) == "매출액"


# ── 정기보고서 I·II·III절 ──────────────────────────────────────────────────────

def test_half_section_ii_passages_with_table_units():
    ps = parse.regular_passages(SAMSUNG, "20260814003699", _fix("half_samsung_2026H1_sectionII_excerpt.xml"),
                                require=("II",))
    assert {p.section for p in ps} == {"II"}  # 발췌 앞쪽의 I절 꼬리(시작 태그 없음)는 버린다
    assert len(ps) == 22
    assert ps[0].id == f"{SAMSUNG}-20260814003699-II-0"
    assert [p.idx for p in ps] == list(range(len(ps)))
    assert any("단위 억원, %" in p.text for p in ps)
    assert any("309개의 종속기업" in p.text for p in ps)


def test_half_section_iii_passages_with_table_units():
    ps = parse.regular_passages(SAMSUNG, "20260814003699", _fix("half_samsung_2026H1_sectionIII_excerpt.xml"),
                                require=("III",))
    assert {p.section for p in ps} == {"III"}
    assert len(ps) == 20
    assert any("단위 백만원" in p.text and "379,711,982" in p.text for p in ps)  # 요약재무정보 표
    # 재무제표 본표(TABLE-GROUP 안, 머리 표에 단위)도 제목·단위가 행마다 붙는다
    bs = next(p for p in ps if "자산총계 | 제 58 기 반기말: 759,480,516" in p.text)
    assert "[2-1. 연결 재무상태표 표, 연결 재무상태표, 단위 백만원] 자산총계" in bs.text


def test_missing_required_section_raises():
    with pytest.raises(ValueError, match="III"):
        parse.regular_passages(SAMSUNG, "1", _fix("half_samsung_2026H1_sectionII_excerpt.xml"))


def _sec1(title, body):
    return f'<SECTION-1><TITLE ATOC="Y">{title}</TITLE>{body}</SECTION-1>'


def _sec2(title, body):
    return f'<SECTION-2><TITLE ATOC="Y">{title}</TITLE>{body}</SECTION-2>'


def test_sections_whole_i_ii_iii_without_notes_and_tolerate_unclosed_tail():
    xml = ("<DOCUMENT>"
           + _sec1("Ⅰ. 회사의 개요", _sec2("1. 회사의 개요", "<P>개요 문단</P>") + _sec2("2. 회사의 연혁", "<P>연혁 문단</P>"))
           + _sec1("II. 사업의 내용", "<P>사업 문단</P>")
           + _sec1("III. 재무에 관한 사항",
                   _sec2("1. 요약재무정보", "<P>요약 문단</P>")
                   + _sec2("3. 연결재무제표 주석", "<P>주석 문단</P>")
                   + _sec2("4. 재무제표", "<P>별도 문단</P>"))
           + _sec1("IV. 이사의 경영진단 및 분석의견", "<P>경영진단 문단</P>")
           + '<SECTION-1><TITLE>V. 회계감사인</TITLE><P>끊긴 문단</P>')  # 닫는 태그 없이 끝남
    secs = parse.split_sections(xml)
    assert set(secs) == {"I", "II", "III"}
    assert "연혁 문단" in secs["I"]  # I절 전체(1. 회사의 개요만이 아님)
    assert "요약 문단" in secs["III"] and "별도 문단" in secs["III"]
    assert "주석 문단" not in secs["III"]
    assert "경영진단" not in secs["III"]
    ps = parse.regular_passages(SAMSUNG, "R1", xml)
    assert [p.section for p in ps] == ["I", "I", "II", "III", "III"]  # 제목 경계마다 문단을 끊는다
    assert [p.idx for p in ps] == [0, 1, 0, 0, 1]


def test_table_group_and_statement_cover_table_get_unit():
    xml = _sec1("III. 재무에 관한 사항", _sec2("2. 연결재무제표", (
        '<TABLE-GROUP ACLASS="{XBRL}BS_C"><TITLE ATOC="Y">2-1. 연결 재무상태표</TITLE>'
        "<TABLE><TBODY><TR><TE>연결 재무상태표</TE></TR><TR><TE>제 58 기 반기말 2026.06.30 현재</TE></TR>"
        "<TR><TE>(단위 : 백만원)</TE></TR></TBODY></TABLE>"
        "<TABLE><THEAD><TR><TH> </TH><TH>제 58 기 반기말</TH></TR></THEAD>"
        "<TBODY><TR><TE>자산총계</TE><TE>759,480,516</TE></TR></TBODY></TABLE></TABLE-GROUP>")))
    ps = parse.regular_passages(SAMSUNG, "R1", xml, require=("III",))
    assert [p.text for p in ps] == [
        "[2-1. 연결 재무상태표 표, 연결 재무상태표, 단위 백만원] 자산총계 | 제 58 기 반기말: 759,480,516"]


# ── 수집: 목록 전체 순회·선별 ─────────────────────────────────────────────────

def _item(rcept_no, report_nm, corp=SAMSUNG):
    return {"corp_code": corp, "corp_name": "삼성전자", "stock_code": "005930", "report_nm": report_nm,
            "rcept_no": rcept_no, "flr_nm": "삼성전자", "rcept_dt": rcept_no[:8], "rm": ""}


def test_list_filings_walks_every_page():
    pages = {1: [_item("20250101000001", "a")], 2: [_item("20250201000001", "b")],
             3: [_item("20250301000001", "c")]}
    seen = []

    def handler(request):
        seen.append(request)
        n = int(request.url.params["page_no"])
        return httpx.Response(200, json={"status": "000", "page_no": n, "total_page": 3, "list": pages[n]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    got = collect.list_filings(client, "KEY", SAMSUNG, "20231006", "20261006", "A")
    assert [g["report_nm"] for g in got] == ["a", "b", "c"]
    assert [r.url.params["page_no"] for r in seen] == ["1", "2", "3"]
    assert {(r.url.params["pblntf_ty"], r.url.params["page_count"], r.url.params["bgn_de"]) for r in seen} == {
        ("A", "100", "20231006")}


def test_list_filings_no_data():
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"status": "013", "message": "조회된 데이타가 없습니다."})))
    assert collect.list_filings(client, "KEY", SAMSUNG, "20231006", "20261006", "I") == []


@pytest.mark.parametrize("name,expected", [
    ("사업보고서 (2025.12)", ("annual", "2025")),
    ("[기재정정]사업보고서 (2024.12)", ("annual", "2024")),
    ("반기보고서 (2026.06)", ("half", "2026H1")),
    ("분기보고서 (2025.03)", ("quarter", "2025Q1")),
    ("분기보고서 (2025.09)", ("quarter", "2025Q3")),
    ("주요사항보고서(자기주식취득결정)", None),
    ("임원ㆍ주요주주특정증권등소유상황보고서", None),
])
def test_classify_regular(name, expected):
    assert collect.classify_regular(name) == expected


def test_pick_regular_latest_per_period_and_marks_correction():
    items = [_item("20250311000100", "사업보고서 (2024.12)"),
             _item("20250402000200", "[기재정정]사업보고서 (2024.12)"),
             _item("20250515000300", "분기보고서 (2025.03)"),
             _item("20250814000400", "반기보고서 (2025.06)"),
             _item("20250820000500", "[첨부정정]반기보고서 (2025.06)"),
             _item("20250901000600", "주요사항보고서(자기주식취득결정)")]
    got = {d["period"]: d for d in collect.pick_regular(items)}
    assert set(got) == {"2024", "2025Q1", "2025H1"}
    assert (got["2024"]["rcept_no"], got["2024"]["is_correction"]) == ("20250402000200", True)
    assert (got["2025H1"]["rcept_no"], got["2025H1"]["report_type"]) == ("20250814000400", "half")
    assert got["2025Q1"] == {"corp_code": SAMSUNG, "corp_name": "삼성전자", "rcept_no": "20250515000300",
                            "report_type": "quarter", "report_nm": "분기보고서 (2025.03)",
                            "period": "2025Q1", "rcept_dt": "20250515", "is_correction": False,
                            "superseded": False}


def test_pick_prelim_keeps_originals_and_corrections():
    items = [_item("20260707800001", "연결재무제표기준영업(잠정)실적(공정공시)"),
             _item("20260730800123", "[기재정정]연결재무제표기준영업(잠정)실적(공정공시)"),
             _item("20260710800002", "주식등의대량보유상황보고서(일반)"),
             _item("20260801800003", "기업설명회(IR)개최(안내공시)")]
    got = collect.pick_prelim(items)
    assert [(d["rcept_no"], d["report_type"], d["is_correction"]) for d in got] == [
        ("20260707800001", "preliminary", False), ("20260730800123", "preliminary", True)]


def test_mark_superseded_by_period():
    docs = [{"corp_code": SAMSUNG, "report_type": "preliminary", "period": "2026Q2", "rcept_dt": "20260707",
             "rcept_no": "20260707800001"},
            {"corp_code": SAMSUNG, "report_type": "preliminary", "period": "2026Q2", "rcept_dt": "20260730",
             "rcept_no": "20260730800123"},
            {"corp_code": SAMSUNG, "report_type": "preliminary", "period": "2026Q1", "rcept_dt": "20260407",
             "rcept_no": "20260407800009"}]
    collect.mark_superseded(docs)
    assert [d["superseded"] for d in docs] == [True, False, False]


# ── 수집 전체(가짜 OpenDART) ──────────────────────────────────────────────────

def _zip(name: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, data)
    return buf.getvalue()


def test_collect_end_to_end_with_fake_dart(tmp_path):
    regular = [_item("20260814003699", "반기보고서 (2026.06)")]
    prelim = [_item(PRELIM_RCEPT, "[기재정정]연결재무제표기준영업(잠정)실적(공정공시)")]
    docs = {"20260814003699": (FIX / "half_samsung_2026H1_sectionII_excerpt.xml").read_bytes(),
            PRELIM_RCEPT: (FIX / "prelim_samsung_2026Q2_correction.xml").read_bytes()}
    xbrl_fix = {"CFS": json.loads((FIX / "xbrl_samsung_2026_11012_CFS.json").read_text()),
                "OFS": {"status": "013", "message": "조회된 데이타가 없습니다."}}
    calls = []

    def handler(request):
        p = request.url.params
        path = request.url.path.rsplit("/", 1)[-1]
        calls.append(path)
        if path == "list.json":
            body = (regular if p["pblntf_ty"] == "A" else prelim) if p["corp_code"] == SAMSUNG else []
            return httpx.Response(200, json={"status": "000" if body else "013", "page_no": 1, "total_page": 1,
                                             "list": body})
        if path == "document.xml":
            return httpx.Response(200, content=_zip(f"{p['rcept_no']}.xml", docs[p["rcept_no"]]))
        if path == "fnlttSinglAcntAll.json":
            return httpx.Response(200, json=xbrl_fix[p["fs_div"]])
        raise AssertionError(path)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    summary = collect.collect(client, "KEY", [SAMSUNG, HYNIX], "20231006", "20261006", tmp_path)

    manifest = json.loads((tmp_path / "documents.json").read_text())
    assert [(d["rcept_no"], d["report_type"], d["period"]) for d in manifest] == [
        ("20260814003699", "half", "2026H1"), (PRELIM_RCEPT, "preliminary", "2026Q2")]
    assert manifest[1]["is_correction"] is True and manifest[1]["superseded"] is False
    assert (tmp_path / "docs" / f"{PRELIM_RCEPT}.xml").exists()
    facts = json.loads((tmp_path / "xbrl_facts.json").read_text())
    assert facts and {f["fs_div"] for f in facts} == {"CFS"}
    assert {f["rcept_dt"] for f in facts} == {"20260814"}
    assert summary == {"documents": 2, "regular": 1, "preliminary": 1, "xbrl_rows": len(facts)}
    assert calls.count("fnlttSinglAcntAll.json") == 2  # 정기보고서 1건 × CFS·OFS


def test_collect_main_requires_key_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(collect, "load_dart_key", lambda: (_ for _ in ()).throw(FileNotFoundError("no key")))
    with pytest.raises(FileNotFoundError):
        collect.main(["--out", str(tmp_path)])


# ── 상장사명 사전 ─────────────────────────────────────────────────────────────

CORP_XML = """<?xml version="1.0" encoding="UTF-8"?><result>
<list><corp_code>00126380</corp_code><corp_name>삼성전자</corp_name><stock_code>005930</stock_code></list>
<list><corp_code>00164779</corp_code><corp_name>SK하이닉스</corp_name><stock_code>000660</stock_code></list>
<list><corp_code>00181712</corp_code><corp_name>SK</corp_name><stock_code>034730</stock_code></list>
<list><corp_code>00999999</corp_code><corp_name>비상장 주식회사</corp_name><stock_code> </stock_code></list>
</result>""".encode()


@pytest.mark.parametrize("raw,norm", [
    ("SK하이닉스", "SK하이닉스"), ("SK 하이닉스", "SK하이닉스"), ("(주)삼성전자", "삼성전자"), ("㈜삼성전자", "삼성전자"),
    ("삼성전자 주식회사", "삼성전자"), ("ｓｋ하이닉스", "SK하이닉스"), ("Ｓ Ｋ", "SK"),
])
def test_normalize_name(raw, norm):
    assert corp_names.normalize(raw) == norm


def test_corp_name_dictionary_exact_keys(tmp_path):
    entries = corp_names.build(CORP_XML)
    assert [e["corp_code"] for e in entries] == ["00126380", "00164779", "00181712"]  # 상장사만
    path = tmp_path / "corp_names.json"
    corp_names.save(path, entries)
    index = corp_names.index(corp_names.load(path))
    assert index["SK하이닉스"] == ["00164779"]
    assert index["SK"] == ["00181712"]  # 'SK'가 'SK하이닉스'로 번지지 않는다(부분 일치 없음)
    assert corp_names.lookup(index, "SK 하이닉스") == ["00164779"]
    assert corp_names.lookup(index, "하이닉스") == []
