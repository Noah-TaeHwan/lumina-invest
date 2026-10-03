# tests/evidence/test_a2_cli.py
"""A-2 연구 분리(--study a2)와 신뢰 장치: 경로, 원장 표시, 확인 세트 봉인·동결·1회 실행, 사전등록 대조."""
import hashlib
import json
from pathlib import Path

import pytest

from app.services.evidence import claims, generate, judge
from lab.evidence import __main__ as cli
from lab.evidence import a2
from lab.evidence import split as sp

REPO = Path(__file__).resolve().parents[2]
COMPS = [{"corp_code": "t", "corp_name": "T", "split": "tune", "cluster": 0, "rcept_no": "1"},
         {"corp_code": "k", "corp_name": "K", "split": "check", "cluster": 1, "rcept_no": "2"}]


def _P(tmp_path):
    P = cli.Paths(tmp_path, "a2")
    P.split_json.parent.mkdir(parents=True, exist_ok=True)
    P.split_json.write_text(json.dumps({"companies": COMPS}))
    P.prereg.write_text(json.dumps({"study": "a2", "generator": {"model": "llama3.1:8b", "digest": "46e0c10c039e"}}))
    return P


def _tune_result(P):
    out = P.ev / "results/a2-tune.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"tau_s": 0.7, "tau_c": 0.35, "theta_low": 0.2, "theta_high": None}))


def _check_data(P):
    cli.write_jsonl(P.jsonl("questions.jsonl"), [{"qid": "k-q1", "corp_code": "k", "question": "q", "split": "check"}])
    cli.write_jsonl(P.jsonl("retrieval.jsonl"), [{"qid": "k-q1", "passage_ids": ["k-p"]}])
    cli.write_jsonl(P.jsonl("answers.jsonl"), [{"qid": "k-q1", "answer": "a"}])
    cli.write_jsonl(P.jsonl("claims.jsonl"), [{"cid": "k-q1-n1", "qid": "k-q1", "source": "natural", "text": "x"}])
    cli.write_jsonl(P.jsonl("labels.jsonl"), [{"cid": "k-q1-n1", "label": "supported"}])


def test_a2_paths_are_separate_and_ledger_is_shared(tmp_path):
    P1, P2 = cli.Paths(tmp_path), cli.Paths(tmp_path, "a2")
    assert P2.data == tmp_path / "lab/evidence/a2/data" and P2.priv == tmp_path / "lab/data/evidence_a2"
    assert P2.split_json == tmp_path / "lab/evidence/split_a2.json"
    assert P2.prereg == tmp_path / "lab/evidence/prereg_a2.json"
    assert P2.prereg_holdout == tmp_path / "lab/evidence/prereg_a2_check.json"
    assert P2.calls == tmp_path / "lab/data/evidence_a2/jev_calls.jsonl"
    assert P2.attempts == P1.attempts and P2.sealed == "check" and P1.sealed == "holdout"
    assert (P1.data, P1.priv, P1.split_json, P1.prereg) == (tmp_path / "lab/evidence/data", tmp_path / "lab/data/evidence",
                                                            tmp_path / "lab/evidence/split.json",
                                                            tmp_path / "lab/evidence/prereg.json")
    cli.log_attempt(P1, "x")
    cli.log_attempt(P2, "y", n=1)
    a, b = [json.loads(x) for x in P1.attempts.read_text().splitlines()]
    assert "study" not in a and b["study"] == "a2" and b["n"] == 1


def test_check_split_sealed_until_tune_result_then_frozen(tmp_path, monkeypatch):
    P = _P(tmp_path)
    assert [c["corp_code"] for c in cli.companies(P, "dev")] == ["t"]
    with pytest.raises(SystemExit, match="sealed"):
        cli.companies(P, "check", stage="data")
    _tune_result(P)
    assert [c["corp_code"] for c in cli.companies(P, "check", stage="data")] == ["k"]
    with pytest.raises(SystemExit, match="frozen"):
        cli.companies(P, "check")  # 판정은 동결 뒤
    _check_data(P)
    monkeypatch.setattr(cli, "_code_sha", lambda P: "code1")
    monkeypatch.setattr(cli, "_tree_dirty", lambda P: False)
    cli.cmd_freeze_holdout(P, None)
    frozen = json.loads(P.prereg_holdout.read_text())
    assert frozen["tune_sha256"] and frozen["models"]["generator"]
    assert [c["corp_code"] for c in cli.companies(P, "check")] == ["k"]
    with pytest.raises(SystemExit, match="frozen"):
        cli.companies(P, "check", stage="data")
    with pytest.raises(SystemExit, match="already"):
        cli.cmd_freeze_holdout(P, None)
    (P.ev / "results/a2-tune.json").write_text(json.dumps({"tau_s": 0.55}))  # 동결 뒤 임계값을 바꾸면
    with pytest.raises(SystemExit, match="mismatch"):
        cli.companies(P, "check")


def test_once_counts_only_same_study_ledger_lines(tmp_path):
    P = _P(tmp_path)
    cli.log_attempt(cli.Paths(tmp_path), "judge", split="check", tag="check")  # A-1 개발 분할 기록
    cli.once(P, tmp_path / "s.jsonl", "check", "judge", "check")
    cli.log_attempt(P, "judge", split="check", tag="check")
    with pytest.raises(SystemExit, match="already"):
        cli.once(P, tmp_path / "s.jsonl", "check", "judge", "check")
    cli.once(P, tmp_path / "s.jsonl", "tune", "judge", "tune")
    A1 = cli.Paths(tmp_path)
    cli.once(A1, tmp_path / "s.jsonl", "check", "judge", "check")  # A-1에서 check는 봉인 분할이 아니다


def test_tune_refused_once_check_data_exists(tmp_path):
    P = _P(tmp_path)
    _tune_result(P)
    _check_data(P)
    with pytest.raises(SystemExit, match="check data"):
        cli.guard_tune_open(P)


def test_study_only_commands(tmp_path):
    with pytest.raises(SystemExit, match="a1"):
        cli.main(["--root", str(tmp_path), "--study", "a2", "stage1-report"])
    with pytest.raises(SystemExit, match="a2"):
        cli.main(["--root", str(tmp_path), "a2-tune"])


def test_natural_claims_a2_keep_rule_flags_and_cap_eight():
    sents = [f"회사는 제품 {i}번을 만든다." for i in range(1, 11)]
    answer = "알 수 없습니다. " + " ".join(sents)
    out = a2.natural_claims(answer_rows=[{"qid": "x-q1", "answer": answer}])
    assert out[0]["not_claim_rule"] is True and out[0]["cid"] == "x-q1-n1"
    kept = [c for c in out if not c["not_claim_rule"]]
    assert [c["text"] for c in kept] == sents[:8]  # 비주장을 뺀 앞 8개(제품 주장 상한과 같다)
    assert all(answer[c["start"]:c["end"]] == c["text"] for c in out)
    assert out[1]["cid"] == "x-q1-n2" and out[1]["source"] == "natural" and out[1]["expected"] is None


def test_prereg_code_hash_guard(tmp_path):
    P = _P(tmp_path)
    f = tmp_path / "app/services/evidence/lexical.py"
    f.parent.mkdir(parents=True)
    f.write_text("x")
    P.prereg.write_text(json.dumps({"code_sha256": {"app/services/evidence/lexical.py": hashlib.sha256(b"x").hexdigest()}}))
    cli.verify_prereg_code(P)
    f.write_text("y")
    with pytest.raises(SystemExit, match="lexical.py"):
        cli.verify_prereg_code(P)


def _sha(rel):
    return hashlib.sha256((REPO / rel).read_bytes()).hexdigest()


def test_committed_prereg_a2_matches_code():
    """사전등록 파일의 값이 코드 상수·해시와 같다(사전등록 뒤 코드가 바뀌면 여기서 먼저 드러난다)."""
    pre = json.loads((REPO / "lab/evidence/prereg_a2.json").read_text())
    assert pre["study"] == "a2" and pre["judge_question_sha"] == judge.QUESTION_SHA
    assert pre["generator"]["prompt_sha"] == generate.PROMPT_SHA and pre["generator"]["options"] == generate.OPTIONS
    assert (pre["generator"]["model"], pre["generator"]["digest"]) == ("llama3.1:8b", "46e0c10c039e")
    assert pre["embedder"]["digest"] == "0a109f422b47"
    assert pre["draw"]["seed"] == sp.A2_SEED == 20261103 and pre["draw"]["random_n"] == sp.A2_RANDOM_N
    assert pre["split"]["check_cap"] == sp.A2_CHECK_CAP
    sel = pre["selection"]
    assert sel["tau_s"]["candidates"] == a2.TAU_S_GRID and sel["tau_s"]["min_precision"] == a2.TAU_S_MIN_PRECISION
    assert sel["tau_s"]["min_predicted"] == a2.MIN_PREDICTED and sel["tau_s"]["fallback"] == a2.TAU_S_FALLBACK
    assert sel["tau_c"] == a2.TAU_C and sel["theta"]["candidates"] == a2.THETA_GRID
    assert sel["theta"]["low_max_supported_rate"] == a2.THETA_LOW_MAX_SUPPORTED
    assert sel["theta"]["high_min_precision"] == a2.THETA_HIGH_MIN_PRECISION
    assert sel["theta"]["high_min_claims"] == a2.MIN_HIGH_BAND
    assert pre["bootstrap"] == {"levels": ["cluster", "question"], "n": a2.BOOTSTRAP_N, "seed": a2.SEED}
    assert pre["token_cap"] == a2.TOKEN_CAP == 6_000_000
    assert pre["gates"] == a2.GATES
    assert pre["claims"]["cap"] == a2.CLAIM_CAP
    rule = pre["claims"]["not_claim_rule"]
    assert rule["phrases"] == list(claims.NOT_CLAIM_PHRASES) and rule["lead"]["ends"] == list(claims.NOT_CLAIM_LEAD_ENDS)
    assert pre["provisional_policy"]["version"] == "a2-provisional-2"
    for rel, sha in pre["code_sha256"].items():
        assert _sha(rel) == sha, rel
