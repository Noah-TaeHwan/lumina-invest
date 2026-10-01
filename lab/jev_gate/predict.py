"""판정력(H-predict) 통계 — AUC, Brier, 보정표, 로지스틱 기준선, 일 단위 블록 부트스트랩."""
from __future__ import annotations

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score


def auc(y, s) -> float | None:
    """라벨이 한 종류뿐이면 None."""
    y = np.asarray(y)
    return None if len(np.unique(y)) < 2 else float(roc_auc_score(y, s))


def brier(y, p) -> float:
    """확률과 0/1 라벨의 평균 제곱 오차."""
    return float(np.mean((np.asarray(p, float) - np.asarray(y, float)) ** 2))


def calibration_table(y, p, bins: int = 10) -> list[dict]:
    """확률을 같은 폭 구간으로 나눠 구간별 평균 확률과 실제 실패 비율을 낸다."""
    y, p = np.asarray(y, float), np.asarray(p, float)
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    return [{"bin_lo": float(edges[b]), "bin_hi": float(edges[b + 1]), "n": int((idx == b).sum()),
             "mean_p": float(p[idx == b].mean()) if (idx == b).any() else None,
             "fail_rate": float(y[idx == b].mean()) if (idx == b).any() else None} for b in range(bins)]


def fit_logistic(X, y) -> dict:
    """표준화 후 로지스틱 회귀. JSON으로 저장할 수 있게 파라미터만 반환한다."""
    X = np.asarray(X, float)
    mean, scale = X.mean(axis=0), X.std(axis=0)
    scale[scale == 0] = 1.0
    m = LogisticRegression(max_iter=1000).fit((X - mean) / scale, y)
    return {"mean": mean.tolist(), "scale": scale.tolist(), "coef": m.coef_[0].tolist(),
            "intercept": float(m.intercept_[0])}


def logistic_proba(model: dict, X) -> np.ndarray:
    """fit_logistic 파라미터로 실패 확률을 계산한다."""
    z = ((np.asarray(X, float) - model["mean"]) / model["scale"]) @ np.asarray(model["coef"]) + model["intercept"]
    return 1 / (1 + np.exp(-z))


def block_bootstrap_auc_diff(y, s1, s2, days, n_boot: int, seed: int) -> dict:
    """UTC 일 단위로 묶어 복원 추출한 AUC(s1) − AUC(s2)의 점추정과 95% 구간. s2가 None이면 0.5와 비교."""
    y, s1, days = np.asarray(y), np.asarray(s1, float), np.asarray(days)
    s2 = None if s2 is None else np.asarray(s2, float)

    def diff(ix):
        a = auc(y[ix], s1[ix])
        b = 0.5 if s2 is None else auc(y[ix], s2[ix])
        return None if a is None or b is None else a - b

    point = diff(np.arange(len(y)))
    uniq = np.unique(days)
    groups = {d: np.flatnonzero(days == d) for d in uniq}
    rng = np.random.default_rng(seed)
    samples = []
    for _ in range(n_boot):
        ix = np.concatenate([groups[d] for d in rng.choice(uniq, size=len(uniq), replace=True)])
        v = diff(ix)
        if v is not None:
            samples.append(v)
    lo, hi = np.percentile(samples, [2.5, 97.5]) if samples else (None, None)
    return {"diff": point, "lo": None if lo is None else float(lo), "hi": None if hi is None else float(hi),
            "n_boot_used": len(samples)}


def _fmt(v, pct=False):
    if v is None:
        return "-"
    return f"{v:.1%}" if pct else f"{v:.3f}"


def render_report(s: dict) -> str:
    """판정력 리포트 Markdown. 주장은 홀드아웃 결과와 사전등록 규칙으로만 한다."""
    lines = ["# JEV Gate Lab — Stage 1 판정력 리포트", "",
             "라벨은 지연 0 가상 거래(다음 5분봉 시가 진입, 코드 청산)의 비용 차감 손실 여부다. "
             "JEV p_fail을 무작위(0.5)·개발 구간으로 학습한 로지스틱 회귀와 비교한다.", "",
             "## 구간별", "", "| 구간 | 후보 | JEV 응답 커버리지 | 실패 비율 | JEV AUC | 로지스틱 AUC | JEV Brier |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for name, p in s["periods"].items():
        lines.append(f"| {name} | {p['n']} | {_fmt(p['coverage'], True)} | {_fmt(p['fail_rate'], True)} | "
                     f"{_fmt(p['auc_jev'])} | {_fmt(p['auc_logistic'])} | {_fmt(p['brier_jev'])} |")
    h = s.get("holdout")
    lines += ["", "## 홀드아웃 비교 (일 단위 블록 부트스트랩 95% 구간)", ""]
    if h:
        for label, d in (("JEV − 0.5", h["vs_half"]), ("JEV − 로지스틱", h["vs_logistic"])):
            lines.append(f"- {label}: {_fmt(d['diff'])} (95% {_fmt(d['lo'])} ~ {_fmt(d['hi'])}, "
                         f"유효 반복 {d['n_boot_used']})")
        lines += ["", "| p_fail 구간 | 후보 | 평균 p_fail | 실제 실패 비율 |", "|---|---:|---:|---:|"]
        for r in h["calibration"]:
            if r["n"]:
                lines.append(f"| {r['bin_lo']:.1f}~{r['bin_hi']:.1f} | {r['n']} | {_fmt(r['mean_p'])} | "
                             f"{_fmt(r['fail_rate'], True)} |")
    else:
        lines.append("- 아직 홀드아웃 응답이 없다.")
    lines += ["", "## 판단 규칙(사전등록)", "", s["claim"], ""]
    return "\n".join(lines)
