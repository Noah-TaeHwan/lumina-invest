"""XAI(설명 가능한 AI) — LightGBM 방향성 분류 모델의 예측 근거를 SHAP 기여도로 풀어 설명한다.

LightGBM은 `predict(..., pred_contrib=True)`로 TreeSHAP 기여도를 네이티브로 제공한다
(shap 패키지 불필요). 다중분류에서는 (피처 수 + 1[bias]) × 클래스 수 길이의 벡터가 나오며,
예측된 클래스 구간만 잘라 각 피처가 그 판단을 얼마나 밀어 올렸는지/끌어내렸는지 계산한다.

같은 로직이 sagemaker/train.py(배치)에도 복제되어 있으므로 피처 정의를 바꾸면 함께 갱신한다.
"""
from __future__ import annotations

from typing import Any

import numpy as np

SIGNAL_LABEL = {1: "매수", 0: "관망", -1: "매도"}

# 피처별 한글 라벨 + 값 해석 문구
FEATURE_META: dict[str, dict] = {
    "ret_1":      {"label": "1일 수익률",        "fmt": lambda v: f"{v*100:+.2f}%",
                   "interp": lambda v: "단기 급등" if v > 0.03 else "단기 급락" if v < -0.03 else "소폭 변동"},
    "ret_5":      {"label": "5일 수익률",        "fmt": lambda v: f"{v*100:+.2f}%",
                   "interp": lambda v: "1주 강세" if v > 0.05 else "1주 약세" if v < -0.05 else "1주 보합"},
    "ret_20":     {"label": "20일 수익률",       "fmt": lambda v: f"{v*100:+.2f}%",
                   "interp": lambda v: "1개월 상승 추세" if v > 0.08 else "1개월 하락 추세" if v < -0.08 else "1개월 횡보"},
    "ma5_ratio":  {"label": "종가/MA5",          "fmt": lambda v: f"{v:.3f}",
                   "interp": lambda v: "5일선 위" if v > 1.01 else "5일선 아래" if v < 0.99 else "5일선 근접"},
    "ma20_ratio": {"label": "종가/MA20",         "fmt": lambda v: f"{v:.3f}",
                   "interp": lambda v: "20일선 위(단기 상승 추세)" if v > 1.02 else "20일선 아래(단기 하락 추세)" if v < 0.98 else "20일선 근접"},
    "rsi":        {"label": "RSI(14)",           "fmt": lambda v: f"{v:.1f}",
                   "interp": lambda v: "과매도 구간(반등 기대)" if v < 30 else "과매수 구간(조정 경계)" if v > 70 else "중립 구간"},
    "macd":       {"label": "MACD",              "fmt": lambda v: f"{v:+.2f}",
                   "interp": lambda v: "상승 모멘텀(양수)" if v > 0 else "하락 모멘텀(음수)"},
    "macd_hist":  {"label": "MACD 히스토그램",   "fmt": lambda v: f"{v:+.2f}",
                   "interp": lambda v: "모멘텀 강화 중" if v > 0 else "모멘텀 약화 중"},
    "bb_width":   {"label": "볼린저 밴드폭",     "fmt": lambda v: f"{v:.3f}",
                   "interp": lambda v: "변동성 확대" if v > 0.15 else "변동성 수축(스퀴즈)" if v < 0.06 else "변동성 보통"},
    "bb_pos":     {"label": "볼린저 밴드 내 위치","fmt": lambda v: f"{v:.2f}",
                   "interp": lambda v: "하단 밴드 근접(과매도)" if v < 0.2 else "상단 밴드 근접(과매수)" if v > 0.8 else "밴드 중앙"},
    "vol_ratio":  {"label": "거래량/20일 평균",  "fmt": lambda v: f"{v:.2f}배",
                   "interp": lambda v: "거래량 급증" if v > 1.8 else "거래량 감소" if v < 0.6 else "거래량 평균 수준"},
    "atr":        {"label": "ATR(14)",           "fmt": lambda v: f"{v:,.1f}",
                   "interp": lambda v: "일중 변동폭"},
}


def _meta(feature: str) -> dict:
    return FEATURE_META.get(feature, {"label": feature, "fmt": lambda v: f"{v:.3f}", "interp": lambda v: ""})


def lgb_contributions(model: Any, x_row: np.ndarray, feature_names: list[str], predicted_class: int) -> tuple[list[dict], float]:
    """예측 클래스에 대한 피처별 SHAP 기여도 [{feature, contribution}] 와 bias(기대값)."""
    contrib = np.asarray(model.predict(np.asarray(x_row, dtype=float).reshape(1, -1), pred_contrib=True))[0]
    n_feat = len(feature_names)
    block = n_feat + 1
    if contrib.size == block:            # 이진/회귀 형태
        seg = contrib
    else:                                 # 다중분류: 클래스별 블록
        seg = contrib[predicted_class * block:(predicted_class + 1) * block]
    rows = [{"feature": f, "contribution": float(seg[i])} for i, f in enumerate(feature_names)]
    return rows, float(seg[-1])


def explain_signal(model: Any, x_row: np.ndarray, feature_names: list[str], feature_values: dict[str, float],
                   probs: np.ndarray | list[float], top_k: int = 3) -> dict:
    """LightGBM 신호를 사람이 읽을 수 있는 설명으로 변환한다."""
    probs = np.asarray(probs, dtype=float).ravel()
    cls = int(np.argmax(probs))              # 0,1,2
    signal = cls - 1
    label = SIGNAL_LABEL[signal]
    prob_pct = float(probs[cls] * 100)

    rows, bias = lgb_contributions(model, x_row, feature_names, cls)
    total_abs = sum(abs(r["contribution"]) for r in rows) or 1.0
    for r in rows:
        meta = _meta(r["feature"])
        v = float(feature_values.get(r["feature"], float("nan")))
        r["label"] = meta["label"]
        r["value"] = None if np.isnan(v) else round(v, 4)
        r["value_text"] = "" if np.isnan(v) else meta["fmt"](v)
        r["interpretation"] = "" if np.isnan(v) else meta["interp"](v)
        r["share_pct"] = round(abs(r["contribution"]) / total_abs * 100, 1)
        r["contribution"] = round(r["contribution"], 4)
    rows.sort(key=lambda r: abs(r["contribution"]), reverse=True)
    positives = [r for r in rows if r["contribution"] > 0][:top_k]
    negatives = [r for r in rows if r["contribution"] < 0][:top_k]

    def _phrase(r: dict) -> str:
        interp = f", {r['interpretation']}" if r["interpretation"] else ""
        return f"{r['label']} {r['value_text']}{interp}"

    parts = [f"모델은 '{label}' 신호를 {prob_pct:.0f}% 확률로 예측했습니다."]
    if positives:
        parts.append("이 판단을 가장 강하게 뒷받침한 요인은 " + " / ".join(_phrase(r) for r in positives) + " 입니다.")
    if negatives:
        parts.append("반대로 " + " / ".join(_phrase(r) for r in negatives) + " 은(는) 이 판단을 약화시켰습니다.")
    if signal == 0:
        parts.append("매수·매도 근거가 서로 상쇄되어 관망이 권고됩니다.")
    return {
        "method": "SHAP (LightGBM TreeSHAP · pred_contrib)",
        "signal": signal,
        "signal_label": label,
        "probability_pct": round(prob_pct, 1),
        "class_probabilities": {"매도": round(float(probs[0]) * 100, 1), "관망": round(float(probs[1]) * 100, 1), "매수": round(float(probs[2]) * 100, 1)} if probs.size == 3 else {},
        "base_value": round(bias, 4),
        "contributions": rows,
        "top_positive": positives,
        "top_negative": negatives,
        "summary": " ".join(parts),
        "disclaimer": "SHAP 기여도는 모델이 '왜 그렇게 판단했는지'를 설명할 뿐, 미래 수익을 보장하지 않습니다.",
    }
