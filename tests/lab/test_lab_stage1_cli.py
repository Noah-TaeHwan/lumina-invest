"""Stage 1 CLI 검증 — 가짜 JEV와 합성 후보 표."""
import json

import httpx
import pandas as pd
import pytest

import lab.jev_gate.__main__ as cli
from lab.jev_gate import features as ft, gate


def _table(n_per=40):
    rows = []
    for period in ("dev", "validation", "holdout", "post_release"):
        for i in range(n_per):
            state = {"features": {"i": i, "p": period}}
            rows.append({"period": period, "bar": i, "t_close": i, "day": f"2025-10-{1 + i % 20:02d}",
                         "key": gate.cache_key(state), "state": state, "label": i % 2,
                         "net_ret": -0.01 if i % 2 else 0.01, "exit_reason": "time", "atr14": 1.0,
                         **{f: float((i * (j + 3)) % 7) for j, f in enumerate(ft.STATE_FEATURES)}})
    return pd.DataFrame(rows)


class Fake:
    def __init__(self):
        self.calls = 0

    def __call__(self, paths, pre):
        def handler(request):
            self.calls += 1
            body = json.loads(request.content)
            p = 0.7 if body["state"]["features"]["i"] % 2 else 0.3
            return httpx.Response(200, json={"model": "jev-1.13.0", "answers": {"fail": {"type": "noul", "noul": p}},
                                             "usage": {"input_tokens": 800, "output_tokens": 20}})
        return gate.JevGate(paths.calls, client=httpx.Client(transport=httpx.MockTransport(handler)), api_key="k")


def _paths(tmp_path):
    pre = cli.load_prereg()
    return cli.Stage1Paths(tmp_path, pre), pre


def test_holdout_requires_matching_freeze(tmp_path):
    paths, pre = _paths(tmp_path)
    t, fake = _table(), Fake()
    with pytest.raises(SystemExit, match="동결"):
        cli.cmd_stage1_call(paths, pre, "holdout", gate_factory=fake, table=t)
    assert fake.calls == 0 and not paths.calls.exists()
    cli.cmd_stage1_freeze(paths, pre, table=t)
    frozen = json.loads(paths.freeze.read_text())
    frozen["prereg_sha256"] = "0" * 64
    paths.freeze.write_text(json.dumps(frozen))
    with pytest.raises(SystemExit, match="동결"):
        cli.cmd_stage1_call(paths, pre, "holdout", gate_factory=fake, table=t)


def test_dev_call_then_freeze_then_holdout(tmp_path):
    paths, pre = _paths(tmp_path)
    t, fake = _table(), Fake()
    cli.cmd_stage1_call(paths, pre, "dev", gate_factory=fake, table=t)
    assert fake.calls == 40
    cli.cmd_stage1_freeze(paths, pre, table=t)
    frozen = json.loads(paths.freeze.read_text())
    assert frozen["question_sha256"] == gate.question_hash() and len(frozen["logistic"]["coef"]) == 11
    cli.cmd_stage1_call(paths, pre, "holdout", gate_factory=fake, table=t)
    cli.cmd_stage1_call(paths, pre, "holdout", gate_factory=fake, table=t)   # 재실행은 캐시
    assert fake.calls == 80


def test_freeze_refuses_to_overwrite(tmp_path):
    paths, pre = _paths(tmp_path)
    t = _table()
    cli.cmd_stage1_freeze(paths, pre, table=t)
    with pytest.raises(SystemExit, match="이미"):
        cli.cmd_stage1_freeze(paths, pre, table=t)
