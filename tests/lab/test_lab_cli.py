"""CLI 흐름 검증 — 가짜 JEV로 세션 3회·반복·리포트까지 돌린다.

세션 시각은 호출 기록의 called_at에서 계산하므로, 가짜 JEV의 시계(fake.now)와 cmd의 now를 함께 움직인다.
"""
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
    """JevGate를 MockTransport로 만드는 팩토리. 호출 수를 세고, 시계·실패를 조절한다."""

    def __init__(self):
        self.calls = 0
        self.now = T0
        self.fail_next = 0      # 다음 n번 호출을 연결 오류로 만든다
        self.broken = False     # 게이트 생성 자체가 실패(키를 못 읽는 셸 등)

    def __call__(self, paths, pre):
        if self.broken:
            raise RuntimeError("API 키를 읽을 수 없음")

        def handler(request):
            self.calls += 1
            if self.fail_next:
                self.fail_next -= 1
                raise httpx.ConnectError("down")
            return httpx.Response(200, json={"model": "jev-1.13.0", "answers": {"fail": {"type": "noul", "noul": 0.3}},
                                             "usage": {"input_tokens": 700, "output_tokens": 20}})
        return gate.JevGate(paths.calls, budget_usd=pre["jev"]["budget_usd"], api_key="k",
                            client=httpx.Client(transport=httpx.MockTransport(handler)), clock=lambda: self.now)


def _call(paths, pre, fake, session, at):
    fake.now = at
    return cli.cmd_stage0_call(paths, pre, session, gate_factory=fake, now=at)


def _repeat(paths, pre, fake, at):
    fake.now = at
    return cli.cmd_stage0_repeat(paths, pre, gate_factory=fake)


def _all_sessions(paths, pre, fake):
    for k in (1, 2, 3):
        _call(paths, pre, fake, k, T0 + timedelta(hours=3 * (k - 1)))


def test_full_stage0_flow_reaches_go(tmp_path):
    paths, pre, fake = _setup(tmp_path), cli.load_prereg(), FakeFactory()
    _all_sessions(paths, pre, fake)
    _repeat(paths, pre, fake, T0 + timedelta(hours=6))
    assert fake.calls == 300 + 50 * 4
    summary = cli.cmd_stage0_report(paths, pre, now=T0 + timedelta(hours=7))
    assert summary["complete"] is True
    assert summary["sessions"][0]["started_at"] == T0.isoformat()
    assert "판정: GO" in paths.report.read_text(encoding="utf-8")
    attempt = json.loads(paths.attempts.read_text().splitlines()[-1])
    assert attempt["verdict"] == "GO" and attempt["stage"] == "stage0"


def test_session_gap_is_enforced(tmp_path):
    paths, pre, fake = _setup(tmp_path), cli.load_prereg(), FakeFactory()
    _call(paths, pre, fake, 1, T0)
    with pytest.raises(SystemExit):
        _call(paths, pre, fake, 2, T0 + timedelta(hours=1))
    with pytest.raises(SystemExit):
        _call(paths, pre, fake, 3, T0 + timedelta(hours=5))


def test_aborted_session_start_cannot_bypass_gap(tmp_path):
    """세션 2가 호출 전에 죽어도 그 시각이 세션 시작으로 남으면 안 된다(리뷰 C1 재현)."""
    paths, pre, fake = _setup(tmp_path), cli.load_prereg(), FakeFactory()
    _call(paths, pre, fake, 1, T0)
    fake.broken = True
    with pytest.raises(RuntimeError):
        _call(paths, pre, fake, 2, T0 + timedelta(hours=2, minutes=1))
    fake.broken = False
    _call(paths, pre, fake, 2, T0 + timedelta(hours=9))
    with pytest.raises(SystemExit):
        _call(paths, pre, fake, 3, T0 + timedelta(hours=9, minutes=5))


def test_session_rerun_uses_cache(tmp_path):
    paths, pre, fake = _setup(tmp_path), cli.load_prereg(), FakeFactory()
    _call(paths, pre, fake, 1, T0)
    _call(paths, pre, fake, 1, T0 + timedelta(minutes=5))
    assert fake.calls == 100


def test_consecutive_failures_stop_session_and_resume(tmp_path):
    """연속 실패 3회면 세션을 멈추고, 미완료 세션 다음 세션은 막으며, 재실행으로 이어서 채운다(리뷰 I1)."""
    paths, pre, fake = _setup(tmp_path), cli.load_prereg(), FakeFactory()
    fake.fail_next = 100
    with pytest.raises(SystemExit):
        _call(paths, pre, fake, 1, T0)
    assert fake.calls == 3
    fake.fail_next = 0
    with pytest.raises(SystemExit):
        _call(paths, pre, fake, 2, T0 + timedelta(hours=3))
    _call(paths, pre, fake, 1, T0 + timedelta(hours=3))
    assert fake.calls == 103


def test_repeat_counts_only_ok_attempts(tmp_path):
    """반복 측정 중 일시 실패 1건은 재실행에서 다시 채워져야 한다(리뷰 I2-A)."""
    paths, pre, fake = _setup(tmp_path), cli.load_prereg(), FakeFactory()
    _all_sessions(paths, pre, fake)
    fake.fail_next = 1
    _repeat(paths, pre, fake, T0 + timedelta(hours=6))
    _repeat(paths, pre, fake, T0 + timedelta(hours=6, minutes=5))
    assert fake.calls == 300 + 200 + 1
    assert cli.cmd_stage0_report(paths, pre, now=T0 + timedelta(hours=7))["complete"] is True


def test_repeat_requires_completed_session_one(tmp_path):
    """세션 1이 끝나기 전 반복 측정을 하면 세션 1 기록이 영영 안 생길 수 있다(리뷰 I2-B)."""
    paths, pre, fake = _setup(tmp_path), cli.load_prereg(), FakeFactory()
    fake.fail_next = 100
    with pytest.raises(SystemExit):
        _call(paths, pre, fake, 1, T0)
    fake.fail_next = 0
    with pytest.raises(SystemExit):
        _repeat(paths, pre, fake, T0)


def test_report_before_completion_is_partial(tmp_path):
    paths, pre, fake = _setup(tmp_path), cli.load_prereg(), FakeFactory()
    _call(paths, pre, fake, 1, T0)
    assert cli.cmd_stage0_report(paths, pre, now=T0)["complete"] is False
    assert "판정: PARTIAL" in paths.report.read_text(encoding="utf-8")


def test_main_dispatches_report(tmp_path):
    paths, pre, fake = _setup(tmp_path), cli.load_prereg(), FakeFactory()
    _call(paths, pre, fake, 1, T0)
    assert cli.main(["--root", str(tmp_path), "stage0-report"]) == 0


def test_report_requires_rule_stats(tmp_path):
    paths, pre, fake = _setup(tmp_path), cli.load_prereg(), FakeFactory()
    _call(paths, pre, fake, 1, T0)
    paths.rule.unlink()
    with pytest.raises(SystemExit, match="stage0-rule"):
        cli.cmd_stage0_report(paths, pre, now=T0)


def test_missing_api_key_stops_before_any_record(tmp_path, monkeypatch):
    paths, pre = _setup(tmp_path), cli.load_prereg()
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    with pytest.raises(SystemExit, match="API 키"):
        cli.cmd_stage0_call(paths, pre, 1, now=T0)
    assert not paths.calls.exists()
