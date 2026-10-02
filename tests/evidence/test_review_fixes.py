"""최종 리뷰 지적(Critical 1~4, Important 5~10, 일부 Minor)을 재현하는 테스트."""
import json

import httpx
import pytest

from app.lib import jev
from app.services.evidence import dart
from app.services.evidence import passages as ps
from lab.evidence import __main__ as cli
from lab.evidence import split


def _root(tmp_path, comps):
    P = cli.Paths(tmp_path)
    P.split_json.parent.mkdir(parents=True, exist_ok=True)
    P.split_json.write_text(json.dumps({"companies": comps}))
    return P


COMPS = [{"corp_code": "a", "corp_name": "A", "split": "tune", "cluster": 0, "rcept_no": "1"},
         {"corp_code": "b", "corp_name": "B", "split": "check", "cluster": 1, "rcept_no": "2"},
         {"corp_code": "c", "corp_name": "C", "split": "holdout", "cluster": 2, "rcept_no": "3"}]


# Critical 1 — all 분할과 빈 동결 파일로 홀드아웃 봉인을 우회할 수 없다
def test_all_split_and_empty_freeze_file_do_not_unseal_holdout(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "verify_freeze", lambda P: None)  # 해시 재검증은 test_stage1_fixes에서
    P = _root(tmp_path, COMPS)
    with pytest.raises(SystemExit, match="frozen"):
        cli.companies(P, "all")
    P.prereg_holdout.write_text("{}")
    with pytest.raises(SystemExit, match="frozen"):
        cli.companies(P, "holdout")
    P.prereg_holdout.write_text(json.dumps({"split_sha256": "x", "claims_sha256": "y"}))
    assert [c["corp_code"] for c in cli.companies(P, "all")] == ["a", "b", "c"]


# Critical 2 — 라벨 누락·중복을 조용히 넘기지 않는다
def test_label_coverage_reports_missing_and_duplicates(tmp_path):
    r1 = {"opus": {"x": {"label": "supported"}}, "codex": {"x": {"label": "supported"}, "y": {"label": "no_evidence"}}}
    assert cli.missing_labels(r1, ["x", "y", "z"]) == {"opus": ["y", "z"], "codex": ["z"]}
    P = cli.Paths(tmp_path)
    d = P.data / "labels/r1_opus"
    d.mkdir(parents=True)
    (d / "a.jsonl").write_text('{"lid":"L1","label":"supported"}\n{"lid":"L1","label":"no_evidence"}\n')
    with pytest.raises(ValueError, match="duplicate"):
        cli._read_labels(P, 1, "opus", {"a"}, {"L1": "a-q1-n1"})


# Critical 3 — 지연·실패 관문은 단건(single) 요청을 섞지 않는다
def test_latency_rows_exclude_single_and_report_sessions():
    calls = [{"attempt": 1, "tag": "check", "ok": True, "latency_ms": 300.0, "input_tokens": 2000,
              "called_at": "2026-10-02T01:00:00+00:00"},
             {"attempt": 1, "tag": "single", "ok": True, "latency_ms": 50.0, "input_tokens": 400,
              "called_at": "2026-10-02T01:01:00+00:00"},
             {"attempt": 2, "tag": "check", "ok": True, "latency_ms": 900.0, "input_tokens": 2000,
              "called_at": "2026-10-02T01:02:00+00:00"},
             {"attempt": 1, "tag": "repeat1", "ok": False, "latency_ms": 500.0, "input_tokens": 0,
              "called_at": "2026-10-02T03:00:00+00:00"}]
    gated, single = cli.latency_rows(calls)
    assert [r["latency_ms"] for r in gated] == [300.0, 500.0] and [r["tag"] for r in single] == ["single"]
    s = cli.session_stats(gated)
    assert [x["n"] for x in s] == [1, 1] and s[1]["failures"] == 1


# Critical 4 — 두 칸짜리 캡션·단위 표를 살리고, 단위가 다음 표로 새지 않는다
def test_caption_unit_table_and_no_unit_leak():
    sec = """<SECTION-1><TITLE>II. 사업의 내용</TITLE><SECTION-2><TITLE>가. 매출</TITLE>
<TABLE><TR><TD>[연결기준]</TD><TD>(단위 : 백만원)</TD></TR></TABLE>
<TABLE><TR><TH>구분</TH><TH>2025</TH></TR><TR><TD>매출</TD><TD>1,000</TD></TR></TABLE>
<TABLE><TR><TH>구분</TH><TH>인원</TH></TR><TR><TD>직원</TD><TD>120</TD></TR></TABLE>
</SECTION-2></SECTION-1>"""
    rows = [t for k, t in ps.blocks(sec) if k == "row"]
    assert rows == ["[가. 매출 표, [연결기준], 단위 백만원] 구분: 매출 | 2025: 1,000", "[가. 매출 표] 구분: 직원 | 인원: 120"]


# Minor — 유니코드 로마 숫자 제목
def test_sections_accept_unicode_roman_numerals():
    xml = ("<SECTION-1><TITLE>Ⅰ. 회사의 개요</TITLE><SECTION-2><TITLE>1. 회사의 개요</TITLE><P>가</P></SECTION-2>"
           "</SECTION-1><SECTION-1><TITLE>Ⅱ. 사업의 내용</TITLE><P>나</P></SECTION-1>")
    assert set(ps.sections(xml)) == {"I1", "II"}


# Important 5 — 교차 언급 근거를 남기고, 분할이 비면 실패한다
def test_mention_edges_and_split_sanity():
    names = {"a": "에이사", "b": "비이사", "c": "씨이사"}
    assert split.mention_edges(names, {"a": "비이사와 거래 " * 10, "b": "", "c": ""}) == [("a", "b", "비이사")]
    with pytest.raises(SystemExit, match="split"):
        split.check_split_sizes({"tune": 3, "check": 0, "holdout": 18})
    split.check_split_sizes({"tune": 9, "check": 8, "holdout": 18})


# Important 6 — 문단 본문이 바뀌면 낡은 벡터를 쓰지 않는다
def test_load_vectors_drops_stale(tmp_path):
    P = cli.Paths(tmp_path)
    cli.write_jsonl(P.priv / "passages.jsonl", [{"id": "p1", "corp_code": "a", "text": "새 본문"},
                                                {"id": "p2", "corp_code": "a", "text": "그대로"}])
    import hashlib
    h = lambda t: hashlib.sha256(t.encode()).hexdigest()
    cli.write_jsonl(P.priv / "passage_vecs.jsonl", [{"id": "p1", "sha256": h("옛 본문"), "vec": [1]},
                                                    {"id": "p2", "sha256": h("그대로"), "vec": [2]}])
    assert cli.load_vectors(P) == {"p2": [2]}


# Important 7 — 통제 주장: 배정 유형 일치, qid 중복 거부, 배정은 분할과 무관
def test_assigned_variant_is_global_and_checked():
    qids = ["b-q1", "a-q2", "a-q1"]
    assert cli.assigned_variant("a-q1", qids) == cli.VARIANTS[0] and cli.assigned_variant("b-q1", qids) == cli.VARIANTS[2]
    row = {"qid": "a-q1", "true_text": "가 100", "variant_text": "가 900", "variant_type": cli.VARIANTS[1]}
    assert cli.check_controlled(row, ["가 100"], (), expected_type=cli.VARIANTS[0]) == (False, "variant type mismatch")
    with pytest.raises(ValueError, match="duplicate qid"):
        cli.unique_by_qid([{"qid": "a-q1"}, {"qid": "a-q1"}])


# Important 8 — 답변이 동결된 질문의 검색 결과는 다시 쓰지 않는다
def test_retrieval_targets_skip_answered():
    qs = [{"qid": "a-q1"}, {"qid": "a-q2"}]
    assert [q["qid"] for q in cli.retrieval_targets(qs, {"a-q1"})] == ["a-q2"]


# Important 9 — 원장 해시와 다른(끊긴) 파일은 다시 받는다
def test_download_refetches_when_hash_differs(tmp_path):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("d.xml", "<DOCUMENT>full</DOCUMENT>")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=buf.getvalue())))
    ledger = tmp_path / "ledger.jsonl"
    p = dart.download_document(client, "K", "9", tmp_path / "docs", ledger)
    p.write_text("<DOCUMENT>trunc")
    p2 = dart.download_document(client, "K", "9", tmp_path / "docs", ledger)
    assert p2.read_text() == "<DOCUMENT>full</DOCUMENT>" and not list((tmp_path / "docs").glob("*.part"))


# Important 10 — 불일치가 없는 회사에는 2차 꾸러미를 만들지 않는다
def test_round2_packets_only_for_disagreements(tmp_path):
    P = _root(tmp_path, COMPS[:2])
    (P.ev / "prompts").mkdir(parents=True)
    (P.ev / "prompts/labeler.md").write_text("지시")
    cli.write_jsonl(P.priv / "passages.jsonl", [{"id": f"{c}-p", "corp_code": c, "text": "t"} for c in "ab"])
    cli.write_jsonl(P.jsonl("retrieval.jsonl"), [{"qid": f"{c}-q1", "passage_ids": [f"{c}-p"]} for c in "ab"])
    cli.write_jsonl(P.jsonl("questions.jsonl"), [{"qid": f"{c}-q1", "corp_code": c, "question": "q"} for c in "ab"])
    cli.write_jsonl(P.jsonl("claims.jsonl"), [{"cid": f"{c}-q1-n1", "qid": f"{c}-q1", "text": "x"} for c in "ab"])
    for lb, labs in (("opus", {"a": "supported", "b": "supported"}), ("codex", {"a": "supported", "b": "no_evidence"})):
        for c, l in labs.items():
            cli.write_jsonl(P.data / f"labels/r1_{lb}/{c}.jsonl",
                            [{"lid": cli.lid(f"{c}-q1-n1"), "label": l, "passage": 1, "reason": "r"}])
    args = type("A", (), {"split": "dev", "round": 2, "labeler": "opus"})()
    cli.cmd_label_packets(P, args)
    assert sorted(x.name for x in (P.priv / "packets/labels/r2_opus").glob("*.md")) == ["b.md"]


# Minor — 캐시를 끈 반복 호출이 기존 캐시를 덮지 않는다
def test_no_cache_call_does_not_replace_cached_answer(tmp_path):
    Q = {"p1": {"type": "choice", "instructions": "x", "criteria": {"supports": "a", "contradicts": "b", "says_nothing": "c"}}}
    bodies = iter([{"supports": 0.9, "contradicts": 0.05, "says_nothing": 0.05},
                   {"supports": 0.1, "contradicts": 0.05, "says_nothing": 0.85}])

    def handler(request):
        return httpx.Response(200, json={"model": jev.MODEL, "usage": {"input_tokens": 1},
                                         "answers": {"p1": {"probabilities": next(bodies)}}})

    c = jev.JevClient(tmp_path / "c.jsonl", 100, client=httpx.Client(transport=httpx.MockTransport(handler)), api_key="k")
    c.ask("s", Q)
    c.ask("s", Q, use_cache=False)
    assert c.ask("s", Q).answers["p1"]["supports"] == 0.9
    again = jev.JevClient(tmp_path / "c.jsonl", 100, client=httpx.Client(transport=httpx.MockTransport(handler)), api_key="k")
    assert again.ask("s", Q).answers["p1"]["supports"] == 0.9


# Minor — κ 군집 부트스트랩 구간과 Ollama digest 고정 확인
def test_kappa_ci_and_digest_check():
    rows = [{"cluster": c, "qid": f"{c}-{q}", "a": (c + q) % 2 == 0, "b": (c + q) % 2 == 0} for c in range(5) for q in range(4)]
    k, lo, hi = cli.kappa_ci(rows)
    assert k == 1.0 and lo == 1.0 and hi == 1.0
    tags = [{"name": "llama3.2:1b", "digest": "baf6a787fdff0000"}]
    assert cli.model_digest(tags, "llama3.2:1b", "baf6a787fdff") == "baf6a787fdff"
    with pytest.raises(SystemExit, match="digest"):
        cli.model_digest(tags, "llama3.2:1b", "ffffffffffff")


# 실행 중 발견 — embed는 --split을 따른다(홀드아웃은 동결 전 임베딩하지 않음)
def test_embed_respects_split(tmp_path, monkeypatch):
    P = _root(tmp_path, COMPS)
    cli.write_jsonl(P.priv / "passages.jsonl", [{"id": f"{c}-p", "corp_code": c, "text": c} for c in "abc"])

    class Fake:
        async def embed(self, model, text):
            return [1.0]

    monkeypatch.setattr(cli, "_ollama", lambda: Fake())
    cli.cmd_embed(P, type("A", (), {"split": "tune"})())
    assert [r["id"] for r in cli.read_jsonl(P.priv / "passage_vecs.jsonl")] == ["a-p"]
