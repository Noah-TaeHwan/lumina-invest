# tests/evidence/test_a2_select.py
"""A-2 조정 세트 선택 규칙(spec 6.3절): τ_s 0.55~0.95·정밀도 0.93·최소 30개·실패 시 0.85, θ_low·θ_high."""
import pytest

from lab.evidence import a2


def _jev_row(s, y, valid=True):
    """지지 확률 s(한 문단), 반박 0, 숫자 확인 valid, 라벨 지지됨 여부 y."""
    return {"s": [s], "c": [0.0], "valid": [valid], "y": int(y), "label": "supported" if y else "no_evidence"}


def test_grids_and_constants():
    assert a2.TAU_S_GRID == [0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95]
    assert a2.THETA_GRID[0] == 0.05 and a2.THETA_GRID[-1] == 0.95 and len(a2.THETA_GRID) == 19
    assert (a2.TAU_C, a2.TAU_S_MIN_PRECISION, a2.MIN_PREDICTED, a2.TAU_S_FALLBACK) == (0.35, 0.93, 30, 0.85)
    assert (a2.THETA_LOW_MAX_SUPPORTED, a2.THETA_HIGH_MIN_PRECISION, a2.MIN_HIGH_BAND) == (0.03, 0.97, 30)


def test_tau_s_picks_smallest_candidate_with_precision_at_least_093():
    # s=0.62: 정답 27 + 오답 3, s=0.72: 정답 28 + 오답 2
    rows = [_jev_row(0.62, 1)] * 27 + [_jev_row(0.62, 0)] * 3 + [_jev_row(0.72, 1)] * 28 + [_jev_row(0.72, 0)] * 2
    # 0.55·0.6: 60개 중 55 = 0.917 탈락. 0.65·0.7: 30개 중 28 = 0.933 ≥ 0.93 → 0.65
    got = a2.choose_tau_s(rows)
    assert got["tau_s"] == 0.65 and got["fallback"] is False
    assert got["candidates"][0] == {"tau_s": 0.55, "predicted": 60, "correct": 55, "precision": pytest.approx(55 / 60)}


def test_tau_s_precision_exactly_093_passes():
    rows = [_jev_row(0.9, 1)] * 93 + [_jev_row(0.9, 0)] * 7
    assert a2.choose_tau_s(rows)["tau_s"] == 0.55


def test_tau_s_needs_at_least_30_predicted():
    rows = [_jev_row(0.9, 1)] * 29 + [_jev_row(0.6, 0)] * 10
    got = a2.choose_tau_s(rows)  # 0.65 이상은 29개(정밀도 1.0)뿐 → 버림, 0.55·0.6은 정밀도 0.74
    assert got["tau_s"] == 0.85 and got["fallback"] is True
    rows = [_jev_row(0.9, 1)] * 30 + [_jev_row(0.6, 0)] * 10
    assert a2.choose_tau_s(rows)["tau_s"] == 0.65


def test_tau_s_ignores_failed_number_check_and_uses_fixed_tau_c():
    rows = [_jev_row(0.9, 0, valid=False)] * 40 + [_jev_row(0.9, 1)] * 30
    assert a2.choose_tau_s(rows)["tau_s"] == 0.55  # 숫자 확인 실패 문단은 지지 근거가 아니다
    contra = [{"s": [0.9], "c": [0.95], "valid": [True], "y": 0, "label": "contradicted"}] * 40
    assert a2.choose_tau_s(contra + [_jev_row(0.9, 1)] * 30)["tau_s"] == 0.55  # 반박 우선(τ_c 0.35)


def test_boundary_share():
    rows = [_jev_row(0.62, 1), _jev_row(0.70, 1), _jev_row(0.74, 1), _jev_row(0.9, 0)]
    assert a2.boundary_share(rows, 0.70) == pytest.approx(2 / 4)  # |S_V − τ_s| ≤ 0.05


def _lex_row(lex, y, high_ok=True):
    return {"lex": lex, "y": int(y), "high_ok": high_ok}


def test_theta_low_is_largest_with_supported_rate_at_most_003():
    rows = [_lex_row(0.1, 0)] * 100 + [_lex_row(0.3, 1)] * 3 + [_lex_row(0.3, 0)] * 97 + [_lex_row(0.6, 1)] * 50
    # θ ≤ 0.3: 하단 0.1 행만 → 0 / 100. θ 0.35~0.6: 3 / 200 = 0.015. θ ≥ 0.65: 53 / 250 > 0.03
    got = a2.choose_theta_low(rows)
    assert got["theta_low"] == 0.6
    rows = [_lex_row(0.1, 0)] * 97 + [_lex_row(0.1, 1)] * 3  # 정확히 0.03 → 통과
    assert a2.choose_theta_low(rows)["theta_low"] == 0.95


def test_theta_low_none_when_every_band_is_empty_or_too_supported():
    assert a2.choose_theta_low([_lex_row(0.97, 1)] * 10)["theta_low"] is None  # 모든 후보에서 하단이 빈다
    assert a2.choose_theta_low([_lex_row(0.01, 1)] * 10)["theta_low"] is None


def test_theta_high_smallest_with_precision_097_and_30_claims():
    rows = [_lex_row(0.95, 1)] * 30 + [_lex_row(0.7, 1)] * 29 + [_lex_row(0.7, 0)] * 3
    got = a2.choose_theta_high(rows)  # θ ≤ 0.7: 59 / 62 = 0.95 탈락, θ 0.75~0.95: 30 / 30 → 0.75
    assert got["theta_high"] == 0.75
    rows = [_lex_row(0.95, 1)] * 29
    assert a2.choose_theta_high(rows)["theta_high"] is None  # 30개 미만
    rows = [_lex_row(0.95, 1)] * 30 + [_lex_row(0.95, 0, high_ok=False)] * 30
    assert a2.choose_theta_high(rows)["theta_high"] == 0.05  # 회사명·숫자 조건 실패 주장은 상단 구간 밖


def test_theta_selection_reads_only_labels_and_lex():
    """MCA 2.3(b): θ는 AI 참조 라벨로만 고른다. JEV 확률(s·c)이 없는 행으로도 돌아야 한다."""
    rows = [{"lex": 0.1, "y": 0, "high_ok": True}] * 40
    a2.choose_theta_low(rows)
    a2.choose_theta_high(rows)


def test_theta_low_requires_high_ok_key():
    with pytest.raises(KeyError):
        a2.choose_theta_low([{"lex": 0.1, "y": 0}])
