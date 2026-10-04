# tests/evidence/test_confidence_label.py
"""확신도 두 단계(P4 리뷰, spec 3.3): 서버가 정책의 τ_s로 "높음"/"보통"을 정해 문장 직렬화에 넣는다.

확률 숫자는 화면에 보이지 않고, 화면은 이 라벨만 쓴다(τ_s를 프런트에 복제하지 않는다). DB를 쓰지 않는다.
"""
import uuid

import pytest

from app.models import EvidenceClaim, EvidenceRun
from app.services.evidence import records
from app.services.evidence.runner import A2_PROVISIONAL


def _claim(status="supported", s=None, source_idx=0):
    return EvidenceClaim(idx=0, text="t", start=0, end=1, status=status, source_idx=source_idx, s=s,
                         attempts=0, latency_ms=0.0, cached=False, input_tokens=0)


@pytest.mark.parametrize("s_v, want", [
    (0.70, "보통"),    # τ_s
    (0.849, "보통"),   # τ_s + 0.15 미만
    (0.85, "높음"),    # τ_s + 0.15
    (0.99, "높음"),
])
def test_supported_band_by_policy_tau(s_v, want):
    assert records.confidence_label(_claim(s=[0.1, s_v], source_idx=1), A2_PROVISIONAL.version) == want


@pytest.mark.parametrize("claim, policy", [
    (_claim(status="contradicted", s=[0.99]), A2_PROVISIONAL.version),  # ✅만 정의된다
    (_claim(status="no_evidence", s=[0.99]), A2_PROVISIONAL.version),
    (_claim(s=None), A2_PROVISIONAL.version),                          # 확률 없음(lex 경로)
    (_claim(s=[0.99], source_idx=None), A2_PROVISIONAL.version),
    (_claim(s=[0.99], source_idx=5), A2_PROVISIONAL.version),          # 범위 밖
    (_claim(s=[0.99]), "a9-unknown"),                                  # 모르는 정책
])
def test_no_label(claim, policy):
    assert records.confidence_label(claim, policy) is None


def test_serialize_run_carries_label_per_claim():
    run = EvidenceRun(id=uuid.uuid4(), chat_id=uuid.uuid4(), conversation_id=uuid.uuid4(), user_id=uuid.uuid4(),
                      status="done", trigger="auto", company="c", corp_code="1", rcept_no="r", passages=[],
                      policy_version=A2_PROVISIONAL.version, jev_model="m", generator_model="g",
                      calls=0, cache_hits=0, input_tokens=0, created_at=records.now())
    out = records.serialize_run(run, [_claim(s=[0.9]), _claim(status="no_evidence", s=[0.2], source_idx=None)])
    assert [c["confidence"] for c in out["claims"]] == ["높음", None]


def test_previous_provisional_runs_keep_confidence_label():
    # 비주장 규칙 보강 전 정책("a2-provisional")으로 저장된 실행도 같은 τ_s로 라벨을 낸다
    assert records.confidence_label(_claim(s=[0.1, 0.95], source_idx=1), "a2-provisional") == "높음"


@pytest.mark.parametrize("s_v, want", [
    (0.85, "보통"),    # a2-v1 τ_s
    (0.99, "보통"),    # τ_s + 0.15 = 1.0 미만(사전등록 τ_s와 spec 3.3 구간을 그대로 적용한 결과)
    (1.0, "높음"),
])
def test_a2_v1_band(s_v, want):
    assert records.confidence_label(_claim(s=[0.1, s_v], source_idx=1), "a2-v1") == want


def test_a2_v1_lex_high_claim_has_no_label():
    # 1차 필터 상단 구간(lex_high) ✅는 JEV 확률이 없어 확신도 라벨을 내지 않는다
    assert records.confidence_label(_claim(s=None, source_idx=0), "a2-v1") is None


def _run_row(policy_version, status="done"):
    return EvidenceRun(id=uuid.uuid4(), chat_id=uuid.uuid4(), conversation_id=uuid.uuid4(), user_id=uuid.uuid4(),
                       status=status, trigger="auto", company="c", corp_code="1", rcept_no="r", passages=[],
                       policy_version=policy_version, jev_model="m", generator_model="g",
                       calls=0, cache_hits=0, input_tokens=0, created_at=records.now())


@pytest.mark.parametrize("policy_version, status, latest, want", [
    ("a2-provisional", "done", True, False),       # 비주장 머리말 규칙 이전 실행: 문장을 다시 나누지 않으므로 제외
    ("a2-provisional-2", "done", True, True),
    ("a2-provisional-2", "partial", True, True),
    ("a2-provisional-2", "done", False, False),    # 최신 실행만
    ("a2-provisional-2", "failed", True, False),   # 저장된 확률이 없다(다시 판정으로)
    ("a2-provisional-2", "limited", True, False),
    ("a2-provisional-2", "skipped", True, False),
    ("a2-provisional-2", "running", True, False),
    ("a2-v1", "done", True, False),                # 현재 정책은 재판정 대상이 아니다
])
def test_rejudgeable_only_previous_policy_runs(policy_version, status, latest, want):
    assert records.serialize_run(_run_row(policy_version, status), [], latest=latest)["rejudgeable"] is want
