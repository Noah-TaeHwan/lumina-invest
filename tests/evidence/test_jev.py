# tests/evidence/test_jev.py
import json

import httpx
import pytest

from app.lib import jev

Q = {"p1": {"type": "choice", "instructions": "x", "criteria": {"supports": "a", "contradicts": "b", "says_nothing": "c"}}}


def _payload(probs=None, model=jev.MODEL, tokens=100):
    probs = probs or {"supports": 0.7, "contradicts": 0.1, "says_nothing": 0.2}
    return {"model": model, "answers": {"p1": {"type": "choice", "choice": "supports", "confidence": 0.5, "probabilities": probs}},
            "usage": {"input_tokens": tokens, "output_tokens": 5}}


def _client(responses, seen):
    it = iter(responses)

    def handler(request):
        seen.append(request)
        status, body = next(it)
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _jc(tmp_path, responses, seen, cap=10_000):
    return jev.JevClient(tmp_path / "calls.jsonl", cap, client=_client(responses, seen), api_key="SECRET-KEY")


def test_ask_parses_probabilities_and_logs_without_key(tmp_path):
    seen = []
    r = _jc(tmp_path, [(200, _payload())], seen).ask("state", Q, tag="t")
    assert r.ok and r.answers["p1"]["supports"] == 0.7 and r.input_tokens == 100
    body = json.loads(seen[0].content)
    assert body["model"] == "jev-1.13.0" and body["questions"] == Q
    log = (tmp_path / "calls.jsonl").read_text()
    assert "SECRET-KEY" not in log and json.loads(log)["tag"] == "t"


def test_cache_hit_skips_http_and_survives_restart(tmp_path):
    seen = []
    _jc(tmp_path, [(200, _payload())], seen).ask("state", Q)
    again = jev.JevClient(tmp_path / "calls.jsonl", 10_000, client=_client([], seen), api_key="k")
    r = again.ask("state", Q)
    assert r.cached and len(seen) == 1 and again.used_tokens == 100


def test_no_cache_forces_new_request(tmp_path):
    seen = []
    c = _jc(tmp_path, [(200, _payload()), (200, _payload())], seen)
    c.ask("state", Q)
    assert not c.ask("state", Q, use_cache=False).cached and len(seen) == 2


@pytest.mark.parametrize("bad", [
    _payload(model="jev-latest"),
    _payload(probs={"supports": 1.0}),
    _payload(probs={"supports": 0.7, "contradicts": 0.1, "says_nothing": 0.1}),
    _payload(probs={"supports": 1.5, "contradicts": -0.5, "says_nothing": 0.0}),
])
def test_invalid_answers_fail_after_one_retry_and_are_not_cached(tmp_path, bad):
    seen = []
    c = _jc(tmp_path, [(200, bad), (200, bad)], seen)
    r = c.ask("state", Q)
    assert not r.ok and r.attempts == 2 and len(seen) == 2
    lines = [json.loads(x) for x in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert [x["attempt"] for x in lines] == [1, 2] and not any(x["ok"] for x in lines)


def test_retry_recovers_from_server_error(tmp_path):
    seen = []
    r = _jc(tmp_path, [(500, {"error": "x"}), (200, _payload())], seen).ask("state", Q)
    assert r.ok and r.attempts == 2


def test_token_cap_blocks_before_http(tmp_path):
    seen = []
    c = _jc(tmp_path, [(200, _payload(tokens=100))], seen, cap=100)
    c.ask("a", Q)
    with pytest.raises(jev.TokenCapExceeded):
        c.ask("b", Q)
    assert len(seen) == 1


def test_load_api_key_rejects_open_permissions(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    p = tmp_path / ".config/typesafe/api_key"
    p.parent.mkdir(parents=True)
    p.write_text("k\n")
    p.chmod(0o644)
    with pytest.raises(PermissionError):
        jev.load_api_key()
    p.chmod(0o600)
    assert jev.load_api_key() == "k"
