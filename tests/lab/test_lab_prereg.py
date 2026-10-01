"""사전등록 파일이 코드와 일치하는지 검증한다(질문·모델·규칙을 바꾸면 여기서 깨진다)."""
import json
from pathlib import Path

from lab.jev_gate import gate, rule

PREREG = json.loads((Path(__file__).parents[2] / "lab/jev_gate/prereg.json").read_text(encoding="utf-8"))


def test_question_and_model_match_code():
    assert PREREG["jev"]["model"] == gate.MODEL
    assert PREREG["jev"]["question_sha256"] == gate.question_hash()
    assert PREREG["jev"]["timeout_s"] == gate.TIMEOUT_S


def test_rule_constants_match_code():
    r = PREREG["rule"]
    assert (r["stop_atr"], r["take_atr"], r["max_bars"]) == (rule.STOP_ATR, rule.TAKE_ATR, rule.MAX_BARS)


def test_stage0_thresholds_present():
    assert set(PREREG["stage0"]["thresholds"]) == {
        "latency_p99_ms", "failure_rate_max", "schema_valid_min", "repeat_agreement_min",
        "dev_candidates_min", "projected_cost_max_usd"}
