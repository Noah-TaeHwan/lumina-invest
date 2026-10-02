"""Stage 1 최종 리뷰 지적(C1, I-1~I-8)과 B-llm 출력 형식 문제를 재현하는 테스트."""
import json

import pytest

from app.services.evidence import numbers as nb
from lab.evidence import __main__ as cli
from lab.evidence import baselines as bl


# C1 — 표 단위가 연도·복사 셀에까지 곱해져 지지 주장을 놓치던 문제
@pytest.mark.parametrize("claim,passage", [
    ("2024년 매출은 약 1조 67억원이다.", "[매출 표, 단위 백만원] 2024년: 1,006,771"),
    ("비중은 45.2%다.", "[가 표, 단위 백만원, %] 비중: 45.2"),
    ("판매 비율은 13.2%다.", "[나 표] 판매 비율(%) 2025년: 13.2"),
    ("국내 매출은 27,102백만원이다.", "[다 표] 국내: 27,102"),
    ("영업손실은 1,234억원이다.", "[라 표, 단위 억원] 영업이익: (1,234)"),
    ("기준일은 2025년 12월 31일이다.", "기준일 2025.12.31"),
    ("발행 주식은 3천500만 주다.", "발행주식 35,000,000주"),
])
def test_number_check_accepts_real_dart_forms(claim, passage):
    assert nb.number_check(claim, passage)


@pytest.mark.parametrize("claim,passage", [
    ("매출은 2조원이다.", "[매출 표, 단위 백만원] 2024년: 1,006,771"),
    ("비중은 34%다.", "비중 33.04%"),
    ("매출은 9,999백만원이다.", "[다 표] 국내: 27,102"),
])
def test_number_check_still_rejects_wrong_values(claim, passage):
    assert not nb.number_check(claim, passage)


# B-llm — 1B 모델이 지시를 무시하므로 JSON 스키마 구조화 출력으로 받는다
def test_llm_payload_uses_json_schema_and_parse():
    p = bl.llm_payload("주장", ["가"])
    assert p["format"]["properties"]["score"]["type"] == "integer" and p["stream"] is False
    assert bl.parse_llm_json('{"score": 85}') == 0.85
    assert bl.parse_llm_json('{"score": 150}') is None and bl.parse_llm_json("모름") is None
    assert bl.parse_llm_score("100점 중 85") == 0.85


def _P(tmp_path, comps):
    P = cli.Paths(tmp_path)
    P.split_json.parent.mkdir(parents=True, exist_ok=True)
    P.split_json.write_text(json.dumps({"companies": comps}))
    P.prereg.write_text(json.dumps({"version": 2, "stage1": {"tau_s": 0.7}}))
    return P


COMPS = [{"corp_code": "h", "corp_name": "H", "split": "holdout", "cluster": 1, "rcept_no": "9"}]


def _holdout_data(P):
    cli.write_jsonl(P.jsonl("questions.jsonl"), [{"qid": "h-q1", "corp_code": "h", "question": "q", "split": "holdout"}])
    cli.write_jsonl(P.jsonl("retrieval.jsonl"), [{"qid": "h-q1", "passage_ids": ["h-p"]}])
    cli.write_jsonl(P.jsonl("answers.jsonl"), [{"qid": "h-q1", "answer": "a"}])
    cli.write_jsonl(P.jsonl("claims.jsonl"), [{"cid": "h-q1-n1", "qid": "h-q1", "source": "natural", "text": "x"}])
    cli.write_jsonl(P.jsonl("labels.jsonl"), [{"cid": "h-q1-n1", "label": "supported"}])


# I-1 — 1회 실행은 커밋되는 원장 기준, all 분할·--limit 우회 차단
def test_run_once_uses_committed_ledger(tmp_path):
    P = _P(tmp_path, COMPS)
    cli.once(P, tmp_path / "s.jsonl", "holdout", "judge", "holdout")
    cli.log_attempt(P, "judge", split="holdout", tag="holdout")
    with pytest.raises(SystemExit, match="already"):
        cli.once(P, tmp_path / "missing.jsonl", "holdout", "judge", "holdout")
    with pytest.raises(SystemExit, match="split"):
        cli.once(P, tmp_path / "x.jsonl", "all", "judge", "x")


# I-2 — 동결 뒤 홀드아웃 데이터·코드가 바뀌면 판정 거부, 데이터 명령은 동결 뒤 홀드아웃 거부
def test_freeze_is_verified_and_closes_data_stage(tmp_path, monkeypatch):
    P = _P(tmp_path, COMPS)
    _holdout_data(P)
    monkeypatch.setattr(cli, "_code_sha", lambda P: "code1")
    monkeypatch.setattr(cli, "_tree_dirty", lambda P: False)
    cli.cmd_freeze_holdout(P, None)
    assert [c["corp_code"] for c in cli.companies(P, "holdout")] == ["h"]
    with pytest.raises(SystemExit, match="frozen"):
        cli.companies(P, "holdout", stage="data")
    cli.write_jsonl(P.jsonl("labels.jsonl"), [{"cid": "h-q1-n1", "label": "no_evidence"}])
    with pytest.raises(SystemExit, match="mismatch"):
        cli.companies(P, "holdout")
    _holdout_data(P)
    monkeypatch.setattr(cli, "_code_sha", lambda P: "code2")
    with pytest.raises(SystemExit, match="mismatch"):
        cli.companies(P, "holdout")


# I-5·I-6 — 기준선 4종이 다 있어야 조정, MDE>0.10이면 질문 수·탐색 지정 없이는 동결 거부
def test_freeze_config_refuses_large_mde(tmp_path):
    P = _P(tmp_path, COMPS)
    P.prereg.write_text(json.dumps({"version": 1}))
    (P.ev / "results").mkdir(parents=True)
    (P.ev / "results/stage1-tune.json").write_text(json.dumps({"mde": 0.2, "holdout_questions_per_company": 6, "exploratory": False}))
    with pytest.raises(SystemExit, match="MDE"):
        cli.cmd_stage1_freeze_config(P, None)
    assert cli.missing_baselines({"lex": [1.0], "emb": [None]}, 1) == ["emb", "nli", "llm"]


# I-7 — 기술 통계 전용·탐색적이면 통계 판정을 내지 않는다
def test_reported_verdict_respects_descriptive_and_exploratory():
    assert cli.reported_verdict(0.1, 0.3, descriptive_only=True, exploratory=False)["statistical"] == "descriptive"
    assert cli.reported_verdict(0.1, 0.3, descriptive_only=False, exploratory=True)["statistical"] == "exploratory"
    assert cli.reported_verdict(0.1, 0.3, descriptive_only=False, exploratory=False)["statistical"] == "superior"


# Minor — NLI 점수 자르기는 주장별 문단 수 오프셋으로
def test_split_by_counts():
    assert cli.split_by_counts([1, 2, 3, 4, 5], [2, 3]) == [[1, 2], [3, 4, 5]]
