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
