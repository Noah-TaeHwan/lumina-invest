# tests/evidence/test_evidence_notice.py
"""외부 전송 고지 확인 기록(P4, spec 8절): 처음 켤 때 확인한 사실을 Redis user_state에 남긴다.

DB·Redis를 쓰지 않는다. user_state 읽기·쓰기는 메모리 가짜로 바꾼다.
"""
import asyncio
import uuid

import pytest

from app.config import settings
from app.routes import evidence
from tests.evidence.p2_support import Who, client, make_app

USER = {"id": str(uuid.uuid4()), "roles": ["user"]}


@pytest.fixture
def state(monkeypatch):
    store: dict[str, dict] = {}

    async def get(uid):
        return dict(store.get(uid, {}))

    async def update(uid, updates):
        store.setdefault(uid, {}).update(updates)
        return store[uid]

    monkeypatch.setattr(evidence, "get_user_state", get)
    monkeypatch.setattr(evidence, "update_user_state", update)
    monkeypatch.setattr(settings, "EVIDENCE_CHAT_ENABLED", True)
    return store


def _call(app, method, path):
    async def go():
        async with client(app) as c:
            return await c.request(method, path)
    return asyncio.run(go())


def test_flag_off_is_404(monkeypatch):
    monkeypatch.setattr(settings, "EVIDENCE_CHAT_ENABLED", False)
    app = make_app(None, Who(USER))
    assert _call(app, "GET", "/api/evidence/notice").status_code == 404
    assert _call(app, "POST", "/api/evidence/notice").status_code == 404


def test_login_required(state):
    app = make_app(None, Who(None))
    assert _call(app, "GET", "/api/evidence/notice").status_code == 401
    assert _call(app, "POST", "/api/evidence/notice").status_code == 401


def test_not_acknowledged_until_confirmed_then_recorded(state):
    app = make_app(None, Who(USER))
    r = _call(app, "GET", "/api/evidence/notice")
    assert r.status_code == 200
    assert r.json() == {"acknowledged": False, "version": evidence.NOTICE_VERSION}

    r = _call(app, "POST", "/api/evidence/notice")
    assert r.status_code == 200 and r.json()["acknowledged"] is True
    saved = state[USER["id"]]
    assert saved["evidence_notice_ack"] == evidence.NOTICE_VERSION
    assert saved["evidence_notice_ack_at"]

    assert _call(app, "GET", "/api/evidence/notice").json()["acknowledged"] is True


def test_older_notice_version_asks_again(state):
    state[USER["id"]] = {"evidence_notice_ack": "old-version"}
    app = make_app(None, Who(USER))
    assert _call(app, "GET", "/api/evidence/notice").json()["acknowledged"] is False


def test_redis_failure_reads_as_not_acknowledged_and_save_is_503(state, monkeypatch):
    async def boom(*a, **k):
        raise ConnectionError("redis down")

    monkeypatch.setattr(evidence, "get_user_state", boom)
    monkeypatch.setattr(evidence, "update_user_state", boom)
    app = make_app(None, Who(USER))
    assert _call(app, "GET", "/api/evidence/notice").json()["acknowledged"] is False
    assert _call(app, "POST", "/api/evidence/notice").status_code == 503
