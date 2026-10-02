# tests/evidence/test_passages.py
from app.services.evidence import passages as ps

XML = """<DOCUMENT><BODY>
<SECTION-1><TITLE ATOC="Y">I. 회사의 개요</TITLE>
<SECTION-2><TITLE>1. 회사의 개요</TITLE><P>당사는 반도체를 만듭니다.</P></SECTION-2>
<SECTION-2><TITLE>2. 회사의 연혁</TITLE><P>연혁 문단.</P></SECTION-2>
</SECTION-1>
<SECTION-1><TITLE ENG="II">II.사업의  내용</TITLE>
<SECTION-2><TITLE>1. 사업의 개요</TITLE>
<P>첫 문장입니다. 둘째 문장입니다.</P>
<TABLE><TBODY><TR><TD>(단위 : 억원, %)</TD></TR></TBODY></TABLE>
<TABLE><THEAD><TR><TH>부문</TH><TH>매출액</TH></TR></THEAD>
<TBODY><TR><TD>DX 부문</TD><TD>1,006,771</TD></TR><TR><TE>DS 부문</TE><TE>2,092,317</TE></TR></TBODY></TABLE>
</SECTION-2></SECTION-1>
<SECTION-1><TITLE>III. 재무에 관한 사항</TITLE><P>제외.</P></SECTION-1>
</BODY></DOCUMENT>"""


def test_sections_tolerates_title_spacing():
    s = ps.sections(XML)
    assert set(s) == {"I1", "II"}
    assert "연혁" not in s["I1"] and "제외" not in s["II"]


def test_table_rows_carry_unit_and_header():
    rows = [t for k, t in ps.blocks(ps.sections(XML)["II"]) if k == "row"]
    assert rows == ["[1. 사업의 개요 표, 단위 억원, %] 부문: DX 부문 | 매출액: 1,006,771",
                    "[1. 사업의 개요 표, 단위 억원, %] 부문: DS 부문 | 매출액: 2,092,317"]


def test_pack_keeps_chunks_under_limit_and_splits_long_paragraph():
    long = "가" * 400 + ". " + "나" * 400 + "."
    chunks = ps.pack([("title", "제목"), ("para", long), ("para", "짧은 문단.")], max_chars=600)
    assert len(chunks) == 2 and chunks[0].startswith("[제목] ")
    assert chunks[1].endswith("짧은 문단.")


def test_oversized_single_sentence_is_kept_whole():
    one = "다" * 700 + "."
    assert ps.pack([("para", one)], max_chars=600) == [one]


def test_build_passages_ids_and_missing_section():
    out = ps.build_passages("00126380", "2026", XML)
    assert out[0].id == "00126380-I1-0000" and out[0].text == "[1. 회사의 개요] 당사는 반도체를 만듭니다."
    assert {p.section for p in out} == {"I1", "II"} and len(out[0].sha256) == 64
    import pytest
    with pytest.raises(ValueError, match="missing sections"):
        ps.build_passages("1", "2", "<DOCUMENT></DOCUMENT>")
