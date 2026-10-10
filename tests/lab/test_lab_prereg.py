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
    assert PREREG["version"] >= 2
    assert PREREG["stage0"]["max_consecutive_failures"] == cli.MAX_CONSECUTIVE_FAILURES
    assert set(PREREG["arms"]["matching"]) == {"deterministic_filter", "random_block", "logistic"}


def test_prereg_price_matches_the_private_price_when_configured():
    """사전등록에 적힌 단가와 코드가 읽는 단가가 같아야 한다. 단가는 비공개라 설정이 없는 환경에서는 건너뛴다."""
    price = gate.load_price_per_input_token()  # autouse 대체값이 아니라 실제 설정을 읽는다
    if price is None:
        pytest.skip("입력 토큰 단가가 이 환경에 설정되어 있지 않다")
    assert PREREG["jev"]["price_per_million_input_tokens"] == pytest.approx(price * 1e6)


def test_prereg_v3_five_minute_bars_and_separate_results():
    assert PREREG["version"] >= 3
    assert PREREG["bar_minutes"] == 5
    assert PREREG["rule"]["n_grid"] == [48]
    paths = cli.Paths(Path("/repo"), PREREG)
    assert str(paths.results).endswith("lab/results/stage0-v3")
    assert str(paths.report).endswith("docs/lab/stage0-v3-report.md")


def test_prereg_v4_stage1_block():
    assert PREREG["version"] == 4
    s1 = PREREG["stage1"]
    assert s1["calls_file"] == s1["results_dir"] + "/jev_calls.jsonl"
    assert set(s1) >= {"results_dir", "report", "freeze_file", "bootstrap", "claim_rule"}
