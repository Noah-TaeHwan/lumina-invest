# tests/evidence/test_a2_flow.py
"""A-2 흐름을 합성 데이터로 끝까지: 조정(a2-tune) → 확인 세트 데이터 → 동결 → 확인 1회 점수 → a2-report.
외부 호출 없음(JEV 점수 파일을 직접 쓴다). 예산 규칙(a2_budget)도 본다."""
import json

import pytest

from lab.evidence import __main__ as cli
from lab.evidence import a2

TUNE = [f"t{i}" for i in range(4)]
CHECK = [f"k{i}" for i in range(4)]
PASSAGE = "회사는 메모리 반도체와 스마트폰을 생산한다."
OTHER = "주요 원재료는 웨이퍼다."


def _setup(tmp_path, monkeypatch):
    P = cli.Paths(tmp_path, "a2")
    comps = [{"corp_code": c, "corp_name": f"회사{c}", "split": "tune", "cluster": i, "rcept_no": "1"}
             for i, c in enumerate(TUNE)]
    comps += [{"corp_code": c, "corp_name": f"회사{c}", "split": "check", "cluster": 10 + i, "rcept_no": "2"}
              for i, c in enumerate(CHECK)]
    P.split_json.parent.mkdir(parents=True, exist_ok=True)
    P.split_json.write_text(json.dumps({"companies": comps}))
    P.prereg.write_text(json.dumps({"study": "a2", "token_cap": 6_000_000,
                                    "generator": {"model": "llama3.1:8b", "digest": "46e0c10c039e"},
                                    "labels": {"labelers": {"opus": "x", "codex": "y"}}}))
    cli.write_jsonl(P.priv / "passages.jsonl", [{"id": f"{c}-p{j}", "corp_code": c, "text": t}
                                                for c in TUNE + CHECK for j, t in enumerate((PASSAGE, OTHER))])
    monkeypatch.setattr(cli, "verify_prereg_code", lambda P: None)
    monkeypatch.setattr(cli, "_code_sha", lambda P: "code")
    monkeypatch.setattr(cli, "_tree_dirty", lambda P: False)
    return P


def _data(P, corps, split, n_q):
    """질문마다 지지 주장 1(어휘 겹침 높음)·근거 없음 주장 1(겹침 낮음)과 비주장 문장 1."""
    qs, ret, ans, claims, labels, scores = [], [], [], [], [], []
    for c in corps:
        for q in range(1, n_q + 1):
            qid = f"{c}-q{q}"
            qs.append({"qid": qid, "corp_code": c, "question": "?", "split": split})
            ret.append({"qid": qid, "passage_ids": [f"{c}-p0", f"{c}-p1"]})
            ans.append({"qid": qid, "answer": "x", "latency_ms": 12000.0 + q, "cold": q == 1})
            for n, (text, lab, s, rule) in enumerate([(PASSAGE, "supported", 0.9, False),
                                                      ("전혀 관계없는 문장입니다.", "no_evidence", 0.1, False),
                                                      ("알 수 없습니다.", "non_claim", None, True)], 1):
                cid = f"{qid}-n{n}"
                claims.append({"cid": cid, "qid": qid, "source": "natural", "text": text, "not_claim_rule": rule,
                               "variant": None, "expected": None})
                r1 = {"label": lab, "passage": 1, "reason": ""}
                labels.append({"cid": cid, "label": lab, "r1": {"opus": r1, "codex": r1}, "r2": None})
                if s is not None:
                    scores.append({"cid": cid, "s": [s, 0.0], "c": [0.0, 0.0], "ok": True, "requests": 1})
    for name, rows in (("questions.jsonl", qs), ("retrieval.jsonl", ret), ("answers.jsonl", ans),
                       ("claims.jsonl", claims), ("labels.jsonl", labels)):
        cli.write_jsonl(P.jsonl(name), sorted(cli.read_jsonl(P.jsonl(name)) + rows, key=lambda r: next(iter(r.values()))))
    return scores


def test_tune_freeze_check_report(tmp_path, monkeypatch):
    P = _setup(tmp_path, monkeypatch)
    cli.write_jsonl(P.priv / "scores/tune.jsonl", _data(P, TUNE, "tune", 10))
    cli.cmd_a2_tune(P, None)
    tuned = json.loads(P.tune_json.read_text())
    assert tuned["n"] == 80 and tuned["tau_s"] == 0.55 and tuned["tau_s_fallback"] is False
    assert tuned["theta_low"] is not None and tuned["theta_high"] is not None
    assert tuned["not_claim_audit"]["flagged"] == 40 and tuned["not_claim_audit"]["claim_share"] == 0
    with pytest.raises(SystemExit, match="frozen"):
        cli.companies(P, "check")  # 판정은 동결 뒤

    check_scores = _data(P, CHECK, "check", 10)
    with pytest.raises(SystemExit, match="check data"):
        cli.cmd_a2_tune(P, None)  # 확인 세트 데이터가 생기면 다시 고르지 못한다
    cli.cmd_freeze_holdout(P, None)
    cli.write_jsonl(P.priv / "scores/check.jsonl", check_scores)
    cli.cmd_a2_report(P, None)
    res = json.loads((P.ev / "results/a2-check.json").read_text())
    assert res["n"] == 80 and res["gates"]["h_prec"]["predicted"] == 40
    assert res["gates"]["h_prec"]["descriptive_only"] is True  # ✅ 예측 150건 미만
    assert res["policy_a2_v1"]["tau_s"] == 0.85 and res["policy_a2_v1"]["version"] == "a2-v1"
    assert res["gates"]["h_tier"]["status"] == "tested" and res["gates"]["h_tier"]["call_reduction"] == 1.0
    assert res["generation_latency"]["cold_excluded"] == 8 and res["generation_latency"]["n"] == 72
    doc = (tmp_path / "docs/lab/evidence-a2-report.md").read_text()
    assert "AI 참조 라벨" in doc and "제휴 관계가 아니다" in doc and "정밀도 목표 미확인" in doc
    events = [json.loads(x) for x in P.attempts.read_text().splitlines()]
    assert [e["event"] for e in events] == ["a2-tune", "freeze-holdout", "a2-report"]
    assert all(e["study"] == "a2" for e in events)


def test_tune_stops_on_low_label_kappa(tmp_path, monkeypatch):
    P = _setup(tmp_path, monkeypatch)
    _data(P, TUNE, "tune", 3)
    rows = cli.read_jsonl(P.jsonl("labels.jsonl"))
    for i, r in enumerate(rows):  # 두 라벨러가 반대로 갈린다
        if r["r1"]["opus"]["label"] != "non_claim":
            r["r1"] = {"opus": {"label": "supported"}, "codex": {"label": "no_evidence" if i % 2 else "supported"}}
    cli.write_jsonl(P.jsonl("labels.jsonl"), rows)
    with pytest.raises(SystemExit, match="kappa"):
        cli.cmd_a2_tune(P, None)
    assert not P.tune_json.exists()


def test_budget_reserves_check_on_tune_and_drops_controlled_on_check(tmp_path, monkeypatch):
    P = _setup(tmp_path, monkeypatch)
    text = {f"{c}-p{j}": t for c in TUNE + CHECK for j, t in enumerate((PASSAGE, OTHER))}
    ret = {f"{c}-q1": [f"{c}-p0", f"{c}-p1"] for c in TUNE + CHECK}
    names = {c: f"회사{c}" for c in TUNE + CHECK}
    nat = [{"cid": f"{c}-q1-n1", "qid": f"{c}-q1", "source": "natural", "text": PASSAGE} for c in TUNE]
    ctl = [{"cid": f"{c}-q1-c1", "qid": f"{c}-q1", "source": "controlled", "text": OTHER} for c in TUNE]
    out = cli.a2_budget(P, "tune", nat + ctl, text, ret, names)
    assert out == nat  # 조정 세트는 자연 주장만
    used = 6_000_000 - 8 * a2.TOKENS_PER_CALL  # 남은 몫 = 주장 8개
    cli.write_jsonl(P.calls, [{"key": "z", "ok": True, "input_tokens": used}])
    cli.a2_budget(P, "tune", nat, text, ret, names)  # 4 + 예약 4 = 8 → 통과
    with pytest.raises(SystemExit, match="budget"):
        cli.a2_budget(P, "tune", nat + [dict(nat[0], cid="x")], text, ret, names)
    cnat = [dict(c, qid=c["qid"].replace("t", "k"), cid=c["cid"].replace("t", "k")) for c in nat + nat]
    cctl = [dict(c, qid=c["qid"].replace("t", "k")) for c in ctl]
    kept = cli.a2_budget(P, "check", cnat + cctl, text, ret, names)  # 8 + 4 > 8 → 통제 주장을 뺀다
    assert kept == cnat
    assert json.loads(P.attempts.read_text().splitlines()[-1])["event"] == "budget-drop-controlled"
    with pytest.raises(SystemExit, match="budget"):
        cli.a2_budget(P, "check", cnat + cnat[:1], text, ret, names)
