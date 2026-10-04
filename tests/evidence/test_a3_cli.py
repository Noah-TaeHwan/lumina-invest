# tests/evidence/test_a3_cli.py
"""A-3 연구 분리(--study a3)와 신뢰 장치: 경로, 사전등록 확정 전 봉인, 통제 주장 c1·c2·c3, 예산 중지, 사전등록 대조."""
import hashlib
import json
from pathlib import Path

import pytest

from app.services.evidence import claims, generate, judge, lexical, subject
from lab.evidence import __main__ as cli
from lab.evidence import a2, a3

REPO = Path(__file__).resolve().parents[2]
COMPS = [{"corp_code": "k", "corp_name": "케이산업", "split": "check", "cluster": 0, "rcept_no": "2"},
         {"corp_code": "m", "corp_name": "엠소재", "split": "check", "cluster": 1, "rcept_no": "3"}]


def _P(tmp_path, status="draft"):
    P = cli.Paths(tmp_path, "a3")
    P.split_json.parent.mkdir(parents=True, exist_ok=True)
    P.split_json.write_text(json.dumps({"companies": COMPS}))
    P.prereg.write_text(json.dumps({"study": "a3", "status": status, "token_cap": 7_000_000,
                                    "generator": {"model": "llama3.1:8b", "digest": "46e0c10c039e"}}))
    return P


def test_a3_paths_are_separate(tmp_path):
    P = cli.Paths(tmp_path, "a3")
    assert P.data == tmp_path / "lab/evidence/study_a3" and P.priv == tmp_path / "lab/data/evidence_a3"
    assert P.split_json == tmp_path / "lab/evidence/split_a3.json"
    assert (P.prereg, P.prereg_holdout) == (tmp_path / "lab/evidence/prereg_a3.json",
                                            tmp_path / "lab/evidence/prereg_a3_check.json")
    assert P.calls == tmp_path / "lab/data/evidence_a3/jev_calls.jsonl" and P.sealed == "check"
    assert P.attempts == cli.Paths(tmp_path).attempts
    assert cli.companies(_P(tmp_path), "dev") == []  # 조정 세트 없음


def test_check_data_sealed_until_prereg_registered(tmp_path):
    P = _P(tmp_path)
    with pytest.raises(SystemExit, match="registered"):
        cli.companies(P, "check", stage="data")
    _P(tmp_path, "registered")
    assert [c["corp_code"] for c in cli.companies(P, "check", stage="data")] == ["k", "m"]
    P.prereg_holdout.write_text("{}")
    with pytest.raises(SystemExit, match="frozen"):
        cli.companies(P, "check", stage="data")


def test_split_refused_while_draft(tmp_path):
    P = _P(tmp_path)
    with pytest.raises(SystemExit, match="registered"):
        cli.cmd_split(P, None)


def test_study_only_commands(tmp_path):
    with pytest.raises(SystemExit, match="a3"):
        cli.main(["--root", str(tmp_path), "--study", "a2", "a3-report"])
    with pytest.raises(SystemExit, match="a2"):
        cli.main(["--root", str(tmp_path), "--study", "a3", "a2-tune"])
    with pytest.raises(SystemExit, match="a1"):
        cli.main(["--root", str(tmp_path), "--study", "a3", "stage1-report"])


def _controlled_inputs(P):
    cli.write_jsonl(P.priv / "passages.jsonl", [
        {"id": "k-p1", "corp_code": "k", "text": "당사는 2006년 품질경영시스템 인증을 획득하였습니다."},
        {"id": "k-p2", "corp_code": "k", "text": "㈜한빛소재와 공급계약을 체결하였습니다."}])
    cli.write_jsonl(P.jsonl("questions.jsonl"), [{"qid": "k-q1", "corp_code": "k", "question": "q", "split": "check"}])
    cli.write_jsonl(P.jsonl("retrieval.jsonl"), [{"qid": "k-q1", "passage_ids": ["k-p1", "k-p2"]}])


def test_controlled_check_writes_c1_c2_c3(tmp_path):
    P = _P(tmp_path, "registered")
    _controlled_inputs(P)
    sub, nt = a3.assigned("k-q1", ["k-q1"])
    cli.write_jsonl(P.data / "controlled" / "k.jsonl", [{
        "qid": "k-q1", "true_text": "케이산업은 2006년 인증을 획득했다.",
        "swap_text": "고려제강은 2006년 인증을 획득했다.", "swap_subtype": sub,
        "notation_text": "케이산업은 2006년 인증을 획득했다.", "notation_type": nt, "source_passage": 1}])
    cli.cmd_controlled_check(P, type("A", (), {"split": "check"})())
    rows = cli.read_jsonl(P.jsonl("claims.jsonl"))
    assert [r["cid"] for r in rows] == []  # 표기 변형이 참 문장과 같아 질문 전체를 거부한다
    rej = cli.read_jsonl(P.jsonl("controlled_rejected.jsonl"))
    assert rej == [{"qid": "k-q1", "reason": "notation: notation equals true text"}]

    cli.write_jsonl(P.data / "controlled" / "k.jsonl", [{
        "qid": "k-q1", "true_text": "한빛소재와 공급 계약을 맺었다.", "swap_text": "두리소재와 공급 계약을 맺었다.",
        "swap_subtype": sub, "notation_text": "한빛소재 주식회사와 공급 계약을 맺었다.", "notation_type": nt,
        "source_passage": 2}])
    cli.cmd_controlled_check(P, type("A", (), {"split": "check"})())
    rows = cli.read_jsonl(P.jsonl("claims.jsonl"))
    assert [(r["cid"], r["variant"], r["expected"]) for r in rows] == [
        ("k-q1-c1", "의역", "supported"), ("k-q1-c2", f"주체 교체:{sub}", "not_supported"),
        ("k-q1-c3", f"표기 변형:{nt}", "supported")]


def test_controlled_check_allows_missing_abbreviation_only(tmp_path):
    P = _P(tmp_path, "registered")
    _controlled_inputs(P)
    qids = ["k-q1", "k-q2", "k-q3"]
    cli.write_jsonl(P.jsonl("questions.jsonl"), [{"qid": q, "corp_code": "k", "question": "q", "split": "check"}
                                                 for q in qids])
    cli.write_jsonl(P.jsonl("retrieval.jsonl"), [{"qid": q, "passage_ids": ["k-p1", "k-p2"]} for q in qids])
    rows = []
    for q in qids:
        sub, nt = a3.assigned(q, qids)
        rows.append({"qid": q, "true_text": "한빛소재와 공급 계약을 맺었다.", "swap_text": "두리소재와 공급 계약을 맺었다.",
                     "swap_subtype": sub, "notation_text": None, "notation_type": nt, "source_passage": 2})
    cli.write_jsonl(P.data / "controlled" / "k.jsonl", rows)
    cli.cmd_controlled_check(P, type("A", (), {"split": "check"})())
    got = [r["cid"] for r in cli.read_jsonl(P.jsonl("claims.jsonl"))]
    assert got == ["k-q3-c1", "k-q3-c2"]  # 영문·약칭(3번째)만 c3 없이 받고 나머지는 거부
    assert [r["qid"] for r in cli.read_jsonl(P.jsonl("controlled_rejected.jsonl"))] == ["k-q1", "k-q2"]


def test_controlled_packets_use_a3_prompt(tmp_path):
    P = _P(tmp_path, "registered")
    _controlled_inputs(P)
    (P.ev / "prompts").mkdir(parents=True)
    (P.ev / "prompts/controlled_writer_a3.md").write_text("A3 지시")
    cli.cmd_controlled_packets(P, type("A", (), {"split": "check"})())
    out = (P.priv / "packets/controlled/k.md").read_text()
    assert out.startswith("A3 지시") and "주체 교체 하위 유형: 회사" in out and "표기 변형 유형: 법인 표기" in out


def test_budget_stops_instead_of_dropping_controlled(tmp_path):
    P = _P(tmp_path, "registered")
    rows = [{"cid": f"k-q1-n{i}", "qid": "k-q1", "source": "natural", "text": f"문장 {i}"} for i in range(1400)]
    text = {"k-p1": "문단"}
    with pytest.raises(SystemExit, match="token budget"):
        cli.a2_budget(P, "check", rows, text, {"k-q1": ["k-p1"]}, {"k": "케이산업"})
    assert cli.a2_budget(P, "check", rows[:10], text, {"k-q1": ["k-p1"]}, {"k": "케이산업"}) == rows[:10]
    assert not any("budget-drop" in x for x in (P.attempts.read_text() if P.attempts.exists() else "").splitlines())


def test_freeze_bundle_includes_a3_prompt(tmp_path, monkeypatch):
    P = _P(tmp_path, "registered")
    (P.ev / "prompts").mkdir(parents=True)
    (P.ev / "prompts/controlled_writer_a3.md").write_text("x")
    monkeypatch.setattr(cli, "_code_sha", lambda P: "code1")
    b = cli.freeze_bundle(P)
    assert b["prompts_sha256"]["controlled_writer_a3.md"] == hashlib.sha256(b"x").hexdigest()
    assert "tune_sha256" not in b


def _sha(rel):
    return hashlib.sha256((REPO / rel).read_bytes()).hexdigest()


def test_committed_prereg_a3_matches_code():
    """사전등록 초안의 값이 코드 상수·해시와 같다(사전등록 뒤 코드가 바뀌면 여기서 먼저 드러난다)."""
    pre = json.loads((REPO / "lab/evidence/prereg_a3.json").read_text())
    assert pre["study"] == "a3" and pre["status"] in ("draft", "registered")
    assert pre["judge_question_sha"] == judge.QUESTION_SHA
    g = pre["generator"]
    assert (g["model"], g["digest"], g["prompt_sha"], g["options"]) == (
        "llama3.1:8b", "46e0c10c039e", generate.PROMPT_SHA, generate.OPTIONS)
    assert pre["draw"]["seed"] == a3.SEED == 20261204 and pre["draw"]["random_n"] == a3.RANDOM_N
    assert pre["draw"]["seed"] not in (20261002, 20261103)
    assert pre["token_cap"] == a3.TOKEN_CAP and pre["gates"] == a3.GATES
    assert pre["bootstrap"] == {"levels": ["cluster", "question"], "n": a3.BOOTSTRAP_N, "seed": a3.SEED}
    assert pre["policies"] == {"base": a3.policy_fields(a3.BASE), "exp": a3.policy_fields(a3.EXP)}
    assert pre["controlled"]["swap_subtypes"] == list(a3.SWAP_SUBTYPES)
    assert pre["controlled"]["notation_types"] == list(a3.NOTATION_TYPES)
    assert pre["controlled"]["prompt_sha256"] == _sha("lab/evidence/prompts/controlled_writer_a3.md")
    assert pre["subject_check"]["generic_extra"] == list(subject.SUBJECT_GENERIC)
    assert pre["subject_check"]["heads"] == sorted(subject._HEADS)
    assert pre["claims"]["cap"] == a3.CLAIM_CAP == a2.CLAIM_CAP
    assert pre["claims"]["not_claim_phrases"] == list(claims.NOT_CLAIM_PHRASES)
    assert pre["claims"]["generic_nouns"] == list(lexical.GENERIC_NOUNS)
    assert pre["labels"]["kappa_stop"] == a3.KAPPA_MIN
    assert {"app/services/evidence/subject.py", "lab/evidence/a3.py", "lab/evidence/a2.py",
            "app/services/evidence/lexical.py", "app/services/evidence/judge.py"} <= set(pre["code_sha256"])
    assert pre["exploration"]["max_loss"] == a3.EXPLORE_MAX_LOSS
    assert {"all_pass", "h_swap_fail", "h_recall_fail", "h_prec_fail_only"} <= set(pre["product_mapping"])
    spec = (REPO / "docs/superpowers/specs/2026-10-04-subject-swap-a3-design.md").read_text()
    assert pre["exploration"]["allowed_changes"] in spec  # 탐색 허용 범위 문구가 spec과 같다
    for rel, sha in pre["code_sha256"].items():
        assert _sha(rel) == sha, rel


def test_a3_report_end_to_end_on_frozen_fake_data(tmp_path, monkeypatch):
    """동결 → a3-report: 같은 JEV 확률에서 a2-v1은 주체 교체를 ✅로, a3는 ❔로 둔다. 원자료·리포트·원장을 쓴다."""
    P = _P(tmp_path, "registered")
    code = tmp_path / "app/services/evidence/subject.py"
    code.parent.mkdir(parents=True)
    code.write_text("x")
    pre = json.loads(P.prereg.read_text())
    pre.update(code_sha256={"app/services/evidence/subject.py": hashlib.sha256(b"x").hexdigest()},
               policies={"base": a3.policy_fields(a3.BASE), "exp": a3.policy_fields(a3.EXP)},
               labels={"labelers": {"opus": "o", "codex": "c"}})
    P.prereg.write_text(json.dumps(pre))
    cli.write_jsonl(P.priv / "passages.jsonl", [
        {"id": "k-p1", "corp_code": "k", "text": "당사는 2006년 품질경영시스템 인증을 획득하였습니다."}])
    cli.write_jsonl(P.jsonl("questions.jsonl"), [{"qid": "k-q1", "corp_code": "k", "question": "q", "split": "check"}])
    cli.write_jsonl(P.jsonl("retrieval.jsonl"), [{"qid": "k-q1", "passage_ids": ["k-p1"]}])
    cli.write_jsonl(P.jsonl("answers.jsonl"), [{"qid": "k-q1", "answer": "a"}])
    claim_rows = [
        {"cid": "k-q1-n1", "qid": "k-q1", "source": "natural", "text": "케이산업은 2006년 인증을 획득했다."},
        {"cid": "k-q1-c1", "qid": "k-q1", "source": "controlled", "text": "케이산업은 2006년 인증을 받았다.",
         "variant": "의역", "expected": "supported"},
        {"cid": "k-q1-c2", "qid": "k-q1", "source": "controlled", "text": "고려제강은 2006년 인증을 획득했다.",
         "variant": "주체 교체:회사", "expected": "not_supported"}]
    cli.write_jsonl(P.jsonl("claims.jsonl"), claim_rows)
    r1 = lambda lb: {"opus": {"label": lb}, "codex": {"label": lb}}  # noqa: E731
    cli.write_jsonl(P.jsonl("labels.jsonl"), [
        {"cid": "k-q1-n1", "label": "supported", "r1": r1("supported"), "r2": None},
        {"cid": "k-q1-c1", "label": "supported", "r1": r1("supported"), "r2": None},
        {"cid": "k-q1-c2", "label": "no_evidence", "r1": r1("no_evidence"), "r2": None}])
    monkeypatch.setattr(cli, "_code_sha", lambda P: "code1")
    monkeypatch.setattr(cli, "_tree_dirty", lambda P: False)
    with pytest.raises(SystemExit, match="kappa"):  # 한 종류 라벨뿐이면 κ를 못 구해 멈춘다
        cli.write_jsonl(P.jsonl("labels.jsonl"), [
            {"cid": c["cid"], "label": "supported", "r1": r1("supported"), "r2": None} for c in claim_rows])
        cli.cmd_freeze_holdout(P, None)
    cli.write_jsonl(P.jsonl("labels.jsonl"), [
        {"cid": "k-q1-n1", "label": "supported", "r1": r1("supported"), "r2": None},
        {"cid": "k-q1-c1", "label": "supported", "r1": r1("supported"), "r2": None},
        {"cid": "k-q1-c2", "label": "no_evidence", "r1": r1("no_evidence"), "r2": None}])
    cli.cmd_freeze_holdout(P, None)
    cli.write_jsonl(P.priv / "scores/check.jsonl", [
        {"cid": c["cid"], "s": [0.9], "c": [0.0], "ok": True, "requests": 1} for c in claim_rows])
    cli.cmd_a3_report(P, None)
    res = json.loads((P.ev / "results/a3-check.json").read_text())
    assert res["gates"]["h_swap"]["base_accuracy"] == 0.0 and res["gates"]["h_swap"]["exp_accuracy"] == 1.0
    assert res["gates"]["h_recall"]["loss"] == 0.0 and res["recommendation"]["switch_default"] is False  # 표본 부족
    assert res["controlled"]["accuracy"]["exp"] == {"의역": 1.0, "주체 교체:회사": 1.0}
    md = (tmp_path / "docs/lab/evidence-a3-report.md").read_text()
    assert "AI 참조 라벨" in md and "a2-v1 유지" in md
    assert json.loads(P.attempts.read_text().splitlines()[-1])["event"] == "a3-report"
    pre["policies"]["exp"]["tau_s"] = 0.9  # 사전등록 정책과 제품 상수가 다르면 거부
    P.prereg.write_text(json.dumps(pre))
    with pytest.raises(SystemExit):
        cli.cmd_a3_report(P, None)


def test_a3_explore_reads_a2_tune_only(tmp_path):
    """확정 전 탐색: A-2 조정 세트의 저장 확률로 두 정책을 비교한다. 확인 세트를 열지 않고, 확정 뒤에는 거부한다."""
    P = _P(tmp_path)
    A2 = cli.Paths(tmp_path, "a2")
    A2.split_json.write_text(json.dumps({"companies": [
        {"corp_code": "t", "corp_name": "티산업", "split": "tune", "cluster": 0},
        {"corp_code": "x", "corp_name": "엑스", "split": "check", "cluster": 1}]}))
    cli.write_jsonl(A2.priv / "passages.jsonl", [{"id": "t-p1", "corp_code": "t", "text": "당사는 2006년 인증을 획득하였습니다."}])
    cli.write_jsonl(A2.jsonl("retrieval.jsonl"), [{"qid": "t-q1", "passage_ids": ["t-p1"]}])
    cli.write_jsonl(A2.jsonl("claims.jsonl"), [
        {"cid": "t-q1-n1", "qid": "t-q1", "source": "natural", "text": "티산업은 2006년 인증을 획득했다."},
        {"cid": "t-q1-n2", "qid": "t-q1", "source": "natural", "text": "고려제강은 2006년 인증을 획득했다."}])
    cli.write_jsonl(A2.jsonl("labels.jsonl"), [{"cid": "t-q1-n1", "label": "supported"},
                                               {"cid": "t-q1-n2", "label": "supported"}])
    cli.write_jsonl(A2.priv / "scores/tune.jsonl", [{"cid": c, "s": [0.9], "c": [0.0], "ok": True}
                                                    for c in ("t-q1-n1", "t-q1-n2")])
    cli.cmd_a3_explore(P, None)
    out = json.loads((P.priv / "a3_explore_tune.json").read_text())
    assert (out["positive"], out["base_supported_true"], out["lost"]) == (2, 2, 1)
    assert out["loss_rate"] == 0.5 and out["proceed"] is False and out["max_loss"] == a3.EXPLORE_MAX_LOSS
    assert [r["missing"] for r in out["removed"]] == [["고려제강"]]
    last = json.loads(P.attempts.read_text().splitlines()[-1])
    assert last["study"] == "a3" and last["loss_rate"] == 0.5 and last["proceed"] is False
    _P(tmp_path, "registered")
    with pytest.raises(SystemExit, match="draft"):
        cli.cmd_a3_explore(P, None)



def test_dart_self_aliases_from_company_info():
    """자기 회사 별칭은 DART 기업개황(company.json)의 종목명·영문명만 쓴다(근거 있는 소스)."""
    info = {"corp_name": "현대자동차", "stock_name": "현대차", "corp_name_eng": "Hyundai Motor Company"}
    assert cli.dart_self_aliases(info, "현대자동차") == ["현대차", "Hyundai Motor Company"]
    assert cli.dart_self_aliases({"stock_name": "현대자동차", "corp_name_eng": ""}, "현대자동차") == []


def test_self_names_use_split_aliases(tmp_path):
    P = _P(tmp_path, "registered")
    comps = [dict(COMPS[0], self_aliases=["케이", "K Industry Co., Ltd."]), COMPS[1]]
    P.split_json.write_text(json.dumps({"companies": comps}))
    assert cli.self_names(P) == {"k": ("케이산업", "케이", "K Industry Co., Ltd."), "m": ("엠소재",)}


def test_take_records_dart_aliases_only_when_asked(tmp_path, monkeypatch):
    from app.services.evidence import dart, passages

    P = _P(tmp_path, "registered")
    monkeypatch.setattr(dart, "list_annual_reports", lambda *a: [])
    monkeypatch.setattr(dart, "pick_annual_report", lambda items: {"rcept_no": "1", "report_nm": "사업보고서 (2025.12)"})
    monkeypatch.setattr(dart, "company_info", lambda *a: {"induty_code": "264", "stock_name": "케이",
                                                          "corp_name_eng": "K Co., Ltd."})
    doc = tmp_path / "d.xml"
    doc.write_text("x")
    monkeypatch.setattr(dart, "download_document", lambda *a: doc)
    monkeypatch.setattr(passages, "build_passages", lambda *a: [type("Pa", (), {"text": "가" * 4000})()])
    corp = dart.Corp("k", "케이산업", "000001")
    _, row, _ = cli._take(P, None, "key", corp, "random", aliases=True)
    assert row["self_aliases"] == ["케이", "K Co., Ltd."]
    _, row, _ = cli._take(P, None, "key", corp, "random")
    assert "self_aliases" not in row  # A-2 추첨 행은 그대로
