# tests/evidence/test_stage1_cli.py
import json

import pytest

from lab.evidence import __main__ as cli

COMPS = [{"corp_code": "a", "corp_name": "A", "split": "tune", "cluster": 0, "rcept_no": "1"},
         {"corp_code": "c", "corp_name": "C", "split": "holdout", "cluster": 2, "rcept_no": "3"}]


def _P(tmp_path):
    P = cli.Paths(tmp_path)
    P.split_json.parent.mkdir(parents=True, exist_ok=True)
    P.split_json.write_text(json.dumps({"companies": COMPS}))
    P.prereg.write_text(json.dumps({"version": 1}))
    return P


def test_two_stage_holdout_seal(tmp_path):
    P = _P(tmp_path)
    with pytest.raises(SystemExit):
        cli.companies(P, "holdout", stage="data")
    P.prereg.write_text(json.dumps({"version": 2, "stage1": {"tau_s": 0.7}}))
    assert [c["corp_code"] for c in cli.companies(P, "holdout", stage="data")] == ["c"]
    with pytest.raises(SystemExit, match="frozen"):
        cli.companies(P, "holdout")


def test_holdout_run_once(tmp_path):
    P = _P(tmp_path)
    out = tmp_path / "x.jsonl"
    cli.once(P, out, "holdout", "judge", "holdout")
    out.write_text("{}\n")
    with pytest.raises(SystemExit, match="already"):
        cli.once(P, out, "holdout", "judge", "holdout")
    cli.once(P, out, "tune", "judge", "tune")


def test_freeze_config_refuses_overwrite(tmp_path):
    P = _P(tmp_path)
    (P.ev / "results").mkdir(parents=True)
    (P.ev / "results/stage1-tune.json").write_text(json.dumps({"tau_s": 0.7, "tau_c": 0.6, "b_star": "nli", "mde": 0.05}))
    cli.cmd_stage1_freeze_config(P, None)
    assert json.loads(P.prereg.read_text())["stage1"]["b_star"] == "nli"
    with pytest.raises(SystemExit, match="already"):
        cli.cmd_stage1_freeze_config(P, None)
