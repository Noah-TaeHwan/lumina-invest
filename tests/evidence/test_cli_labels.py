# tests/evidence/test_cli_labels.py
from lab.evidence import __main__ as cli


def _l(label):
    return {"label": label, "passage": 1, "reason": "r"}


def test_lid_is_opaque_and_stable():
    assert cli.lid("x-q1-c2") == cli.lid("x-q1-c2") and len(cli.lid("a")) == 10 and "c2" not in cli.lid("x-q1-c2")


def test_merge_labels_round1_agree_round2_resolve_and_dispute():
    r1 = {"opus": {"a": _l("supported"), "b": _l("supported"), "c": _l("no_evidence")},
          "codex": {"a": _l("supported"), "b": _l("no_evidence"), "c": _l("supported")}}
    assert cli.disagreements(r1) == ["b", "c"]
    r2 = {"opus": {"b": _l("no_evidence"), "c": _l("no_evidence")},
          "codex": {"b": _l("no_evidence"), "c": _l("supported")}}
    out = {r["cid"]: r for r in cli.merge_labels(r1, r2)}
    assert out["a"]["label"] == "supported" and out["a"]["r2"] is None
    assert out["b"]["label"] == "no_evidence" and out["c"]["label"] == "disputed"
