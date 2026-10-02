# tests/evidence/test_jev_service.py
"""제품용 JEV 클라이언트: 가짜 전송(httpx MockTransport)과 메모리 Redis로만 돈다."""
import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx

from app.lib import jev, jev_service

Q = {"p1": {"type": "choice", "instructions": "x", "criteria": {"supports": "a", "contradicts": "b", "says_nothing": "c"}},
     "p2": {"type": "choice", "instructions": "y", "criteria": {"supports": "a", "contradicts": "b", "says_nothing": "c"}}}
NOW = datetime(2026, 10, 2, 3, 0, tzinfo=timezone.utc)  # KST 2026-10-02 12:00


def _client(redis, steps, seen, *, timeout=0.05):
    """steps: 시도마다 (status, body) 또는 ('sleep', 초, status, body)."""
    it = iter(steps)

    async def handler(request):
        seen.append(request)
        step = next(it)
        if step[0] == "sleep":
            await asyncio.sleep(step[1])
            step = step[2:]
        return httpx.Response(step[0], json=step[1])

    quota = jev_service.Quota(redis, now=lambda: NOW)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return jev_service.ServiceJevClient(redis, quota, api_key="SECRET-KEY", client=http, timeout=timeout)


def _ask(client, state="state", questions=Q, user_id="u1"):
    return asyncio.run(client.ask(state, questions, user_id=user_id))


def test_success_is_cached_under_request_key_for_thirty_days(fake_redis, jev_payload):
    seen = []
    r = _ask(_client(fake_redis, [(200, jev_payload(Q))], seen))
    assert r.ok and not r.cached and r.attempts == 1 and r.input_tokens == 100
    assert r.key == jev.request_key("state", Q)
    cache_key = f"evidence:jev:{jev.request_key('state', Q)}"
    assert json.loads(fake_redis.data[cache_key]) == r.answers
    assert fake_redis.ttl[cache_key] == 30 * 86400
    body = json.loads(seen[0].content)
    assert body == {"model": jev.MODEL, "state": "state", "questions": Q}
    assert seen[0].headers["Authorization"] == "Bearer SECRET-KEY"


def test_cache_hit_makes_no_call_and_uses_no_quota(fake_redis, jev_payload):
    answers = jev.parse_answers(jev_payload(Q), Q)
    fake_redis.data[f"evidence:jev:{jev.request_key('state', Q)}"] = json.dumps(answers)
    seen = []
    r = _ask(_client(fake_redis, [], seen))
    assert r.ok and r.cached and r.answers == answers and r.attempts == 0 and r.input_tokens == 0
    assert seen == [] and not any(k.startswith("evidence:quota") for k in fake_redis.data)


def test_failure_is_not_cached(fake_redis):
    seen = []
    r = _ask(_client(fake_redis, [(500, {}), (502, {})], seen))
    assert not r.ok and r.attempts == 2 and r.error_code == "http_5xx" and len(seen) == 2
    assert not any(k.startswith("evidence:jev:") for k in fake_redis.data)


def test_timeout_retries_once(fake_redis, jev_payload):
    seen = []
    r = _ask(_client(fake_redis, [("sleep", 1.0, 200, jev_payload(Q)), (200, jev_payload(Q))], seen))
    assert r.ok and r.attempts == 2 and len(seen) == 2


def test_timeout_twice_fails_with_timeout_code(fake_redis, jev_payload):
    seen = []
    r = _ask(_client(fake_redis, [("sleep", 1.0, 200, jev_payload(Q))] * 2, seen))
    assert not r.ok and r.attempts == 2 and r.error_code == "timeout"


def test_default_timeout_is_two_seconds_per_attempt():
    assert jev_service.TIMEOUT_S == 2.0


def test_429_401_403_are_not_retried(fake_redis, jev_payload):
    for status in (429, 401, 403):
        seen = []
        r = _ask(_client(fake_redis, [(status, {}), (200, jev_payload(Q))], seen))
        assert not r.ok and r.attempts == 1 and len(seen) == 1
        assert r.http_status == status and r.error_code == "http_4xx"


def test_invalid_response_is_retried_and_tokens_of_both_attempts_counted(fake_redis, jev_payload):
    seen = []
    client = _client(fake_redis, [(200, jev_payload(Q, model="jev-0.0")), (200, jev_payload(Q, tokens=120))], seen)
    r = _ask(client)
    assert r.ok and r.attempts == 2 and r.input_tokens == 220
    assert fake_redis.data["evidence:quota:20261002:user:u1:calls"] == "2"
    assert fake_redis.data["evidence:quota:20261002:user:u1:tokens"] == "220"
    assert fake_redis.data["evidence:quota:20261002:global:tokens"] == "220"


def test_invalid_response_twice_fails(fake_redis, jev_payload):
    bad = jev_payload(Q, {"supports": 0.9, "contradicts": 0.9, "says_nothing": 0.1})
    r = _ask(_client(fake_redis, [(200, bad), (200, bad)], []))
    assert not r.ok and r.error_code == "invalid_response"


def test_server_error_then_success(fake_redis, jev_payload):
    r = _ask(_client(fake_redis, [(503, {}), (200, jev_payload(Q))], []))
    assert r.ok and r.attempts == 2


def test_redis_cache_errors_do_not_break_call(down_redis, jev_payload):
    seen = []
    r = _ask(_client(down_redis, [(200, jev_payload(Q))], seen))
    assert r.ok and len(seen) == 1


def test_attempt_log_has_no_key_state_or_text(fake_redis, jev_payload, caplog):
    caplog.set_level(logging.INFO, logger="app.evidence")
    _ask(_client(fake_redis, [(500, {}), (200, jev_payload(Q))], []), state="[Claim] 비밀 주장 문장")
    lines = [json.loads(r.getMessage()) for r in caplog.records if r.name == "app.evidence.jev"]
    assert [ln["attempt"] for ln in lines] == [1, 2]
    assert lines[0]["http_status"] == 500 and lines[1]["ok"] is True
    assert len(lines[0]["request_key"]) == 12
    assert "SECRET-KEY" not in caplog.text and "비밀 주장" not in caplog.text
