# tests/evidence/test_a4_cli.py
"""A-4 연구 분리(--study a4)와 신뢰 장치: 경로, 확정 전 봉인, 9칸 배정 통제 주장, 1a 실측(호출 0), 탐색(A-2 조정·A-3 확인
세트만 읽고 A-3 원장·결과에 쓰지 않음, 별도 원장), 예산 사전 멈춤, 상한 도달 시 지표 계산 거부, 사전등록 대조, 가짜 데이터로
동결 → a4-report. 외부 호출 없음(JEV는 httpx MockTransport)."""
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from app.lib import jev
from app.services.evidence import claims, generate, judge, lexical, subject, subject_a4
from lab.evidence import __main__ as cli
from lab.evidence import a2, a3, a4

REPO = Path(__file__).resolve().parents[2]
COMPS = [{"corp_code": "k", "corp_name": "케이산업", "split": "check", "cluster": 0, "rcept_no": "2"},
         {"corp_code": "m", "corp_name": "엠소재", "split": "check", "cluster": 1, "rcept_no": "3"}]
SAME = {"same_subject": 0.9, "different_subject": 0.05, "unclear": 0.05}
DIFF = {"same_subject": 0.1, "different_subject": 0.8, "unclear": 0.1}
A = type("A", (), {"split": "check", "tag": "check", "limit": 0, "single": False, "no_cache": False,
                   "prompt_version": "v1", "context": "candidates"})


def _corpcode(P, *names):
    rows = "".join(f"<list><corp_code>{i:08d}</corp_code><corp_name>{n}</corp_name><corp_eng_name></corp_eng_name>"
                   f"<stock_code>{i:06d}</stock_code></list>" for i, n in enumerate(names, 1))
    P.priv.mkdir(parents=True, exist_ok=True)
    (P.priv / "corpCode.xml").write_text(f"<result>{rows}</result>", encoding="utf-8")


def _P(tmp_path, status="draft", **extra):
    P = cli.Paths(tmp_path, "a4")
    P.split_json.parent.mkdir(parents=True, exist_ok=True)
    P.split_json.write_text(json.dumps({"companies": COMPS}))
    pre = {"study": "a4", "status": status, "token_cap": 9_000_000,
           "generator": {"model": "llama3.1:8b", "digest": "46e0c10c039e"},
           "exploration": {"token_cap": 200_000}, "budget": {"followup_tokens_p95_estimate": 2000,
                                                             "followup_ratio": 0.65}, **extra}
    P.prereg.write_text(json.dumps(pre))
    return P


SEL = {"version": "v1", "context": "candidates", "signal": "p_diff", "tau_d": 0.5}
REG = {"tau_d": 0.5, "signal": "p_diff", "fallbacks": [],
       "subject_question": {"sha": subject_a4.SUBJECT_QUESTION_SHA, "version": "v1", "context": "candidates"}}


def _proceed_line(P, proceed=True, **sel):
    """a4-proceed 원장 줄(선택·진행 기준). 확정·추첨·리포트가 이것과 대조한다."""
    cli.log_attempt(P, "a4-proceed", selection={**SEL, **sel}, proceed=proceed)


def _mock_client(answer, counter):
    """주 판정(p*)·후속(s*) 질문에 답하는 JEV 가짜 전송. answer(qid) → 확률."""
    def handle(request):
        body = json.loads(request.content)
        counter.append(sorted(body["questions"]))
        return httpx.Response(200, json={"model": jev.MODEL, "usage": {"input_tokens": 1000},
                                         "answers": {q: {"probabilities": answer(q)} for q in body["questions"]}})
    return httpx.Client(transport=httpx.MockTransport(handle))


def _use_mock(monkeypatch, answer, counter):
    monkeypatch.setattr(cli, "_jev_client", lambda path, cap: jev.JevClient(path, cap, client=_mock_client(
        answer, counter), api_key="test"))


def test_a4_paths_are_separate(tmp_path):
    P = cli.Paths(tmp_path, "a4")
    assert P.data == tmp_path / "lab/evidence/study_a4" and P.priv == tmp_path / "lab/data/evidence_a4"
    assert P.split_json == tmp_path / "lab/evidence/split_a4.json"
    assert (P.prereg, P.prereg_holdout) == (tmp_path / "lab/evidence/prereg_a4.json",
                                            tmp_path / "lab/evidence/prereg_a4_check.json")
    assert P.calls == tmp_path / "lab/data/evidence_a4/jev_calls.jsonl" and P.sealed == "check"
    assert P.explore_calls == tmp_path / "lab/data/evidence_a4/jev_calls_explore.jsonl"
    assert P.attempts == cli.Paths(tmp_path).attempts
    assert cli.companies(_P(tmp_path), "dev") == []


def test_check_data_sealed_until_prereg_registered(tmp_path):
    P = _P(tmp_path)
    with pytest.raises(SystemExit, match="registered"):
        cli.companies(P, "check", stage="data")
    with pytest.raises(SystemExit, match="registered"):
        cli.cmd_split(P, None)
    _P(tmp_path, "registered")
    assert [c["corp_code"] for c in cli.companies(P, "check", stage="data")] == ["k", "m"]


def test_study_only_commands(tmp_path):
    for cmd in ("a4-measure", "a4-explore", "a4-report"):
        with pytest.raises(SystemExit, match="a4"):
            cli.main(["--root", str(tmp_path), "--study", "a3", cmd])
    with pytest.raises(SystemExit, match="a3"):
        cli.main(["--root", str(tmp_path), "--study", "a4", "a3-report"])


def test_split_a4_draws_45_excluding_120_prior(tmp_path, monkeypatch):
    P = _P(tmp_path, "registered", **REG)
    seen = {}
    monkeypatch.setattr(cli, "verify_prereg_code", lambda P: None)
    with pytest.raises(SystemExit, match="a4-proceed"):  # 진행 기준 선택 없이 추첨하지 않는다
        cli.cmd_split(P, None)
    _proceed_line(P, tau_d=0.4)
    with pytest.raises(SystemExit, match="a4-proceed"):  # 사전등록 τ_d가 선택과 다르면 거부
        cli.cmd_split(P, None)
    _proceed_line(P)
    monkeypatch.setattr(cli, "prior_texts", lambda P, studies: seen.setdefault("studies", studies) and ({}, {}))
    monkeypatch.setattr(cli, "_draw_random", lambda P, n_, b_, **kw: seen.update(kw))
    cli.cmd_split(P, None)
    assert seen["studies"] == ("a1", "a2", "a3")
    assert (seen["seed"], seen["n"], seen["check_cap"], seen["aliases"]) == (20261305, 45, 45, True)
    assert set(seen["meta"]) == {"a1_split_sha256", "a2_split_sha256", "a3_split_sha256"}


def _controlled_inputs(P, qids=("k-q1",)):
    cli.write_jsonl(P.priv / "passages.jsonl", [
        {"id": "k-p1", "corp_code": "k", "text": "당사는 2006년 품질경영시스템 인증을 획득하였습니다."},
        {"id": "k-p2", "corp_code": "k", "text": "㈜한빛소재와 공급계약을 체결하였습니다."}])
    cli.write_jsonl(P.jsonl("questions.jsonl"), [{"qid": q, "corp_code": "k", "question": "q", "split": "check"}
                                                 for q in qids])
    cli.write_jsonl(P.jsonl("retrieval.jsonl"), [{"qid": q, "passage_ids": ["k-p1", "k-p2"]} for q in qids])


def test_controlled_uses_nine_slot_cycle_and_a3_prompt(tmp_path):
    P = _P(tmp_path, "registered")
    qids = [f"k-q{i}" for i in range(1, 10)]
    _controlled_inputs(P, qids)
    (P.ev / "prompts").mkdir(parents=True)
    (P.ev / "prompts/controlled_writer_a3.md").write_text("A3 지시")
    cli.cmd_controlled_packets(P, A)
    out = (P.priv / "packets/controlled/k.md").read_text()
    assert out.startswith("A3 지시") and "## k-q9 — 주체 교체 하위 유형: 문단 안 교체" in out
    assert "## k-q5 — 주체 교체 하위 유형: 회사" in out  # A-3 5칸이면 k-q5는 문단 안 교체였다
    rows = []
    for q in qids:
        sub, nt = a4.assigned(q, qids)
        rows.append({"qid": q, "true_text": "한빛소재와 공급 계약을 맺었다.", "swap_subtype": sub, "notation_type": nt,
                     "swap_text": "한빛소재와 공급 계약을 맺었다." if sub == a4.IN_PASSAGE else "두리소재와 공급 계약을 맺었다.",
                     "notation_text": "한빛소재 주식회사와 공급 계약을 맺었다." if nt != "영문·약칭" else None,
                     "source_passage": 2})
    cli.write_jsonl(P.data / "controlled" / "k.jsonl", rows)
    cli.cmd_controlled_check(P, A)
    got = [(r["cid"], r["variant"]) for r in cli.read_jsonl(P.jsonl("claims.jsonl")) if r["cid"].endswith("c2")]
    assert ("k-q2-c2", "주체 교체:부문·사업") in got and ("k-q6-c2", "주체 교체:부문·사업") in got


# --- 탐색용 A-2 조정·A-3 확인 세트 픽스처 -------------------------------------------------------------------------
def _explore_fixture(tmp_path):
    A2 = cli.Paths(tmp_path, "a2")
    A2.split_json.parent.mkdir(parents=True, exist_ok=True)
    A2.split_json.write_text(json.dumps({"companies": [
        {"corp_code": "t", "corp_name": "티산업", "split": "tune", "cluster": 0},
        {"corp_code": "x", "corp_name": "엑스", "split": "check", "cluster": 1}]}))
    cli.write_jsonl(A2.priv / "passages.jsonl", [
        {"id": "t-p1", "corp_code": "t", "text": "당사는 2006년 인증을 획득하였습니다."},
        {"id": "x-p1", "corp_code": "x", "text": "당사는 2007년 인증을 획득하였습니다."}])
    cli.write_jsonl(A2.jsonl("retrieval.jsonl"), [{"qid": "t-q1", "passage_ids": ["t-p1"]},
                                                  {"qid": "x-q1", "passage_ids": ["x-p1"]}])
    cli.write_jsonl(A2.jsonl("claims.jsonl"), [
        {"cid": "t-q1-n1", "qid": "t-q1", "source": "natural", "text": "티산업은 2006년 인증을 획득했다."},
        {"cid": "x-q1-n1", "qid": "x-q1", "source": "natural", "text": "엑스는 2007년 인증을 획득했다."}])
    cli.write_jsonl(A2.jsonl("labels.jsonl"), [{"cid": "t-q1-n1", "label": "supported"},
                                               {"cid": "x-q1-n1", "label": "supported"}])
    cli.write_jsonl(A2.priv / "scores/tune.jsonl", [{"cid": "t-q1-n1", "s": [0.9], "c": [0.0], "ok": True}])
    _corpcode(A2, "티산업", "고려제강")

    A3 = cli.Paths(tmp_path, "a3")
    nm = lambda i: f"씨{chr(0xAC00 + 28 * i)}산업"  # noqa: E731 — 이름에 숫자가 없게(숫자 확인)
    comps = [{"corp_code": f"c{i}", "corp_name": nm(i), "split": "check", "cluster": i} for i in range(25)]
    A3.split_json.write_text(json.dumps({"companies": comps, "clusters": [[c["corp_code"]] for c in comps]}))
    cli.write_jsonl(A3.priv / "passages.jsonl", [
        {"id": f"c{i}-p1", "corp_code": f"c{i}", "text": "커피 부문은 2006년 인증을 획득하였습니다."} for i in range(25)])
    cli.write_jsonl(A3.jsonl("retrieval.jsonl"), [{"qid": f"c{i}-q1", "passage_ids": [f"c{i}-p1"]} for i in range(25)])
    rows, labels, scores = [], [], []
    for i in range(25):
        rows += [{"cid": f"c{i}-q1-n1", "qid": f"c{i}-q1", "source": "natural", "text": f"{nm(i)}은 2006년 인증을 획득했다."},
                 {"cid": f"c{i}-q1-c2", "qid": f"c{i}-q1", "source": "controlled", "variant": "주체 교체:부문·사업",
                  "expected": "not_supported", "text": "건설 부문은 2006년 인증을 획득했다."}]
        labels += [{"cid": f"c{i}-q1-n1", "label": "supported"}, {"cid": f"c{i}-q1-c2", "label": "no_evidence"}]
        scores += [{"cid": f"c{i}-q1-n1", "s": [0.9], "c": [0.0], "ok": True},
                   {"cid": f"c{i}-q1-c2", "s": [0.9], "c": [0.0], "ok": True}]
    cli.write_jsonl(A3.jsonl("claims.jsonl"), rows)
    cli.write_jsonl(A3.jsonl("labels.jsonl"), labels)
    cli.write_jsonl(A3.priv / "scores/check.jsonl", scores)
    cli.write_jsonl(A3.calls, [{"key": "k", "ok": True, "answers": {}, "input_tokens": 5}])
    (A3.ev / "results").mkdir(parents=True, exist_ok=True)
    (A3.ev / "results/a3-check.json").write_text("{}")
    _corpcode(A3, "고려제강")
    return A2, A3


def _snap(*paths):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def test_measure_counts_without_calls_and_logs_half_split(tmp_path):
    P = _P(tmp_path)
    A2, A3 = _explore_fixture(tmp_path)
    cli.cmd_a4_measure(P, A)
    assert not P.explore_calls.exists() and not P.calls.exists()  # 호출 0
    lines = [json.loads(x) for x in P.attempts.read_text().splitlines()]
    half = next(x for x in lines if x["event"] == "a4-half-split")
    assert half["study"] == "a4" and half["seed"] == 20261306 and half["design_companies"] >= 20
    assert len(half["design"]) + len(half["check"]) == 25
    m = next(x for x in lines if x["event"] == "a4-measure")
    assert m["sets"]["a2-tune"]["claims"] == 1 and m["sets"]["a2-tune"]["followup"] == 1
    assert m["all"]["m_mean"] == 1.0 and m["all"]["followup_ratio"] == 1.0
    assert lines.index(half) < lines.index(m)
    assert half["sha256"] == m["half_split_sha256"] and len(half["sha256"]) == 64
    cli.cmd_a4_measure(P, A)  # 다시 돌려도 반분은 같아야 한다(원장의 첫 반분과 대조)
    _P(tmp_path, exploration={"token_cap": 200_000, "half_split": {"sha256": "0" * 64}})
    with pytest.raises(SystemExit, match="half split"):  # 사전등록에 적은 반분 해시와 다르면 거부
        cli.cmd_a4_measure(P, A)
    _P(tmp_path, exploration={"token_cap": 200_000, "half_split": {"sha256": half["sha256"]}})
    cli.cmd_a4_measure(P, A)
    _P(tmp_path, "registered")
    with pytest.raises(SystemExit, match="draft"):
        cli.cmd_a4_measure(P, A)


def test_explore_reads_tune_and_a3_check_only_and_writes_own_ledger(tmp_path, monkeypatch):
    P = _P(tmp_path)
    A2, A3 = _explore_fixture(tmp_path)
    protected = [A3.calls, A3.ev / "results/a3-check.json", A3.priv / "scores/check.jsonl", A2.priv / "scores/tune.jsonl"]
    before = _snap(*protected)
    seen = []
    _use_mock(monkeypatch, lambda q: DIFF if q.startswith("s") else {"supports": 0.9, "contradicts": 0.0,
                                                                    "says_nothing": 0.1}, seen)
    with pytest.raises(SystemExit, match="a4-measure"):
        cli.cmd_a4_explore(P, A)
    cli.cmd_a4_measure(P, A)
    cli.cmd_a4_explore(P, A)
    assert _snap(*protected) == before and not (A3.priv / "explore").exists()
    assert all(q[0].startswith("s") for q in seen) and len(seen) == 51  # 후속 호출만(A-2 조정 1 + A-3 자연 25·교체 25)
    assert len(cli.read_jsonl(P.explore_calls)) == 51 and not P.calls.exists()
    out = json.loads((P.priv / "a4_explore_v1-candidates.json").read_text())
    assert len(out["grid"]) == 12 and out["n"]["a2-tune"] == 1  # x(확인 세트)는 읽지 않는다
    assert out["selected"] is None and "proceed" not in out  # 지지 주장을 모두 잃어 해당 없음
    # F1: 탐색은 설계용 절반만 계산·출력·기록한다(점검용은 a4-proceed 한 번만)
    assert all(set(g["arms"][arm]) == {"design"} for g in out["grid"] for arm in ("C", "A", "B"))
    assert all(x["set"] != "a3-check" for x in out["audit"]["natural"] + out["audit"]["swaps"])
    last = json.loads(P.attempts.read_text().splitlines()[-1])
    assert last["event"] == "a4-explore" and last["prompt_version"] == "v1" and len(last["grid"]) == 12
    assert "proceed" not in last and not any(k.endswith("_check") for g in last["grid"] for k in g)
    assert last["subject_question_sha"] == subject_a4.SUBJECT_QUESTION_SHA
    assert set(last["code_sha256"]) == {"app/services/evidence/subject_a4.py", "lab/evidence/a4.py"}  # F7
    assert last["q_sha256"] == hashlib.sha256((P.priv / "explore_q/v1-candidates.jsonl").read_bytes()).hexdigest()
    n_calls = len(seen)
    cli.cmd_a4_explore(P, A)  # 같은 판을 다시: 캐시만(유료 호출 0)
    assert len(seen) == n_calls


def test_explore_budget_prestop_and_cap_refuses_metrics(tmp_path, monkeypatch):
    P = _P(tmp_path, exploration={"token_cap": 10_000})
    _explore_fixture(tmp_path)
    seen = []
    _use_mock(monkeypatch, lambda q: SAME, seen)
    cli.cmd_a4_measure(P, A)
    with pytest.raises(SystemExit, match="token budget"):  # 51 × 2,000 > 10,000
        cli.cmd_a4_explore(P, A)
    assert seen == []
    P = _P(tmp_path, exploration={"token_cap": 3_000_001})
    with pytest.raises(SystemExit, match="3,000,000"):
        cli.cmd_a4_explore(P, A)
    P = _P(tmp_path, exploration={"token_cap": 200_000})
    monkeypatch.setattr(cli, "_jev_client", lambda path, cap: jev.JevClient(path, 5_000, client=_mock_client(
        lambda q: SAME, seen), api_key="t"))
    with pytest.raises(SystemExit, match="incomplete"):
        cli.cmd_a4_explore(P, A)
    assert not (P.priv / "a4_explore_v1-candidates.json").exists()  # 상한 도달: 지표 계산 안 함
    assert json.loads(P.attempts.read_text().splitlines()[-1])["event"] == "a4-explore-incomplete"


def test_explore_refuses_fourth_prompt_version_and_after_registration(tmp_path, monkeypatch):
    P = _P(tmp_path)
    _explore_fixture(tmp_path)
    cli.cmd_a4_measure(P, A)
    for v, ctx in (("v1", "candidates"), ("v2", "candidates"), ("v2", "all")):
        cli.log_attempt(P, "a4-explore", prompt_version=v, context=ctx)
    monkeypatch.setitem(subject_a4.PROMPTS, "v3", subject_a4.PROMPTS["v1"])
    with pytest.raises(SystemExit, match="exceed 3"):  # F3: (판, 문맥) 쌍으로 센다
        cli.cmd_a4_explore(P, type("B", (A,), {"prompt_version": "v3"}))
    _P(tmp_path, "registered")
    with pytest.raises(SystemExit, match="draft"):
        cli.cmd_a4_explore(P, A)


def test_context_all_needs_ruling_line_with_cause_ma_majority(tmp_path, monkeypatch):
    """F3: '문단 8개 전부'(--context all)는 설계용 절반 C 손실 중 원인 (마) 건수 ruling 줄이 있고 절반 이상일 때만."""
    P = _P(tmp_path, exploration={"token_cap": 300_000})
    _explore_fixture(tmp_path)
    seen = []
    _use_mock(monkeypatch, lambda q: SAME, seen)
    cli.cmd_a4_measure(P, A)
    ALL = type("B", (A,), {"context": "all"})
    with pytest.raises(SystemExit, match="ruling"):
        cli.cmd_a4_explore(P, ALL)
    cli.log_attempt(P, "ruling", topic="context-all", design_c_loss=4, cause_ma=1)
    with pytest.raises(SystemExit, match="ruling"):
        cli.cmd_a4_explore(P, ALL)
    cli.log_attempt(P, "ruling", topic="context-all", design_c_loss=4, cause_ma=2)
    cli.cmd_a4_explore(P, ALL)
    assert seen and all(len(q) for q in seen)
    assert json.loads(P.attempts.read_text().splitlines()[-1])["context"] == "all"


def _explore_once(tmp_path, monkeypatch, answer=lambda q: SAME):
    P = _P(tmp_path)
    _explore_fixture(tmp_path)
    seen = []
    _use_mock(monkeypatch, answer, seen)
    cli.cmd_a4_measure(P, A)
    return P, seen


def test_proceed_runs_once_over_all_explored_versions(tmp_path, monkeypatch):
    """F1: a4-proceed는 한 번만. 탐색한 모든 (판, 문맥)의 설계용 격자 합본에서 6.2 규칙으로 고르고, 그 선택에만 점검용 절반·
    진행 기준을 계산한다. 모든 조합의 격자(점검용 포함)를 공개한다. 이 뒤에는 a4-explore가 거부된다."""
    P, seen = _explore_once(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="a4-explore"):
        cli.cmd_a4_proceed(P, A)
    cli.cmd_a4_explore(P, A)
    n_calls = len(seen)
    cli.cmd_a4_proceed(P, A)
    assert len(seen) == n_calls  # 호출 없음
    last = json.loads(P.attempts.read_text().splitlines()[-1])
    assert last["event"] == "a4-proceed"
    assert last["selection"] == {"version": "v1", "context": "candidates", "signal": "p_diff", "tau_d": 0.7}
    assert last["proceed"] is False and last["criteria"]["swap"]["accuracy"] == 0.0  # 점검용 교체를 하나도 못 막음
    assert set(last["grids"]) == {"v1-candidates"}
    assert {h for g in last["grids"]["v1-candidates"] for h in g if h.startswith("C_")} == {"C_design", "C_check"}
    out = json.loads((P.priv / "a4_proceed.json").read_text())
    assert out["selection"] == last["selection"] and all(x["set"] != "a3-check" for x in out["audit"]["natural"])
    with pytest.raises(SystemExit, match="already ran"):
        cli.cmd_a4_proceed(P, A)
    with pytest.raises(SystemExit, match="a4-proceed"):
        cli.cmd_a4_explore(P, A)


def test_proceed_refuses_if_explore_answers_changed(tmp_path, monkeypatch):
    P, _ = _explore_once(tmp_path, monkeypatch)
    cli.cmd_a4_explore(P, A)
    q = P.priv / "explore_q/v1-candidates.jsonl"
    q.write_text(q.read_text().replace("0.9", "0.8"))
    with pytest.raises(SystemExit, match="changed"):
        cli.cmd_a4_proceed(P, A)
    assert not any(json.loads(x)["event"] == "a4-proceed" for x in P.attempts.read_text().splitlines())


def test_explore_requires_concluded_a3(tmp_path):
    P = _P(tmp_path)
    _, A3 = _explore_fixture(tmp_path)
    (A3.ev / "results/a3-check.json").unlink()
    with pytest.raises(SystemExit, match="a3-check.json"):
        cli.cmd_a4_measure(P, A)
    assert not P.attempts.exists()  # 실패한 실측은 원장에 반분을 남기지 않는다
    (A3.ev / "results/a3-check.json").write_text("{}")
    (A3.priv / "corpCode.xml").unlink()
    with pytest.raises(SystemExit, match="corpCode"):
        cli.cmd_a4_measure(P, A)
    assert not P.attempts.exists()


# --- 확인 판정·동결·리포트 ------------------------------------------------------------------------------------
def _sha(rel):
    return hashlib.sha256((REPO / rel).read_bytes()).hexdigest()


def _check_fixture(tmp_path, monkeypatch):
    exp = replace(a4.EXP, tau_d=0.5, subject_signal="p_diff")
    monkeypatch.setattr(a4, "EXP", exp)
    P = _P(tmp_path, "registered", **REG,
           policies={"base": a4.policy_fields(a4.BASE), "exp": a4.policy_fields(exp), "a3": a4.policy_fields(a4.A3)},
           labels={"labelers": {"opus": "o", "codex": "c"}},
           budget={"followup_tokens_p95_estimate": 2000, "followup_ratio": 0.65})
    _proceed_line(P)
    code = tmp_path / "app/services/evidence/subject_a4.py"
    code.parent.mkdir(parents=True)
    code.write_text("x")
    pre = json.loads(P.prereg.read_text())
    pre["code_sha256"] = {"app/services/evidence/subject_a4.py": hashlib.sha256(b"x").hexdigest()}
    P.prereg.write_text(json.dumps(pre))
    cli.write_jsonl(P.priv / "passages.jsonl", [
        {"id": "k-p1", "corp_code": "k", "text": "당사는 2006년 품질경영시스템 인증을 획득하였습니다."},
        {"id": "k-p2", "corp_code": "k", "text": "커피 부문은 커피머신을 판매합니다."}])
    cli.write_jsonl(P.jsonl("questions.jsonl"), [{"qid": "k-q1", "corp_code": "k", "question": "q", "split": "check"}])
    cli.write_jsonl(P.jsonl("retrieval.jsonl"), [{"qid": "k-q1", "passage_ids": ["k-p1", "k-p2"]}])
    cli.write_jsonl(P.jsonl("answers.jsonl"), [{"qid": "k-q1", "answer": "a"}])
    _corpcode(P, "고려제강", "케이산업")
    rows = [{"cid": "k-q1-n1", "qid": "k-q1", "source": "natural", "text": "케이산업은 2006년 인증을 획득했다."},
            {"cid": "k-q1-c1", "qid": "k-q1", "source": "controlled", "text": "케이산업은 2006년 인증을 받았다.",
             "variant": "의역", "expected": "supported"},
            {"cid": "k-q1-c2", "qid": "k-q1", "source": "controlled", "text": "건설 부문은 2006년 인증을 획득했다.",
             "variant": "주체 교체:부문·사업", "expected": "not_supported"}]
    cli.write_jsonl(P.jsonl("claims.jsonl"), rows)
    r1 = lambda lb, lb2=None: {"opus": {"label": lb}, "codex": {"label": lb2 or lb}}  # noqa: E731
    cli.write_jsonl(P.jsonl("labels.jsonl"), [
        {"cid": "k-q1-n1", "label": "supported", "r1": r1("supported"), "r2": None},
        {"cid": "k-q1-c1", "label": "supported", "r1": r1("supported"), "r2": None},
        {"cid": "k-q1-c2", "label": "no_evidence", "r1": r1("no_evidence"), "r2": None}])
    monkeypatch.setattr(cli, "_code_sha", lambda P: "code1")
    monkeypatch.setattr(cli, "_tree_dirty", lambda P: False)
    return P


def test_freeze_needs_swap_tags_then_judge_and_report_end_to_end(tmp_path, monkeypatch):
    P = _check_fixture(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="swap tag"):
        cli.cmd_freeze_holdout(P, None)
    cli.write_jsonl(P.jsonl("swap_tags.jsonl"), [{"cid": "k-q1-c2", "tag": "일반명사"}])
    cli.cmd_freeze_holdout(P, None)
    assert "swap_tags_sha256" in json.loads(P.prereg_holdout.read_text())
    seen = []
    answer = lambda q: (DIFF if q.startswith("s") else  # noqa: E731
                        {"supports": 0.9, "contradicts": 0.0, "says_nothing": 0.1})
    _use_mock(monkeypatch, answer, seen)
    cli.cmd_judge(P, A)
    sc = {r["cid"]: r for r in cli.read_jsonl(P.priv / "scores/check.jsonl")}
    assert sc["k-q1-c2"]["q"] == {"0": DIFF} and sc["k-q1-c2"]["m"] == 1  # 2번 문단은 숫자 확인 실패
    assert [q[0][0] for q in seen].count("s") == 3 and len(seen) == 6
    tags = {r["tag"] for r in cli.read_jsonl(P.calls)}
    assert tags == {"check", "check-followup"}
    cli.cmd_a4_report(P, None)
    res = json.loads((P.ev / "results/a4-check.json").read_text())
    g = res["gates"]
    assert g["h_swap"]["accuracy"] == 1.0 and g["h_swap"]["base_accuracy"] == 0.0
    assert g["h_swap"]["by_subtype"]["부문·사업"]["by_tag"]["일반명사"]["n"] == 1
    assert res["recommendation"]["switch_default"] is False  # 표본 부족 → 실패
    assert res["removed"] == [{"cid": "k-q1-n1", "label": "supported", "cause": "question", "p_diff": 0.8}]
    assert res["cost"]["followup_ratio"] == 1.0
    md = (tmp_path / "docs/lab/evidence-a4-report.md").read_text()
    assert "AI 참조 라벨" in md and "a2-v1 유지" in md and "보조 팔" in md
    assert json.loads(P.attempts.read_text().splitlines()[-1])["event"] == "a4-report"
    with pytest.raises(SystemExit, match="already ran"):
        cli.cmd_judge(P, A)
    _proceed_line(P, version="v2")  # 사전등록 판이 a4-proceed 선택과 다르면 거부
    with pytest.raises(SystemExit, match="a4-proceed"):
        cli.cmd_a4_report(P, None)
    _proceed_line(P)
    monkeypatch.setattr(a4, "EXP", replace(a4.EXP, tau_d=0.6))  # 사전등록 τ_d와 제품 상수가 다르면 거부
    with pytest.raises(SystemExit, match="policy"):
        cli.cmd_a4_report(P, None)


def test_judge_budget_prestop_and_incomplete_report_refused(tmp_path, monkeypatch):
    P = _check_fixture(tmp_path, monkeypatch)
    cli.write_jsonl(P.jsonl("swap_tags.jsonl"), [{"cid": "k-q1-c2", "tag": "고유명"}])
    cli.cmd_freeze_holdout(P, None)
    monkeypatch.setattr(cli, "verify_freeze", lambda P: None)  # 아래에서 상한만 바꿔 본다(동결 대조는 다른 테스트)
    pre = json.loads(P.prereg.read_text())
    pre["token_cap"] = 10_000  # 3 × 5,070 + 3 × 0.75 × 2,000 > 10,000
    P.prereg.write_text(json.dumps(pre))
    seen = []
    _use_mock(monkeypatch, lambda q: SAME if q.startswith("s") else {"supports": 0.9, "contradicts": 0.0,
                                                                    "says_nothing": 0.1}, seen)
    with pytest.raises(SystemExit, match="token budget"):
        cli.cmd_judge(P, A)
    assert seen == []
    pre["token_cap"] = 9_600_001
    P.prereg.write_text(json.dumps(pre))
    with pytest.raises(SystemExit, match="9,600,000"):
        cli.cmd_judge(P, A)
    pre["token_cap"] = 30_000
    P.prereg.write_text(json.dumps(pre))
    monkeypatch.setattr(cli, "_jev_client", lambda path, cap: jev.JevClient(path, 1_500, client=_mock_client(
        lambda q: SAME if q.startswith("s") else {"supports": 0.9, "contradicts": 0.0, "says_nothing": 0.1}, seen),
        api_key="t"))
    with pytest.raises(SystemExit, match="incomplete"):
        cli.cmd_judge(P, A)
    assert (P.priv / "scores/check.partial.jsonl").exists() and not (P.priv / "scores/check.jsonl").exists()
    with pytest.raises(SystemExit, match="incomplete"):
        cli.cmd_a4_report(P, None)  # 판정이 끝나기 전에는 어떤 지표도 계산하지 않는다
    assert not (P.ev / "results/a4-check.json").exists()


# --- 사전등록 초안 대조 ----------------------------------------------------------------------------------------
def test_committed_prereg_a4_draft_matches_code():
    pre = json.loads((REPO / "lab/evidence/prereg_a4.json").read_text())
    assert pre["study"] == "a4" and pre["status"] == "draft"
    assert pre["tau_d"] is None and pre["signal"] is None  # 탐색 뒤 확정
    assert pre["judge_question_sha"] == judge.QUESTION_SHA
    assert pre["subject_question"]["sha"] == subject_a4.SUBJECT_QUESTION_SHA
    assert pre["subject_question"]["instructions"] == subject_a4.SUBJECT_INSTRUCTIONS
    assert pre["subject_question"]["criteria"] == subject_a4.SUBJECT_CRITERIA
    assert pre["subject_question"]["max_versions"] == a4.MAX_PROMPT_VERSIONS
    g = pre["generator"]
    assert (g["model"], g["digest"], g["prompt_sha"], g["options"]) == (
        "llama3.1:8b", "46e0c10c039e", generate.PROMPT_SHA, generate.OPTIONS)
    assert pre["draw"]["seed"] == a4.SEED == 20261305 and pre["draw"]["random_n"] == a4.RANDOM_N == 45
    assert pre["draw"]["exclude_prior_studies"] == list(a4.PRIOR_STUDIES)
    assert pre["exploration"]["half_split"] == {"seed": a4.HALF_SEED, "design_companies": a4.HALF_N, "sha256": None}
    assert pre["exploration"]["tau_d_grid"] == list(a4.TAU_D_GRID) and pre["exploration"]["signals"] == list(a4.SIGNALS)
    assert pre["exploration"]["token_cap_max"] == a4.EXPLORE_CAP_MAX and pre["exploration"]["token_cap"] is None
    assert pre["token_cap_max"] == a4.TOKEN_CAP_MAX and pre["token_cap"] is None
    assert pre["gates"] == a4.GATES
    assert pre["bootstrap"] == {"levels": ["cluster", "question"], "n": a4.BOOTSTRAP_N, "seed": a4.SEED}
    assert pre["policies"] == {"base": a4.policy_fields(a4.BASE), "exp": a4.policy_fields(a4.EXP),
                               "a3": a4.policy_fields(a4.A3)}
    assert pre["controlled"]["swap_cycle"] == list(a4.SWAP_CYCLE)
    assert pre["controlled"]["prompt_sha256"] == _sha("lab/evidence/prompts/controlled_writer_a3.md")
    assert pre["controlled"]["swap_tags"] == list(a4.SWAP_TAGS)
    sc = pre["subject_code"]
    assert sc["numeral_determiners"] == list(subject_a4.NUMERAL_DETERMINERS)
    assert sc["adverbial_suffixes"] == list(subject_a4.ADVERBIAL_SUFFIXES)
    assert sc["generic_extra"] == list(subject.SUBJECT_GENERIC)
    assert pre["claims"]["cap"] == a4.CLAIM_CAP == a2.CLAIM_CAP
    assert pre["claims"]["not_claim_phrases"] == list(claims.NOT_CLAIM_PHRASES)
    assert pre["claims"]["generic_nouns"] == list(lexical.GENERIC_NOUNS)
    assert pre["labels"]["kappa_stop"] == a4.KAPPA_MIN
    p = pre["exploration"]["proceed"]
    assert (p["loss_rate"], p["swap_min"]) == (a4.PROCEED_LOSS_RATE, a4.PROCEED_SWAP_MIN)
    assert pre["exploration"]["design_max_loss_rate"] == a4.DESIGN_MAX_LOSS_RATE
    assert pre["product_mapping"]["cost_max_increase"] == a4.COST_MAX_INCREASE
    assert {"all_pass", "all_pass_cost_over", "h_swap_fail", "h_recall_fail", "h_prec_fail_only",
            "judge_failed"} <= set(pre["product_mapping"]["cases"])
    assert len(pre["decisions"]) == 6 and pre["fallbacks"] == []
    # 리뷰 반영(F1·F2·F3·F5·F6): 점검용 한 번, 손실 한도 ruling, (판, 문맥) 쌍, 순환 독립, 1% 규칙 분모
    assert "floor(0.01·n + 0.5)" in pre["decisions"][3]["decision"] and "open_question" not in p
    assert "floor(0.01·n + 0.5)" in p["rule"] and "a4-proceed" in p["check_once"]
    assert "a4-proceed" in pre["exploration"]["selection"] and "판·문맥" in pre["exploration"]["selection"]
    assert "후속 실패" in pre["exploration"]["recall_denominator"]
    assert "a4-proceed" in pre["subject_question"]["filled_by"] and "(판, 문맥)" in pre["subject_question"]["max_versions_rule"]
    assert "ruling" in pre["subject_question"]["context_all_rule"]
    assert "(i div 9) mod 3" in pre["controlled"]["assignment"]
    assert "c1·c3" in pre["gate_measure"]["failures"]
    assert any("a4-proceed" in x for x in pre["order"])
    assert {"app/services/evidence/subject_a4.py", "lab/evidence/a4.py", "app/services/evidence/subject.py",
            "lab/evidence/a3.py", "app/services/evidence/judge.py",
            "app/services/evidence/lexical.py", "lab/evidence/metrics.py",
            "app/services/evidence/runner.py"} <= set(pre["code_sha256"])
    for rel, sha in pre["code_sha256"].items():
        assert _sha(rel) == sha, rel
    assert pre["seeds_distinct"] is True and a3.SEED != a4.SEED
