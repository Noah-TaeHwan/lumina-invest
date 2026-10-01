"""CLI 흐름 검증 — 가짜 JEV로 세션 3회·반복·리포트까지 돌린다."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import lab.jev_gate.__main__ as cli
from lab.jev_gate import gate

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def _setup(tmp_path):
    paths = cli.Paths(tmp_path)
    paths.results.mkdir(parents=True)
    paths.rule.write_text(json.dumps({
        "n_selected": 60,
        "stats": {"60": {"candidates": 400, "trades": 300, "net_compound": -0.1, "net_mean": -0.001,
                         "gross_mean": 0.0005, "win_rate": 0.4, "reasons": {"stop": 300}}},
        "candidate_counts": {"dev": 400, "validation": 100, "holdout": 100, "post_release": 30}}))
    sample = [{"bar": i, "t_close": i, "state": {"features": {"ret_1": i / 1000}}} for i in range(300)]
    paths.sample.write_text(json.dumps(sample))
    return paths


class FakeFactory:
    """JevGate를 MockTransport로 만드는 팩토리. 호출 수를 센다."""

    def __init__(self):
        self.calls = 0

    def __call__(self, paths, pre):
        def handler(request):
            self.calls += 1
            return httpx.Response(200, json={"model": "jev-1.13.0", "answers": {"fail": {"type": "noul", "noul": 0.3}},
                                             "usage": {"input_tokens": 700, "output_tokens": 20}})
        return gate.JevGate(paths.calls, budget_usd=pre["jev"]["budget_usd"],
                            client=httpx.Client(transport=httpx.MockTransport(handler)), api_key="k")


def test_full_stage0_flow_reaches_go(tmp_path):
    paths, pre, fake = _setup(tmp_path), cli.load_prereg(), FakeFactory()
    for k in (1, 2, 3):
        cli.cmd_stage0_call(paths, pre, k, gate_factory=fake, now=T0 + timedelta(hours=3 * (k - 1)))
    cli.cmd_stage0_repeat(paths, pre, gate_factory=fake)
    assert fake.calls == 300 + 50 * 4
    summary = cli.cmd_stage0_report(paths, pre, now=T0 + timedelta(hours=7))
    assert summary["complete"] is True
    assert "판정: GO" in paths.report.read_text(encoding="utf-8")
    attempt = json.loads(paths.attempts.read_text().splitlines()[-1])
    assert attempt["verdict"] == "GO" and attempt["stage"] == "stage0"


def test_session_gap_is_enforced(tmp_path):
    paths, pre, fake = _setup(tmp_path), cli.load_prereg(), FakeFactory()
    cli.cmd_stage0_call(paths, pre, 1, gate_factory=fake, now=T0)
    with pytest.raises(SystemExit):
        cli.cmd_stage0_call(paths, pre, 2, gate_factory=fake, now=T0 + timedelta(hours=1))
    with pytest.raises(SystemExit):
        cli.cmd_stage0_call(paths, pre, 3, gate_factory=fake, now=T0 + timedelta(hours=5))


def test_session_rerun_uses_cache(tmp_path):
    paths, pre, fake = _setup(tmp_path), cli.load_prereg(), FakeFactory()
    cli.cmd_stage0_call(paths, pre, 1, gate_factory=fake, now=T0)
    cli.cmd_stage0_call(paths, pre, 1, gate_factory=fake, now=T0 + timedelta(minutes=5))
    assert fake.calls == 100
    assert len(json.loads(paths.sessions.read_text())) == 1


def test_report_before_completion_is_partial(tmp_path):
    paths, pre, fake = _setup(tmp_path), cli.load_prereg(), FakeFactory()
    cli.cmd_stage0_call(paths, pre, 1, gate_factory=fake, now=T0)
    assert cli.cmd_stage0_report(paths, pre, now=T0)["complete"] is False
    assert "판정: PARTIAL" in paths.report.read_text(encoding="utf-8")


def test_main_dispatches_report(tmp_path):
    paths, pre, fake = _setup(tmp_path), cli.load_prereg(), FakeFactory()
    cli.cmd_stage0_call(paths, pre, 1, gate_factory=fake, now=T0)
    assert cli.main(["--root", str(tmp_path), "stage0-report"]) == 0
