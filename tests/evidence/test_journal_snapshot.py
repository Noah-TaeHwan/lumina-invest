# tests/evidence/test_journal_snapshot.py
"""판단 일지 스냅샷 생성기(모듈 C spec 결정 5-3): 끝난 판정 실행 하나에서 서버가 복사, s·c·lex 등은 담지 않는다.

DB 없이 메모리 객체로 확인한다.
"""
import uuid
from datetime import datetime, timezone

import pytest

from app.models import Chat, EvidenceClaim, EvidenceRun
from app.services.journal import snapshot as snap
from tests.evidence.p2_support import ANSWER, CO, CORP, PASSAGES

FORBIDDEN = {"s", "c", "lex", "jev_request_key", "latency_ms", "attempts", "cached", "input_tokens", "calls",
             "cache_hits"}


def _run(status="done", policy="a2-v1"):
    at = datetime(2026, 10, 4, 1, 2, 3, tzinfo=timezone.utc)
    run = EvidenceRun(id=uuid.uuid4(), chat_id=uuid.uuid4(), conversation_id=uuid.uuid4(), user_id=uuid.uuid4(),
                      status=status, trigger="auto", company=CO, corp_code=CORP, rcept_no="20260312000123",
                      passages=[{**{k: p[k] for k in ("passage_id", "section", "idx", "sha256", "text")},
                                 "extra": "x"} for p in PASSAGES],
                      policy_version=policy, jev_model="jev-1.13.0", generator_model="llama3.1:8b",
                      calls=1, cache_hits=0, input_tokens=100, error_code=None, created_at=at, finished_at=at)
    claims = [
        EvidenceClaim(idx=0, text="회사는 메모리 반도체를 생산한다.", start=0, end=18, status="supported", route="jev",
                      reason=None, source_idx=0, s=[0.97, 0.1, 0.1], c=[0.01, 0.0, 0.0], lex=0.4,
                      number_ok=None, jev_request_key="k" * 64, attempts=1, latency_ms=3.0, cached=False,
                      input_tokens=100),
        EvidenceClaim(idx=1, text="2025년 영업이익은 32조 7,260억원이다.", start=19, end=43, status="supported",
                      route="lex_high", reason=None, source_idx=1, s=None, c=None, lex=0.95, number_ok=[True],
                      jev_request_key=None, attempts=0, latency_ms=0.0, cached=False, input_tokens=0),
        EvidenceClaim(idx=2, text="문단에서 확인할 수 없습니다.", start=44, end=59, status="not_claim",
                      route="rule_not_claim", reason=None, source_idx=None, s=None, c=None, lex=None,
                      number_ok=None, jev_request_key=None, attempts=0, latency_ms=0.0, cached=False,
                      input_tokens=0),
    ]
    chat = Chat(id=run.chat_id, user_id=run.user_id, client_id="x", conversation_id=run.conversation_id,
                question="HBM 매출 비중과 주요 고객은?", answer=ANSWER, steps=[], citations=[])
    return run, claims, chat


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


def test_snapshot_c1_shape():
    run, claims, chat = _run()
    out = snap.build_snapshot(run, claims, chat)
    assert snap.SNAPSHOT_VERSION == "c1"
    assert out["run"] == {"id": str(run.id), "status": "done", "error_code": None, "trigger": "auto",
                          "policy_version": "a2-v1", "jev_model": "jev-1.13.0", "generator_model": "llama3.1:8b",
                          "created_at": "2026-10-04T01:02:03+00:00", "finished_at": "2026-10-04T01:02:03+00:00"}
    assert (out["company"], out["corp_code"], out["rcept_no"]) == (CO, CORP, "20260312000123")
    assert (out["question"], out["answer"]) == ("HBM 매출 비중과 주요 고객은?", ANSWER)
    assert out["claims"][0] == {"idx": 0, "text": "회사는 메모리 반도체를 생산한다.", "start": 0, "end": 18,
                                "status": "supported", "route": "jev", "reason": None, "source_idx": 0,
                                "number_ok": None, "confidence": "보통"}  # a2-v1: τ_s+0.15=1.0 미만
    assert out["claims"][1]["number_ok"] == [True] and out["claims"][1]["confidence"] is None  # lex 경로
    assert out["passages"][0] == {k: PASSAGES[0][k] for k in ("passage_id", "section", "idx", "sha256", "text")}


def test_snapshot_has_no_jev_raw_values():
    out = snap.build_snapshot(*_run())
    assert not FORBIDDEN & set(_keys(out))


def test_snapshot_is_a_copy():
    run, claims, chat = _run()
    out = snap.build_snapshot(run, claims, chat)
    run.passages[0]["text"] = "바뀜"
    claims[0].number_ok = [False]
    assert out["passages"][0]["text"] == PASSAGES[0]["text"]
    assert out["claims"][0]["number_ok"] is None


@pytest.mark.parametrize("status", ["done", "partial", "failed", "limited", "skipped"])
def test_finished_statuses_are_recordable(status):
    run, claims, chat = _run(status)
    assert snap.build_snapshot(run, claims, chat)["run"]["status"] == status


@pytest.mark.parametrize("status", ["pending", "running"])
def test_active_run_is_refused(status):
    run, claims, chat = _run(status)
    with pytest.raises(snap.RunNotFinished):
        snap.build_snapshot(run, claims, chat)


def test_selectable_claims_excludes_not_claim():
    out = snap.build_snapshot(*_run())
    assert snap.selectable_claims(out) == {0, 1}
