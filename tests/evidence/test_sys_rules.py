# tests/evidence/test_sys_rules.py
from app.services.evidence.judge import Judgement, sys_decision
from lab.evidence import thresholds as th


def J(s, c, ok=True):
    return Judgement(s, c, ok, 1)


def test_sys_rule_invalid_support_does_not_block_contradiction():
    j = J([0.95, 0.85, 0.0], [0.0, 0.0, 0.9])
    assert sys_decision(j, [False, True, True], tau_s=0.8, tau_c=0.8) == ("contradicted", 2, 0.0)
    assert sys_decision(j, [True, True, True], tau_s=0.8, tau_c=0.8) == ("supported", 0, 0.95)


def test_sys_rule_picks_valid_passage_and_scores():
    j = J([0.9, 0.7, 0.1], [0.05, 0.1, 0.1])
    assert sys_decision(j, [False, True, True], 0.6, 0.8) == ("supported", 1, 0.7)
    assert sys_decision(j, [False, False, False], 0.6, 0.8) == ("no_evidence", None, 0.0)
    assert sys_decision(J([0.0], [0.0], ok=False), [True], 0.5, 0.5) == ("unjudged", None, 0.0)


def test_choose_thresholds():
    rows = ([{"s": [0.9], "c": [0.05], "valid": [True], "label": "supported"}] * 9
            + [{"s": [0.6], "c": [0.1], "valid": [True], "label": "no_evidence"}] * 3
            + [{"s": [0.1], "c": [0.8], "valid": [True], "label": "contradicted"}] * 4
            + [{"s": [0.2], "c": [0.4], "valid": [True], "label": "no_evidence"}] * 2)
    tc = th.choose_tau_c(rows)
    assert tc == 0.8
    assert th.choose_tau_s(rows, tc) == 0.65
