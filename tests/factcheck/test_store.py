# tests/factcheck/test_store.py
"""팩트체커 문단 저장소(Qdrant `factcheck_passages`): 문서 단위 적재·삭제, 문단 ID, payload 계약·색인, 필터 검색.

Qdrant는 qdrant-client 메모리 모드(:memory:)를 감싼 가짜(색인 생성 호출 기록), 임베딩은 고정 벡터 가짜다.
DB·외부 호출 없음.
"""
import asyncio
import json
from pathlib import Path

import numpy as np
import pytest
from qdrant_client import AsyncQdrantClient

from app.services.evidence.passages import Passage
from app.services.factcheck import store

SAMSUNG, HYNIX = "00126380", "00164779"
DIM = 16


class FakeEmbed:
    """본문마다 고정 무작위 벡터. 미리 지정한 본문은 그 벡터를 쓴다."""

    def __init__(self, fixed: dict[str, list[float]] | None = None):
        self.rng = np.random.default_rng(7)
        self.fixed = dict(fixed or {})
        self.calls: list[str] = []

    async def __call__(self, text: str) -> list[float]:
        self.calls.append(text)
        if text not in self.fixed:
            self.fixed[text] = self.rng.normal(size=DIM).tolist()
        return self.fixed[text]


class RecordingQdrant(AsyncQdrantClient):
    """메모리 Qdrant에 payload 색인 생성 호출 기록만 더한 가짜."""

    def __init__(self):
        super().__init__(location=":memory:")
        self.indexes: list[tuple[str, str]] = []

    async def create_payload_index(self, collection_name, field_name, field_schema=None, **kw):
        self.indexes.append((field_name, str(field_schema)))
        return await super().create_payload_index(collection_name, field_name, field_schema=field_schema, **kw)


def _doc(corp=SAMSUNG, rcept_no="20260814003699", report_type="half", period="2026H1", **kw):
    base = dict(corp_code=corp, rcept_no=rcept_no, report_type=report_type, report_nm="반기보고서 (2026.06)",
                period=period, rcept_dt=rcept_no[:8])
    return store.DocMeta(**(base | kw))


def _passages(doc: store.DocMeta, n: int, tag: str = "", section: str = "II") -> list[Passage]:
    return [Passage(store.passage_id(doc.corp_code, doc.rcept_no, section, i), doc.corp_code, doc.rcept_no,
                    section, i, f"{doc.corp_code} {doc.rcept_no} 문단 {i}{tag}") for i in range(n)]


def _run(coro):
    return asyncio.run(coro)


def _all(st: store.FactcheckStore) -> list[dict]:
    return _run(st._scroll(None, store.PAYLOAD_FIELDS))


def test_passage_id_format():
    assert store.passage_id(SAMSUNG, "20260814003699", "III", 12) == "00126380-20260814003699-III-12"


def test_load_document_payload_has_full_contract_and_indexes():
    client = RecordingQdrant()
    st = store.FactcheckStore(client, FakeEmbed())
    doc = _doc(is_correction=True, corp_name="삼성전자")
    res = _run(st.load_document(doc, _passages(doc, 3)))
    assert res == store.LoadResult(passages=3, embedded=3, updated=0, kept=0, removed=0)
    rows = sorted(_all(st), key=lambda p: p["idx"])
    contract = {"passage_id", "corp_code", "rcept_no", "report_type", "report_nm", "period", "rcept_dt", "section",
                "idx", "text"}
    assert contract <= rows[0].keys()
    assert rows[0]["passage_id"] == "00126380-20260814003699-II-0"
    assert (rows[0]["report_type"], rows[0]["period"], rows[0]["rcept_dt"], rows[0]["is_correction"],
            rows[0]["superseded"], rows[0]["corp_name"]) == ("half", "2026H1", "20260814", True, False, "삼성전자")
    assert {f for f, _ in client.indexes} == {"corp_code", "rcept_no", "period", "report_type"}
    assert st.collection == store.COLLECTION == "factcheck_passages"


def test_other_document_of_same_corp_is_kept():
    st = store.FactcheckStore(AsyncQdrantClient(location=":memory:"), FakeEmbed())
    half = _doc()
    q1 = _doc(rcept_no="20260515001111", report_type="quarter", period="2026Q1", report_nm="분기보고서 (2026.03)")
    _run(st.load_document(half, _passages(half, 4)))
    _run(st.load_document(q1, _passages(q1, 2)))
    assert sorted({p["rcept_no"] for p in _all(st)}) == ["20260515001111", "20260814003699"]
    assert len(_all(st)) == 6
    # 반기보고서를 문단 수를 줄여 다시 적재하면 그 문서의 꼬리만 지운다
    res = _run(st.load_document(half, _passages(half, 2)))
    assert (res.embedded, res.kept, res.removed) == (0, 2, 2)
    assert len(_all(st)) == 4
    assert sum(p["rcept_no"] == "20260515001111" for p in _all(st)) == 2


def test_reload_skips_unchanged_and_updates_meta_without_embedding():
    emb = FakeEmbed()
    st = store.FactcheckStore(AsyncQdrantClient(location=":memory:"), emb)
    doc = _doc(report_type="preliminary", period="2026Q2", report_nm="연결재무제표기준영업(잠정)실적(공정공시)")
    ps = _passages(doc, 3, section="PRELIM")
    _run(st.load_document(doc, ps))
    n = len(emb.calls)
    assert _run(st.load_document(doc, ps)) == store.LoadResult(3, 0, 0, 3, 0)
    later = store.DocMeta(**(doc.__dict__ | {"superseded": True}))
    assert _run(st.load_document(later, ps)) == store.LoadResult(3, 0, 3, 0, 0)
    assert len(emb.calls) == n
    assert {p["superseded"] for p in _all(st)} == {True}
    changed = _passages(doc, 3, tag=" 바뀜", section="PRELIM")
    assert _run(st.load_document(later, changed)).embedded == 3


def test_delete_document_removes_only_that_document():
    st = store.FactcheckStore(AsyncQdrantClient(location=":memory:"), FakeEmbed())
    a, b = _doc(), _doc(corp=HYNIX, rcept_no="20260814003509")
    _run(st.load_document(a, _passages(a, 3)))
    _run(st.load_document(b, _passages(b, 2)))
    assert _run(st.delete_document(SAMSUNG, "20260814003699")) == 3
    assert {p["corp_code"] for p in _all(st)} == {HYNIX}
    assert _run(st.delete_document(SAMSUNG, "20260814003699")) == 0


def test_load_rejects_mismatched_passages():
    st = store.FactcheckStore(AsyncQdrantClient(location=":memory:"), FakeEmbed())
    doc = _doc()
    bad = _passages(_doc(rcept_no="20260515001111"), 1)
    with pytest.raises(ValueError):
        _run(st.load_document(doc, bad))
    with pytest.raises(ValueError):
        _run(st.load_document(_doc(report_type="disclosure"), _passages(doc, 1)))


def test_embedding_failure_leaves_previous_load():
    class Boom(FakeEmbed):
        async def __call__(self, text):
            if "실패" in text:
                raise RuntimeError("ollama down")
            return await super().__call__(text)

    st = store.FactcheckStore(AsyncQdrantClient(location=":memory:"), Boom())
    doc = _doc()
    _run(st.load_document(doc, _passages(doc, 2)))
    with pytest.raises(RuntimeError):
        _run(st.load_document(doc, _passages(doc, 1, tag=" 실패")))
    assert len(_all(st)) == 2


def test_search_filters_corp_period_and_report_type():
    q = "2분기 매출액은?"
    qv = np.random.default_rng(3).normal(size=DIM).tolist()
    emb = FakeEmbed({store.QUERY_PREFIX + q: qv})
    st = store.FactcheckStore(AsyncQdrantClient(location=":memory:"), emb)
    half = _doc()
    prelim = _doc(rcept_no="20260730800123", report_type="preliminary", period="2026Q2")
    other = _doc(corp=HYNIX, rcept_no="20260814003509")
    for d in (half, prelim, other):
        _run(st.load_document(d, _passages(d, 3)))
    hits = _run(st.search(SAMSUNG, q, k=10))
    assert {h["corp_code"] for h in hits} == {SAMSUNG} and len(hits) == 6
    hits = _run(st.search(SAMSUNG, q, k=10, periods=["2026Q2"]))
    assert {h["rcept_no"] for h in hits} == {"20260730800123"}
    hits = _run(st.search(SAMSUNG, q, k=10, report_types=["half", "quarter"]))
    assert {h["rcept_no"] for h in hits} == {"20260814003699"}
    assert {"passage_id", "rcept_no", "report_nm", "period", "section", "text", "score"} <= hits[0].keys()


def test_documents_summary():
    st = store.FactcheckStore(AsyncQdrantClient(location=":memory:"), FakeEmbed())
    a, b = _doc(), _doc(rcept_no="20260730800123", report_type="preliminary", period="2026Q2")
    _run(st.load_document(a, _passages(a, 3)))
    _run(st.load_document(b, _passages(b, 1)))
    docs = _run(st.documents())
    assert [(d["rcept_no"], d["passages"]) for d in docs] == [("20260730800123", 1), ("20260814003699", 3)]


def test_load_all_from_collect_output(tmp_path):
    """collect 산출(documents.json·docs/)을 그대로 적재: 잠정실적 본표·정정 문단, I·II·III절, 실패 문서는 센다."""
    fix = Path(__file__).parent / "fixtures"
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "P.xml").write_bytes((fix / "prelim_samsung_2026Q2_correction.xml").read_bytes())
    sec = '<SECTION-1><TITLE>{}</TITLE><P>{} 문단</P></SECTION-1>'
    regular = "".join(sec.format(t, t) for t in ("I. 회사의 개요", "II. 사업의 내용", "III. 재무에 관한 사항"))
    (tmp_path / "docs" / "R.xml").write_text(regular)
    (tmp_path / "docs" / "X.xml").write_text(sec.format("II. 사업의 내용", "II"))  # I·III절 없음 → 실패
    base = {"corp_code": SAMSUNG, "corp_name": "삼성전자", "is_correction": False, "superseded": False}
    docs = [base | {"rcept_no": "20260730800123", "report_type": "preliminary", "is_correction": True,
                    "report_nm": "[기재정정]연결재무제표기준영업(잠정)실적(공정공시)", "period": "2026Q2",
                    "rcept_dt": "20260730", "path": "docs/P.xml"},
            base | {"rcept_no": "20260814003699", "report_type": "half", "report_nm": "반기보고서 (2026.06)",
                    "period": "2026H1", "rcept_dt": "20260814", "path": "docs/R.xml"},
            base | {"rcept_no": "20260515001111", "report_type": "quarter", "report_nm": "분기보고서 (2026.03)",
                    "period": "2026Q1", "rcept_dt": "20260515", "path": "docs/X.xml"},
            base | {"rcept_no": "20260407800009", "report_type": "preliminary", "report_nm": "x", "period": None,
                    "rcept_dt": "20260407", "path": "docs/none.xml", "parse_error": "prelim: 본표 값이 없다"}]
    (tmp_path / "documents.json").write_text(json.dumps(docs, ensure_ascii=False))
    st = store.FactcheckStore(AsyncQdrantClient(location=":memory:"), FakeEmbed())
    summary = _run(store.load_all(st, tmp_path))
    assert (summary["documents"], summary["passages"]) == (2, 3 + 3)
    assert [f["rcept_no"] for f in summary["failed"]] == ["20260515001111", "20260407800009"]
    rows = {p["passage_id"]: p for p in _all(st)}
    assert {"00126380-20260730800123-PRELIM-0", "00126380-20260730800123-PRELIM-1",
            "00126380-20260730800123-CORR-0", "00126380-20260814003699-I-0",
            "00126380-20260814003699-II-0", "00126380-20260814003699-III-0"} == set(rows)
    assert rows["00126380-20260730800123-CORR-0"]["is_correction"] is True
    assert rows["00126380-20260814003699-III-0"]["period"] == "2026H1"


def test_load_all_prunes_documents_dropped_from_manifest(tmp_path):
    """정정 공시가 나와 목록에서 밀린 정기보고서는 지우고, 목록에 없는 회사의 문서는 그대로 둔다."""
    st = store.FactcheckStore(AsyncQdrantClient(location=":memory:"), FakeEmbed())
    old = _doc(rcept_no="20260310002820", report_type="annual", period="2025", report_nm="사업보고서 (2025.12)")
    other = _doc(corp=HYNIX, rcept_no="20260317000635", report_type="annual", period="2025")
    _run(st.load_document(old, _passages(old, 2)))
    _run(st.load_document(other, _passages(other, 1)))
    sec = '<SECTION-1><TITLE>{}</TITLE><P>{} 문단</P></SECTION-1>'
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "N.xml").write_text(
        "".join(sec.format(t, t) for t in ("I. 회사의 개요", "II. 사업의 내용", "III. 재무에 관한 사항")))
    new = {"corp_code": SAMSUNG, "corp_name": "삼성전자", "rcept_no": "20260402000200", "report_type": "annual",
           "report_nm": "[기재정정]사업보고서 (2025.12)", "period": "2025", "rcept_dt": "20260402",
           "is_correction": True, "superseded": False, "path": "docs/N.xml"}
    (tmp_path / "documents.json").write_text(json.dumps([new], ensure_ascii=False))
    assert _run(store.load_all(st, tmp_path))["pruned"] == []  # 기본은 정리하지 않는다
    assert len({p["rcept_no"] for p in _all(st)}) == 3
    summary = _run(store.load_all(st, tmp_path, prune=True))
    assert summary["pruned"] == ["20260310002820"]
    assert sorted({(p["corp_code"], p["rcept_no"]) for p in _all(st)}) == [
        (SAMSUNG, "20260402000200"), (HYNIX, "20260317000635")]


# ── PR #55 검수 반영 ──────────────────────────────────────────────────────────

def _history_store():
    q = "2분기 매출액은?"
    emb = FakeEmbed({store.QUERY_PREFIX + q: np.random.default_rng(3).normal(size=DIM).tolist()})
    st = store.FactcheckStore(AsyncQdrantClient(location=":memory:"), emb)
    orig = _doc(rcept_no="20260707800001", report_type="preliminary", period="2026Q2", superseded=True,
                fs_div="CFS")
    corr = _doc(rcept_no="20260730800123", report_type="preliminary", period="2026Q2", is_correction=True,
                fs_div="CFS")
    _run(st.load_document(orig, _passages(orig, 2, section="PRELIM")))
    _run(st.load_document(corr, _passages(corr, 2, section="PRELIM") + [
        Passage(store.passage_id(SAMSUNG, corr.rcept_no, "CORR", 0), SAMSUNG, corr.rcept_no, "CORR", 0,
                "정정 전 171.00 → 정정 후 171.50")]))
    return st, q


def test_search_excludes_superseded_and_correction_history_by_default():
    st, q = _history_store()
    hits = _run(st.search(SAMSUNG, q, k=10))
    assert {(h["rcept_no"], h["section"]) for h in hits} == {("20260730800123", "PRELIM")}
    hist = _run(st.search(SAMSUNG, q, k=10, include_history=True))
    assert {(h["rcept_no"], h["section"]) for h in hist} == {
        ("20260707800001", "PRELIM"), ("20260730800123", "PRELIM"), ("20260730800123", "CORR")}
    assert {h["fs_div"] for h in hist} == {"CFS"}


def test_latest_period_skips_superseded_documents():
    st = store.FactcheckStore(AsyncQdrantClient(location=":memory:"), FakeEmbed())
    assert _run(st.latest_period(SAMSUNG)) is None
    for d in (_doc(rcept_no="20260310002820", report_type="annual", period="2025"),
              _doc(rcept_no="20260515001111", report_type="quarter", period="2026Q1"),
              _doc(rcept_no="20260814003699", report_type="half", period="2026H1"),
              _doc(rcept_no="20261008800001", report_type="preliminary", period="2026Q3", superseded=True),
              _doc(corp=HYNIX, rcept_no="20261020800002", report_type="preliminary", period="2026Q4")):
        _run(st.load_document(d, _passages(d, 1)))
    assert _run(st.latest_period(SAMSUNG)) == "2026H1"
    later = _doc(rcept_no="20261009800003", report_type="preliminary", period="2026Q3")
    _run(st.load_document(later, _passages(later, 1)))
    assert _run(st.latest_period(SAMSUNG)) == "2026Q3"
    assert store.period_key("2025") > store.period_key("2025Q3") > store.period_key("2025H1") \
        == store.period_key("2025Q2") > store.period_key("2025Q1")


def test_load_all_clears_previous_load_of_document_that_failed_now(tmp_path):
    """이번에 분해 실패한 문서의 예전 적재본은 지운다(옛 값이 현재값처럼 남지 않게)."""
    st = store.FactcheckStore(AsyncQdrantClient(location=":memory:"), FakeEmbed())
    old = _doc(rcept_no="20260730800123", report_type="preliminary", period="2026Q2", is_correction=True)
    _run(st.load_document(old, _passages(old, 2, section="PRELIM")))
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "P.xml").write_text("<html>바뀐 양식</html>")
    doc = {"corp_code": SAMSUNG, "corp_name": "삼성전자", "rcept_no": "20260730800123",
           "report_type": "preliminary", "report_nm": "[기재정정]연결재무제표기준영업(잠정)실적(공정공시)",
           "period": "2026Q2", "rcept_dt": "20260730", "is_correction": True, "superseded": False,
           "fs_div": "CFS", "path": "docs/P.xml"}
    (tmp_path / "documents.json").write_text(json.dumps([doc], ensure_ascii=False))
    summary = _run(store.load_all(st, tmp_path))
    assert [f["rcept_no"] for f in summary["failed"]] == ["20260730800123"]
    assert summary["cleared"] == {"20260730800123": 2}
    assert _all(st) == []


# ── PR #55 마지막 코멘트 반영(후속) ─────────────────────────────────────────────

_SEC = '<SECTION-1><TITLE>{}</TITLE><P>{}</P></SECTION-1>'


def _regular_xml(body: str) -> str:
    return "".join(_SEC.format(t, f"{t} {body}") for t in ("I. 회사의 개요", "II. 사업의 내용", "III. 재무에 관한 사항"))


def test_load_all_keeps_previous_load_when_embedding_fails(tmp_path):
    """임베딩·저장 오류는 기존 적재본을 지우지 않는다(파싱 실패만 기존본을 정리한다)."""
    class BadEmbed(FakeEmbed):
        async def __call__(self, text):
            if "새 본문" in text:
                raise ValueError("embedding: invalid response")
            return await super().__call__(text)

    st = store.FactcheckStore(AsyncQdrantClient(location=":memory:"), BadEmbed())
    old = _doc(rcept_no="20260814003699", report_type="half", period="2026H1")
    _run(st.load_document(old, _passages(old, 2)))
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "R.xml").write_text(_regular_xml("새 본문"))
    d = {"corp_code": SAMSUNG, "corp_name": "삼성전자", "rcept_no": old.rcept_no, "report_type": "half",
         "report_nm": "반기보고서 (2026.06)", "period": "2026H1", "rcept_dt": "20260814", "is_correction": False,
         "superseded": False, "path": "docs/R.xml"}
    (tmp_path / "documents.json").write_text(json.dumps([d], ensure_ascii=False))
    summary = _run(store.load_all(st, tmp_path))
    assert [(f["rcept_no"], f["stage"]) for f in summary["failed"]] == [(old.rcept_no, "load")]
    assert summary["cleared"] == {}
    assert len(_all(st)) == 2


def test_load_all_parse_failure_is_marked_parse_stage(tmp_path):
    st = store.FactcheckStore(AsyncQdrantClient(location=":memory:"), FakeEmbed())
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "X.xml").write_text(_SEC.format("II. 사업의 내용", "II"))
    d = {"corp_code": SAMSUNG, "rcept_no": "20260515001111", "report_type": "quarter", "report_nm": "x",
         "period": "2026Q1", "rcept_dt": "20260515", "path": "docs/X.xml"}
    (tmp_path / "documents.json").write_text(json.dumps([d], ensure_ascii=False))
    assert [f["stage"] for f in _run(store.load_all(st, tmp_path))["failed"]] == ["parse"]


def test_loading_correction_marks_older_same_period_document_superseded(tmp_path):
    """정정으로 목록에서 밀린 옛 정기보고서(prune 없이 남은 것)는 적재 때 superseded로 표시해 검색에서 뺀다."""
    q = "사업 내용"
    emb = FakeEmbed({store.QUERY_PREFIX + q: np.random.default_rng(5).normal(size=DIM).tolist()})
    st = store.FactcheckStore(AsyncQdrantClient(location=":memory:"), emb)
    old = _doc(rcept_no="20260310002820", report_type="annual", period="2025", report_nm="사업보고서 (2025.12)")
    _run(st.load_document(old, _passages(old, 2)))
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "N.xml").write_text(_regular_xml("정정본"))
    new = {"corp_code": SAMSUNG, "corp_name": "삼성전자", "rcept_no": "20260402000200", "report_type": "annual",
           "report_nm": "[기재정정]사업보고서 (2025.12)", "period": "2025", "rcept_dt": "20260402",
           "is_correction": True, "superseded": False, "path": "docs/N.xml"}
    (tmp_path / "documents.json").write_text(json.dumps([new], ensure_ascii=False))
    _run(store.load_all(st, tmp_path))
    flags = {(p["rcept_no"], p["superseded"]) for p in _all(st)}
    assert flags == {("20260310002820", True), ("20260402000200", False)}
    assert {h["rcept_no"] for h in _run(st.search(SAMSUNG, q, k=10))} == {"20260402000200"}


def test_loading_older_document_after_newer_marks_the_older_one():
    st = store.FactcheckStore(AsyncQdrantClient(location=":memory:"), FakeEmbed())
    new = _doc(rcept_no="20260730800123", report_type="preliminary", period="2026Q2", fs_div="CFS")
    old = _doc(rcept_no="20260707800001", report_type="preliminary", period="2026Q2", fs_div="CFS")
    other_fs = _doc(rcept_no="20260707800002", report_type="preliminary", period="2026Q2", fs_div="OFS")
    other_period = _doc(rcept_no="20260407800001", report_type="preliminary", period="2026Q1", fs_div="CFS")
    for d in (new, old, other_fs, other_period):
        _run(st.load_document(d, _passages(d, 1)))
    flags = {p["rcept_no"]: p["superseded"] for p in _all(st)}
    assert flags == {"20260730800123": False, "20260707800001": True, "20260707800002": False,
                     "20260407800001": False}

