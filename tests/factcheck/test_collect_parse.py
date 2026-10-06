# tests/factcheck/test_collect_parse.py
"""공시 수집(목록 전체 순회·정기보고서/잠정실적 선별·원문·XBRL·회사명 사전)과 문단 분해.

고정 자료는 실제 공개 공시다: 삼성전자 2026년 2분기 잠정실적 정정 공시(xforms HTML), 2026년 반기보고서
II·III절 발췌. OpenDART는 httpx.MockTransport 가짜로 대신한다(외부 호출 없음).
"""
import io
import re
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
    assert "171.50조원" in body and "28.11%" in body and "2026-04-01~2026-06-30" in body
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
    assert len(ps) == 26  # 출력 확인 뒤 고정한 값(머리·기간 접두로 문단이 늘었다)
    assert ps[0].id == f"{SAMSUNG}-20260814003699-II-0"
    assert [p.idx for p in ps] == list(range(len(ps)))
    assert any("단위 억원, %" in p.text for p in ps)
    assert any("309개의 종속기업" in p.text for p in ps)


def test_half_section_iii_passages_with_table_units():
    ps = parse.regular_passages(SAMSUNG, "20260814003699", _fix("half_samsung_2026H1_sectionIII_excerpt.xml"),
                                require=("III",))
    assert {p.section for p in ps} == {"III"}
    assert len(ps) == 27  # 출력 확인 뒤 고정한 값
    assert any("단위 백만원" in p.text and "379,711,982" in p.text for p in ps)  # 요약재무정보 표
    # 재무제표 본표(TABLE-GROUP 안, 머리 표에 단위)도 제목·단위가 행마다 붙는다
    bs = next(p for p in ps if "자산총계 | 제 58 기 반기말(2026.06.30): 759,480,516" in p.text)
    assert "[2-1. 연결 재무상태표 표, 연결 재무상태표, 단위 백만원] 자산총계" in bs.text
    assert "제 58 기 반기말(2026.06.30): 759,480,516" in bs.text  # 머리 표의 기간을 열 이름에 남긴다


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
        "[2-1. 연결 재무상태표 표, 연결 재무상태표, 단위 백만원] 자산총계 | 제 58 기 반기말(2026.06.30): 759,480,516"]


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
    assert {f["rcept_dt"] for f in facts if f["report_type"] == "periodic"} == {"20260814"}
    assert {f["rcept_dt"] for f in facts if f["report_type"] == "preliminary"} == {"20260730"}
    assert summary == {"documents": 2, "regular": 1, "preliminary": 1, "xbrl_rows": len(facts),
                       "xbrl_after_end": 0, "parse_errors": {}, "correction_parse_errors": []}
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


# ── PR #55 검수 반영 ──────────────────────────────────────────────────────────

def _rows_of(xml: str, sec: str) -> list[str]:
    return [t for k, t in parse.table_blocks(parse.split_sections(xml)[sec]) if k == "row"]


def test_summary_rows_keep_consolidated_or_separate_and_period():
    """요약재무정보 행마다 '가. 요약연결'/'나. 요약별도' 하위 머리와 표 안 기간 행이 붙는다."""
    rows = [r for r in _rows_of(_fix("half_samsung_2026H1_sectionIII_excerpt.xml"), "III") if "요약재무정보" in r]
    assert rows
    for r in rows:
        assert ("요약연결재무정보" in r) != ("요약별도재무정보" in r), r
        assert "2026년" in r, r  # 제58기(2026년 6월말) / 제58기(2026년 1월~6월)
    sep = [r for r in rows if "자산총계" in r and "요약별도재무정보" in r]
    assert sep and "[1. 요약재무정보 > 나. 요약별도재무정보 표, 단위 백만원]" in sep[0]
    assert "제58기(2026년 6월말): 465,862,647" in sep[0]
    rev = next(r for r in rows if "매출액" in r and "요약연결재무정보" in r)
    assert "제58기(2026년 1월~6월): 305,372,914" in rev


def test_income_statement_two_row_header_splits_three_months_and_cumulative():
    """두 줄 머리(제58기 반기 COLSPAN=2 → 3개월·누적)를 펼치고 실제 기간을 붙인다."""
    rows = _rows_of(_fix("half_samsung_2026H1_IS_excerpt.xml"), "III")
    rev = next(r for r in rows if "| " in r and r.split("] ", 1)[1].startswith("매출액"))
    assert "제 58 기 반기 3개월(2026.04.01~2026.06.30): 171,499,470" in rev
    assert "제 58 기 반기 누적(2026.01.01~2026.06.30): 305,372,914" in rev
    assert "제 57 기 반기 3개월(2025.04.01~2025.06.30): 74,566,317" in rev
    assert "[2-2. 연결 손익계산서 표, 연결 손익계산서, 단위 백만원]" in rev


def test_body_rowspan_is_carried_down():
    xml = _sec1("II. 사업의 내용", _sec2("2. 주요 제품", (
        "<TABLE><TR><TD>(단위 : 억원)</TD></TR></TABLE>"
        "<TABLE><THEAD><TR><TH>부문</TH><TH>품목</TH><TH>매출액</TH></TR></THEAD><TBODY>"
        '<TR><TD ROWSPAN="2">DX</TD><TD>TV</TD><TD>100</TD></TR><TR><TD>폰</TD><TD>200</TD></TR>'
        "</TBODY></TABLE>")))
    assert _rows_of(xml, "II") == ["[2. 주요 제품 표, 단위 억원] 부문: DX | 품목: TV | 매출액: 100",
                                   "[2. 주요 제품 표, 단위 억원] 부문: DX | 품목: 폰 | 매출액: 200"]


def test_prelim_passage_units_make_number_check_correct(prelim):
    from app.services.evidence.numbers import number_check

    ps = parse.prelim_passages(SAMSUNG, PRELIM_RCEPT, prelim)
    rev = next(p.text for p in ps if p.section == "PRELIM" and "매출액" in p.text)
    assert number_check("매출액은 171.5조원이다", rev)
    assert number_check("매출액은 전년 동기 대비 130% 증가했다", rev)
    assert number_check("매출액은 전기 대비 28.1% 늘었다", rev)
    assert not number_check("매출액이 171.50% 증가했다", rev)
    assert not number_check("매출액은 171.0조원이다", rev)
    assert not number_check("매출액은 전년 동기 대비 130조원 늘었다", rev)


def test_prelim_facts_contract_rows(prelim):
    rows = parse.prelim_facts(SAMSUNG, PRELIM_RCEPT, prelim, rcept_dt="20260730", is_correction=True)
    by = {(r["account_id"], r["cumulative"]): r for r in rows}
    q = by[("ifrs-full_Revenue", False)]
    assert (q["period"], q["period_start"], q["period_end"], q["amount"]) == (
        "2026Q2", "2026-04-01", "2026-06-30", 171_500_000_000_000)
    assert (q["report_type"], q["fs_div"], q["rcept_no"], q["rcept_dt"], q["is_correction"], q["value_kind"]) == (
        "preliminary", "CFS", PRELIM_RCEPT, "20260730", True, "duration")
    assert q["rounding_unit"] == 10**10  # 조원 소수 둘째 자리
    cum = by[("dart_OperatingIncomeLoss", True)]
    assert (cum["period"], cum["period_start"], cum["amount"]) == ("2026H1", "2026-01-01", 146_730_000_000_000)
    assert set(by) == {("ifrs-full_Revenue", False), ("ifrs-full_Revenue", True),
                       ("dart_OperatingIncomeLoss", False), ("dart_OperatingIncomeLoss", True)}


def test_mark_superseded_separates_consolidated_and_separate():
    docs = [{"corp_code": SAMSUNG, "report_type": "preliminary", "period": "2026Q2", "fs_div": "CFS",
             "rcept_dt": "20260707", "rcept_no": "1"},
            {"corp_code": SAMSUNG, "report_type": "preliminary", "period": "2026Q2", "fs_div": "OFS",
             "rcept_dt": "20260708", "rcept_no": "2"}]
    collect.mark_superseded(docs)
    assert [d["superseded"] for d in docs] == [False, False]


def test_pick_prelim_sets_fs_div():
    got = collect.pick_prelim([_item("20260707800001", "연결재무제표기준영업(잠정)실적(공정공시)"),
                               _item("20260707800002", "영업(잠정)실적(공정공시)")])
    assert [d["fs_div"] for d in got] == ["CFS", "OFS"]


def _fake_dart(regular, prelim, docs, xbrl_by_fs):
    def handler(request):
        p = request.url.params
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "list.json":
            body = (regular if p["pblntf_ty"] == "A" else prelim) if p["corp_code"] == SAMSUNG else []
            return httpx.Response(200, json={"status": "000" if body else "013", "page_no": 1, "total_page": 1,
                                             "list": body})
        if path == "document.xml":
            return httpx.Response(200, content=_zip(f"{p['rcept_no']}.xml", docs[p["rcept_no"]]))
        if path == "fnlttSinglAcntAll.json":
            return httpx.Response(200, json=xbrl_by_fs[p["fs_div"]])
        raise AssertionError(path)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_collect_writes_prelim_facts_and_marks_periodic(tmp_path):
    client = _fake_dart(
        [_item("20260814003699", "반기보고서 (2026.06)")],
        [_item(PRELIM_RCEPT, "[기재정정]연결재무제표기준영업(잠정)실적(공정공시)")],
        {"20260814003699": (FIX / "half_samsung_2026H1_sectionII_excerpt.xml").read_bytes(),
         PRELIM_RCEPT: (FIX / "prelim_samsung_2026Q2_correction.xml").read_bytes()},
        {"CFS": json.loads((FIX / "xbrl_samsung_2026_11012_CFS.json").read_text()),
         "OFS": {"status": "013", "message": "없음"}})
    collect.collect(client, "KEY", [SAMSUNG], "20231006", "20261006", tmp_path)
    facts = json.loads((tmp_path / "xbrl_facts.json").read_text())
    pre = [f for f in facts if f["report_type"] == "preliminary"]
    assert {f["rcept_no"] for f in pre} == {PRELIM_RCEPT} and all(f["superseded"] is False for f in pre)
    assert {f["report_type"] for f in facts} == {"periodic", "preliminary"}
    manifest = json.loads((tmp_path / "documents.json").read_text())
    assert manifest[1]["fs_div"] == "CFS" and manifest[0].get("fs_div") is None


def test_collect_drops_xbrl_rows_filed_after_end(tmp_path):
    late = json.loads((FIX / "xbrl_samsung_2026_11012_CFS.json").read_text())
    for r in late["list"]:
        r["rcept_no"] = "20261020000001"  # --end(20261006) 뒤에 낸 정정본
    client = _fake_dart([_item("20260814003699", "반기보고서 (2026.06)")], [],
                        {"20260814003699": (FIX / "half_samsung_2026H1_sectionII_excerpt.xml").read_bytes()},
                        {"CFS": late, "OFS": {"status": "013", "message": "없음"}})
    summary = collect.collect(client, "KEY", [SAMSUNG], "20231006", "20261006", tmp_path)
    assert json.loads((tmp_path / "xbrl_facts.json").read_text()) == []
    # 18행 + 자본변동표의 지배기업 소유주지분 순이익(당기·전년 동기 누적) 2행
    assert summary["xbrl_rows"] == 0 and summary["xbrl_after_end"] == 20


def test_collect_main_fails_when_correction_prelim_unparsed(tmp_path, monkeypatch, capsys):
    client = _fake_dart([], [_item(PRELIM_RCEPT, "[기재정정]연결재무제표기준영업(잠정)실적(공정공시)")],
                        {PRELIM_RCEPT: "<html><body>양식이 바뀐 공시</body></html>".encode()}, {})
    monkeypatch.setattr(collect, "load_dart_key", lambda: "KEY")
    monkeypatch.setattr(collect.httpx, "Client", lambda **kw: client)
    code = collect.main(["--out", str(tmp_path), "--corps", SAMSUNG, "--skip-corp-names", "--end", "20261006"])
    out = capsys.readouterr()
    assert code != 0
    assert PRELIM_RCEPT in out.err and "정정" in out.err
    summary = json.loads(out.out)
    assert summary["correction_parse_errors"] == [PRELIM_RCEPT]


def test_by_corp_code():
    entries = corp_names.build(CORP_XML) + [{"corp_code": "00126380", "corp_name": "삼성전자(우)",
                                             "stock_code": "005935", "norm": "삼성전자(우)"}]
    assert corp_names.by_corp_code(entries) == {"00126380": ["삼성전자", "삼성전자(우)"],
                                                "00164779": ["SK하이닉스"], "00181712": ["SK"]}


# ── PR #55 마지막 코멘트 반영(후속): 제목 없는 잠정실적의 연결·별도 기준 ──

def _strip_xforms_title(html: str) -> str:
    return re.sub(r'<div class="xforms_title">.*?</div>', "", html, count=1, flags=re.S)


def test_prelim_without_xforms_title_uses_html_title_for_fs_div():
    html = _strip_xforms_title(_fix("prelim_samsung_2026Q2_correction.xml"))
    assert 'class="xforms_title"' not in html.split("</head>", 1)[1]
    rep = parse.parse_prelim(html)
    assert {r["fs_div"] for r in parse.prelim_facts(SAMSUNG, PRELIM_RCEPT, rep)} == {"CFS"}


def test_prelim_without_any_title_defaults_to_consolidated_or_manifest():
    html = re.sub(r"<title>.*?</title>", "", _strip_xforms_title(_fix("prelim_samsung_2026Q2_correction.xml")),
                  flags=re.S)
    rep = parse.parse_prelim(html)
    assert {r["fs_div"] for r in parse.prelim_facts(SAMSUNG, PRELIM_RCEPT, rep)} == {"CFS"}  # 기본 연결
    assert {r["fs_div"] for r in parse.prelim_facts(SAMSUNG, PRELIM_RCEPT, rep, fs_div="OFS")} == {"OFS"}  # manifest


def test_prelim_separate_title_still_ofs():
    html = re.sub(r"연결재무제표\s*기준\s*", "", _fix("prelim_samsung_2026Q2_correction.xml"))
    rep = parse.parse_prelim(html)
    assert {r["fs_div"] for r in parse.prelim_facts(SAMSUNG, PRELIM_RCEPT, rep)} == {"OFS"}


# ── 잠정실적 구양식(2025년 10월 이전): 기간은 본표 머리 아래 줄, 단위·부호·전환 여부가 공시마다 다르다 ──

def _old(name: str) -> parse.PrelimReport:
    return parse.parse_prelim(parse.decode((FIX / name).read_bytes()))


def _fig(rep, account, basis="당해실적"):
    (f,) = [f for f in rep.figures if f.account == account and f.basis == basis]
    return f


def test_old_form_eokwon_periods_unit_values():
    rep = _old("prelim_samsung_2025Q2_eokwon.xml")
    assert (rep.period, rep.period_start, rep.period_end, rep.unit) == ("2025Q2", "2025-04-01", "2025-06-30", "억원")
    assert rep.periods["전기실적"] == ("2025-01-01", "2025-03-31")
    assert rep.periods["전년동기실적"] == ("2024-04-01", "2024-06-30")
    assert rep.periods["당기누계실적"] == ("2025-01-01", "2025-06-30")
    assert rep.periods["전년동기누적실적"] == ("2024-01-01", "2024-06-30")
    rev = _fig(rep, "매출액")
    assert (rev.current, rev.prior_q, rev.qoq_pct, rev.prior_y, rev.yoy_pct) == (
        Decimal("745663"), Decimal("791405"), Decimal("-5.78"), Decimal("740683"), Decimal("0.67"))
    assert _fig(rep, "매출액", "누계실적").current == Decimal("1537068")
    assert not rep.is_correction
    rows = {(r["account_id"], r["cumulative"]): r for r in parse.prelim_facts(SAMSUNG, "20250731000001", rep)}
    q2 = rows[("ifrs-full_Revenue", False)]
    assert (q2["amount"], q2["period"], q2["rounding_unit"], q2["fs_div"]) == (74_566_300_000_000, "2025Q2", 10**8, "CFS")
    assert rows[("ifrs-full_Revenue", True)]["amount"] == 153_706_800_000_000
    assert rows[("ifrs-full_ProfitLossAttributableToOwnersOfParent", False)]["amount"] == 4_934_000_000_000
    text = " ".join(p.text for p in parse.prelim_passages(SAMSUNG, "20250731000001", rep))
    assert "745,663억원" in text and "-5.78%" in text and "1,537,068억원" in text


def test_old_form_correction_header_and_correction_table():
    rep = _old("prelim_samsung_2023Q3_correction_oldheader.xml")
    assert (rep.period, rep.unit, rep.is_correction, rep.original_date) == ("2023Q3", "조원", True, "20231011")
    assert _fig(rep, "매출액").current == Decimal("67.40")     # 정정 후 본표 값
    assert _fig(rep, "영업이익", "누계실적").current == Decimal("3.74")
    got = {(c.group, c.item, c.basis): (c.before, c.after) for c in rep.corrections}
    assert got[("당기실적", "매출액", "당해실적")] == (Decimal("67.00"), Decimal("67.40"))
    assert got[("당기실적", "영업이익", "누계실적")] == (Decimal("3.71"), Decimal("3.74"))
    assert got[("전년동기대비증감율(%)", "영업이익", "당해실적")] == (Decimal("-77.88"), Decimal("-77.57"))
    assert len(rep.corrections) == 10
    corr = [p.text for p in parse.prelim_passages(SAMSUNG, "20231031000001", rep) if p.section == parse.CORR]
    assert any("정정 전 67.00조원 → 정정 후 67.40조원" in t for t in corr)


def test_old_form_mwon_negative_turnaround_and_signed_rates():
    rep = _old("prelim_skhynix_2023Q3_mwon_loss.xml")
    assert (rep.period, rep.period_start, rep.unit) == ("2023Q3", "2023-07-01", "백만원")
    assert rep.periods["전년동기실적"] == ("2022-07-01", "2022-09-30")
    op = _fig(rep, "영업이익")
    assert (op.current, op.prior_q, op.qoq_pct, op.prior_y) == (
        Decimal("-1791961"), Decimal("-2882084"), Decimal("37.8"), Decimal("1660523"))
    assert (op.yoy_pct, op.yoy_turn) == (None, "적자전환")
    assert _fig(rep, "매출액").qoq_pct == Decimal("24.1")
    rows = {(r["account_id"], r["cumulative"]): r for r in parse.prelim_facts(HYNIX, "20231026000001", rep)}
    assert rows[("dart_OperatingIncomeLoss", False)]["amount"] == -1_791_961_000_000
    assert rows[("dart_OperatingIncomeLoss", True)]["amount"] == -8_076_347_000_000
    assert rows[("dart_OperatingIncomeLoss", False)]["rounding_unit"] == 10**6
    text = " ".join(p.text for p in parse.prelim_passages(HYNIX, "20231026000001", rep))
    assert "-1,791,961백만원" in text and "전년동기대비 적자전환" in text and "37.8%" in text


def test_new_form_still_parses_unchanged():
    rep = _old("prelim_samsung_2026Q2_correction.xml")
    assert (rep.period, rep.unit) == ("2026Q2", "조원")
    assert _fig(rep, "매출액").current == Decimal("171.50")
