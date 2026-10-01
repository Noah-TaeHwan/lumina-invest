"""판정력 통계 검증."""
import numpy as np

from lab.jev_gate import predict


def test_auc_perfect_and_inverse():
    y = np.array([0, 0, 1, 1])
    assert predict.auc(y, np.array([0.1, 0.2, 0.8, 0.9])) == 1.0
    assert predict.auc(y, np.array([0.9, 0.8, 0.2, 0.1])) == 0.0


def test_auc_single_class_is_none():
    assert predict.auc(np.array([1, 1, 1]), np.array([0.2, 0.5, 0.9])) is None


def test_brier_and_calibration():
    y = np.array([0, 1, 1, 0])
    p = np.array([0.0, 1.0, 1.0, 0.0])
    assert predict.brier(y, p) == 0.0
    table = predict.calibration_table(np.array([0, 1] * 50), np.linspace(0, 1, 100), bins=10)
    assert len(table) == 10 and sum(r["n"] for r in table) == 100


def test_logistic_fits_separable_signal_and_roundtrips():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(400, 3))
    y = (X[:, 0] + 0.1 * rng.normal(size=400) > 0).astype(int)
    model = predict.fit_logistic(X, y)
    p = predict.logistic_proba(model, X)
    assert predict.auc(y, p) > 0.95
    assert set(model) == {"mean", "scale", "coef", "intercept"}


def test_block_bootstrap_detects_real_difference_and_is_deterministic():
    rng = np.random.default_rng(1)
    n = 600
    y = rng.integers(0, 2, n)
    good = y + rng.normal(0, 0.5, n)
    noise = rng.normal(0, 1, n)
    days = np.repeat(np.arange(60), 10)
    a = predict.block_bootstrap_auc_diff(y, good, noise, days, n_boot=300, seed=7)
    b = predict.block_bootstrap_auc_diff(y, good, noise, days, n_boot=300, seed=7)
    assert a == b and a["lo"] > 0
    vs_half = predict.block_bootstrap_auc_diff(y, noise, None, days, n_boot=300, seed=7)
    assert vs_half["lo"] < 0 < vs_half["hi"]
