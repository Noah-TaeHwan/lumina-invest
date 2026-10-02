# tests/evidence/test_cli.py
import json

import pytest

from lab.evidence import __main__ as cli


def _split(tmp_path):
    P = cli.Paths(tmp_path)
    P.split_json.parent.mkdir(parents=True, exist_ok=True)
    comps = [{"corp_code": "a", "corp_name": "A", "split": "tune", "cluster": 0},
             {"corp_code": "b", "corp_name": "B", "split": "check", "cluster": 1},
             {"corp_code": "c", "corp_name": "C", "split": "holdout", "cluster": 2}]
    P.split_json.write_text(json.dumps({"companies": comps}))
    return P


def test_holdout_refused_before_freeze(tmp_path):
    P = _split(tmp_path)
    assert [c["corp_code"] for c in cli.companies(P, "dev")] == ["a", "b"]
    with pytest.raises(SystemExit, match="frozen"):
        cli.companies(P, "holdout")
    P.prereg_holdout.write_text("{}")
    assert [c["corp_code"] for c in cli.companies(P, "holdout")] == ["c"]


def test_jsonl_roundtrip_and_attempts(tmp_path):
    P = cli.Paths(tmp_path)
    cli.write_jsonl(P.jsonl("x.jsonl"), [{"a": 1}, {"a": "한글"}])
    assert cli.read_jsonl(P.jsonl("x.jsonl")) == [{"a": 1}, {"a": "한글"}]
    cli.log_attempt(P, "test", n=1)
    rec = json.loads(P.attempts.read_text())
    assert rec["event"] == "test" and rec["n"] == 1 and "at" in rec
