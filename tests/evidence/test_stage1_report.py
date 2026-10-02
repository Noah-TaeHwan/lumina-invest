# tests/evidence/test_stage1_report.py
from lab.evidence import __main__ as cli


def test_delta_stat_pairs_same_rows():
    rows = [{"y": 1, "sys": 0.9, "nli": 0.6}, {"y": 0, "sys": 0.1, "nli": 0.7},
            {"y": 1, "sys": 0.8, "nli": 0.9}, {"y": 0, "sys": 0.2, "nli": 0.1}]
    assert cli.delta_stat("nli")(rows) == 1.0 - 0.75
    assert cli.delta_stat("nli")([r for r in rows if r["y"] == 1]) is None
