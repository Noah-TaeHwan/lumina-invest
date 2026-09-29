"""XAI: LightGBM SHAP 기여도 설명이 일관된 구조와 문장을 만들어야 한다."""
import numpy as np
import pytest

lgb = pytest.importorskip("lightgbm")

from app.services import xai
from app.services.quant_pipeline import feature_engineer, preprocess, train_lgb, FEATURE_COLS
from tests.conftest import make_candles


def test_explain_signal_structure_and_consistency():
    df = feature_engineer(preprocess(make_candles(500)))
    model, _ = train_lgb(df)
    x = df[FEATURE_COLS].iloc[-1]
    probs = model.predict(x.values.reshape(1, -1))[0]
    ex = xai.explain_signal(model, x.values, FEATURE_COLS, x.to_dict(), probs)

    assert ex["signal"] in (-1, 0, 1) and ex["signal_label"] == xai.SIGNAL_LABEL[ex["signal"]]
    assert len(ex["contributions"]) == len(FEATURE_COLS)
    contribs = [abs(r["contribution"]) for r in ex["contributions"]]
    assert contribs == sorted(contribs, reverse=True)           # 기여도 절대값 내림차순
    assert all(r["label"] and "value_text" in r for r in ex["contributions"])
    assert ex["signal_label"] in ex["summary"] and "%" in ex["summary"]
    assert abs(sum(ex["class_probabilities"].values()) - 100) < 0.5
    # SHAP 기여도 + base = 예측 클래스의 raw score (TreeSHAP 가법성)
    raw = model.predict(x.values.reshape(1, -1), raw_score=True)[0]
    cls = int(np.argmax(probs))
    total = sum(r["contribution"] for r in ex["contributions"]) + ex["base_value"]
    assert total == pytest.approx(float(raw[cls]), abs=1e-2)


def test_feature_meta_covers_all_features():
    assert set(FEATURE_COLS) <= set(xai.FEATURE_META)
