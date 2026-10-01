"""JEV 게이트 클라이언트 검증 — 실제 API를 호출하지 않는다."""
import json
from datetime import datetime, timezone

import httpx
import pytest

from lab.jev_gate import gate

STATE = {"features": {"ret_1": 0.1}, "feature_definitions": {"ret_1": "x"}}


class Recorder:
    """MockTransport 핸들러: 요청을 기록하고 make()가 돌려준 응답을 반환한다(예외면 그대로 발생)."""

    def __init__(self, make):
        self.make = make
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.make()


def ok_response(p: float = 0.7, tokens: int = 500) -> httpx.Response:
    return httpx.Response(200, json={"model": "jev-1.13.0", "answers": {"fail": {"type": "noul", "noul": p}},
                                     "usage": {"input_tokens": tokens, "output_tokens": 20}})


def _gate(path, rec, **kw) -> gate.JevGate:
    return gate.JevGate(path, client=httpx.Client(transport=httpx.MockTransport(rec)), api_key="test-key", **kw)


def test_success_parses_and_logs_without_key(tmp_path):
    rec = Recorder(ok_response)
    r = _gate(tmp_path / "calls.jsonl", rec).ask(STATE, tag="session-1")
    assert (r.ok, r.p_fail, r.input_tokens, r.model, r.cached) == (True, 0.7, 500, "jev-1.13.0", False)
    body = json.loads(rec.requests[0].content)
    assert body["model"] == "jev-1.13.0" and body["questions"]["fail"]["type"] == "noul"
    assert body["state"] == STATE
    assert rec.requests[0].headers["Authorization"] == "Bearer test-key"
    text = (tmp_path / "calls.jsonl").read_text()
    assert "test-key" not in text
    line = json.loads(text)
    assert line["state"] == STATE and line["tag"] == "session-1"


def test_cache_hit_and_forced_repeat(tmp_path):
    rec = Recorder(ok_response)
    g = _gate(tmp_path / "calls.jsonl", rec)
    g.ask(STATE)
    assert g.ask(STATE).cached is True and len(rec.requests) == 1
    assert g.ask(STATE, use_cache=False).cached is False and len(rec.requests) == 2


def test_cache_key_ignores_dict_order():
    assert gate.cache_key({"a": 1, "b": 2}) == gate.cache_key({"b": 2, "a": 1})


def _raise(exc):
    def make():
        raise exc
    return make


@pytest.mark.parametrize("make,error", [
    (lambda: httpx.Response(500), "http_500"),
    (lambda: ok_response(p=1.5), "schema"),
    (lambda: httpx.Response(200, json={"model": "jev-1.13.0", "answers": {}}), "schema"),
    (_raise(httpx.ReadTimeout("slow")), "timeout"),
    (_raise(httpx.ConnectError("down")), "ConnectError"),
])
def test_failures_are_blocked_and_not_cached(tmp_path, make, error):
    rec = Recorder(make)
    g = _gate(tmp_path / "calls.jsonl", rec)
    r = g.ask(STATE)
    assert r.ok is False and r.error == error and gate.is_blocked(r, 0.99)
    g.ask(STATE)
    assert len(rec.requests) == 2


def test_budget_stops_calls(tmp_path):
    rec = Recorder(ok_response)
    g = _gate(tmp_path / "calls.jsonl", rec, budget_usd=1e-9)
    g.ask(STATE)
    with pytest.raises(gate.BudgetExceeded):
        g.ask({"features": {"ret_1": 0.2}})
    assert len(rec.requests) == 1


def test_reload_restores_cache_and_spend(tmp_path):
    path = tmp_path / "calls.jsonl"
    _gate(path, Recorder(ok_response)).ask(STATE)
    rec2 = Recorder(ok_response)
    g2 = _gate(path, rec2)
    assert g2.ask(STATE).cached is True and rec2.requests == []
    assert g2.spent_usd == pytest.approx(500 * gate.PRICE_PER_INPUT_TOKEN)


def test_is_blocked_threshold():
    base = dict(key="k", ok=True, latency_ms=1.0, model="jev-1.13.0", input_tokens=1, status=200, error=None,
                called_at="t")
    assert gate.is_blocked(gate.GateResult(p_fail=0.5, **base), 0.5) is True
    assert gate.is_blocked(gate.GateResult(p_fail=0.49, **base), 0.5) is False


def test_load_api_key_rejects_open_permissions(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    key = tmp_path / ".config/typesafe/api_key"
    key.parent.mkdir(parents=True)
    key.write_text("secret-value\n")
    key.chmod(0o644)
    with pytest.raises(PermissionError):
        gate.load_api_key()
    key.chmod(0o600)
    assert gate.load_api_key() == "secret-value"


def test_called_at_uses_injected_clock(tmp_path):
    at = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    r = _gate(tmp_path / "calls.jsonl", Recorder(ok_response), clock=lambda: at).ask(STATE)
    assert r.called_at == at.isoformat()
