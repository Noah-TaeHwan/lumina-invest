"""사전등록 파일이 코드와 일치하는지 검증한다(질문·모델·규칙을 바꾸면 여기서 깨진다)."""
import hashlib
import json
from pathlib import Path

import pytest

import lab.jev_gate.__main__ as cli
from lab.jev_gate import features, gate, rule

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


def test_prereg_pins_state_inputs():
    f = PREREG["features"]
    assert tuple(f["names"]) == features.STATE_FEATURES
    digest = hashlib.sha256(json.dumps(features.FEATURE_DEFINITIONS, sort_keys=True).encode()).hexdigest()
    assert f["definitions_sha256"] == digest
    assert f["round_decimals"] == features.ROUND_DECIMALS
    assert f["prev_signal_cap"] == rule.PREV_SIGNAL_CAP


def test_prereg_pins_cost_operations_and_arms():
    assert PREREG["version"] == 2
    assert PREREG["jev"]["price_per_million_input_tokens"] == pytest.approx(gate.PRICE_PER_INPUT_TOKEN * 1e6)
    assert PREREG["stage0"]["max_consecutive_failures"] == cli.MAX_CONSECUTIVE_FAILURES
    assert set(PREREG["arms"]["matching"]) == {"deterministic_filter", "random_block", "logistic"}
