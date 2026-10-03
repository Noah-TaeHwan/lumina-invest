# lab/evidence/a2.py
"""A-2 평가(A-2 spec 6절): 자연 주장, 조정 세트 선택 규칙(τ_s·θ), 확인 세트 관문, 정책 a2-v1 매핑. 외부 호출 없음.

- 1차 필터 경로는 제품 실행기와 같은 함수(app lexical.tier_route, lex_features)로 정한다.
- MCA 2.3(b): θ_low·θ_high는 AI 참조 라벨과 어휘 점수만 읽어 고른다(JEV 확률을 정답으로 삼지 않는다).
  τ_s는 JEV 지지 확률에 거는 임계값을 AI 참조 라벨 기준 정밀도로 고른다. 어느 것도 JEV 출력으로 학습하지 않는다.
- 값은 lab/evidence/prereg_a2.json과 같아야 한다(tests/evidence/test_a2_cli.py가 대조한다).
"""
from __future__ import annotations

from app.services.evidence.claims import claim_spans, is_not_claim, not_claim_reason
from app.services.evidence.judge import Judgement, sys_decision
from app.services.evidence.lexical import tier_route

SEED = 20261103
TAU_C = 0.35
TAU_S_GRID = [round(0.55 + 0.05 * i, 2) for i in range(9)]
TAU_S_MIN_PRECISION = 0.93
MIN_PREDICTED = 30
TAU_S_FALLBACK = 0.85
THETA_GRID = [round(0.05 * i, 2) for i in range(1, 20)]
THETA_LOW_MAX_SUPPORTED = 0.03
THETA_HIGH_MIN_PRECISION = 0.97
MIN_HIGH_BAND = 30
BOUNDARY = 0.05
BOOTSTRAP_N = 2000
TOKEN_CAP = 6_000_000
TOKENS_PER_CALL = 5_070  # 호출당 입력 토큰 p95(A-2 spec 2.3절). 예산 추정에만 쓴다
CLAIM_CAP = 8  # 제품 Policy.max_claims와 같다
KAPPA_MIN = 0.6
NOT_CLAIM_MAX_CLAIM_SHARE = 0.05
GATES = {
    "h_prec": {"precision_min": 0.90, "ci_lo_min": 0.80, "min_predicted": 150},
    "h_low": {"supported_rate_max": 0.05, "share_min": 0.20},
    "h_high": {"precision_min": 0.95, "max_drop_vs_sys": 0.02},
    "h_tier": {"auc_diff_lo_min": -0.05, "auc_diff_hi_max": 0.05, "call_reduction_min": 0.30},
}
_EPS = 1e-12


def natural_claims(answer_rows: list[dict], cap: int = CLAIM_CAP) -> list[dict]:
    """답변을 제품과 같은 claim_spans로 나눈다. 비주장 규칙에 걸린 문장도 표시해 남기고(라벨로 규칙을 검증),
    규칙을 통과한 문장은 앞 cap개만 둔다(제품은 그 뒤를 판정하지 않는다)."""
    out = []
    for a in answer_rows:
        kept = 0
        for i, sp in enumerate(claim_spans(a["answer"]), 1):
            rule = is_not_claim(sp.text)
            if not rule:
                if kept >= cap:
                    continue
                kept += 1
            out.append({"cid": f"{a['qid']}-n{i}", "qid": a["qid"], "source": "natural", "text": sp.text,
                        "start": sp.start, "end": sp.end, "not_claim_rule": rule, "variant": None, "expected": None})
    return out


def _sys(r: dict, tau_s: float, tau_c: float) -> tuple[str, int | None, float]:
    return sys_decision(Judgement(r["s"], r["c"], True, 1), r["valid"], tau_s, tau_c)


def precision(rows: list[dict], key: str) -> float | None:
    """key 판정이 supported인 행의 라벨 정밀도. 예측이 없으면 None."""
    sup = [r for r in rows if r[key] == "supported"]
    return sum(r["y"] for r in sup) / len(sup) if sup else None


def choose_tau_s(rows: list[dict], tau_c: float = TAU_C) -> dict:
    """spec 6.3: 후보 0.55~0.95 중 ✅ 예측 30개 이상이고 정밀도 ≥ 0.93인 가장 작은 값. 없으면 0.85."""
    cands = []
    for t in TAU_S_GRID:
        sup = [r for r in rows if _sys(r, t, tau_c)[0] == "supported"]
        correct = sum(r["y"] for r in sup)
        cands.append({"tau_s": t, "predicted": len(sup), "correct": correct,
                      "precision": correct / len(sup) if sup else None})
    ok = [c for c in cands if c["predicted"] >= MIN_PREDICTED and c["precision"] >= TAU_S_MIN_PRECISION]
    return {"tau_s": ok[0]["tau_s"] if ok else TAU_S_FALLBACK, "fallback": not ok, "candidates": cands}


def boundary_share(rows: list[dict], tau_s: float) -> float | None:
    """숫자 확인을 통과한 문단의 지지 확률 최댓값이 τ_s ± 0.05 안에 있는 주장 비율."""
    if not rows:
        return None
    s_v = [max((s for s, v in zip(r["s"], r["valid"]) if v), default=0.0) for r in rows]
    return sum(abs(x - tau_s) <= BOUNDARY + _EPS for x in s_v) / len(rows)


def choose_theta_low(rows: list[dict]) -> dict:
    """spec 6.3: 하단 구간(lex < θ)의 AI 라벨 지지됨 비율이 0.03 이하인 후보 중 가장 큰 값. 빈 구간은 후보가 아니다."""
    cands = []
    for t in THETA_GRID:
        band = [r for r in rows if tier_route(r["lex"], r.get("high_ok", False), t, None) == "lex_low"]
        sup = sum(r["y"] for r in band)
        cands.append({"theta": t, "band": len(band), "supported": sup, "rate": sup / len(band) if band else None})
    ok = [c for c in cands if c["band"] and c["rate"] <= THETA_LOW_MAX_SUPPORTED]
    return {"theta_low": ok[-1]["theta"] if ok else None, "candidates": cands}


def choose_theta_high(rows: list[dict]) -> dict:
    """spec 6.3: 상단 구간(lex ≥ θ, 숫자 확인·회사명 조건 통과)이 30개 이상이고 정밀도 ≥ 0.97인 가장 작은 값."""
    cands = []
    for t in THETA_GRID:
        band = [r for r in rows if tier_route(r["lex"], r["high_ok"], None, t) == "lex_high"]
        correct = sum(r["y"] for r in band)
        cands.append({"theta": t, "band": len(band), "correct": correct,
                      "precision": correct / len(band) if band else None})
    ok = [c for c in cands if c["band"] >= MIN_HIGH_BAND and c["precision"] >= THETA_HIGH_MIN_PRECISION]
    return {"theta_high": ok[0]["theta"] if ok else None, "candidates": cands}


def decide(rows: list[dict], cfg: dict) -> list[dict]:
    """행마다 SYS 단독 판정·점수와 계층형 경로·판정·점수를 붙인다.

    계층형 점수: lex_low 0.0, lex_high 1.0, 나머지는 SYS 점수(반박이면 0).
    """
    out = []
    for r in rows:
        dec, _, score = _sys(r, cfg["tau_s"], cfg["tau_c"])
        route = tier_route(r["lex"], r["high_ok"], cfg.get("theta_low"), cfg.get("theta_high"))
        tdec, tscore = {"lex_low": ("no_evidence", 0.0), "lex_high": ("supported", 1.0)}.get(route, (dec, score))
        out.append({**r, "sys_decision": dec, "sys_score": score, "route": route,
                    "tier_decision": tdec, "tier_score": tscore})
    return out


def call_reduction(pool: list[dict], theta_low: float | None, theta_high: float | None) -> float | None:
    """판정 대상 주장 전체(라벨과 무관) 중 1차 필터가 JEV 없이 확정하는 비율."""
    if not pool:
        return None
    return sum(tier_route(p["lex"], p["high_ok"], theta_low, theta_high) != "jev" for p in pool) / len(pool)


def _auc_diff(rs: list[dict]) -> float | None:
    from lab.evidence.metrics import auc

    y = [r["y"] for r in rs]
    a, b = auc(y, [r["tier_score"] for r in rs]), auc(y, [r["sys_score"] for r in rs])
    return None if a is None or b is None else a - b


def check_gates(rows: list[dict], pool: list[dict], cfg: dict, n_boot: int = BOOTSTRAP_N, seed: int = SEED) -> dict:
    """확인 세트 관문(spec 6.1·6.4). rows: 판정 가능한 자연 주장(라벨·JEV·lex), pool: 규칙 통과 자연 주장 전체의 lex."""
    from lab.evidence.metrics import cluster_bootstrap

    G = GATES
    d = decide(rows, cfg)
    pred = sum(r["sys_decision"] == "supported" for r in d)
    point, lo, hi = cluster_bootstrap(d, lambda rs: precision(rs, "sys_decision"), n=n_boot, seed=seed)
    desc = pred < G["h_prec"]["min_predicted"]
    out = {"h_prec": {"predicted": pred, "precision": point, "ci": [lo, hi], "descriptive_only": desc,
                      "pass": (not desc and point is not None and lo is not None
                               and point >= G["h_prec"]["precision_min"] and lo >= G["h_prec"]["ci_lo_min"])}}

    tl, th = cfg.get("theta_low"), cfg.get("theta_high")
    if tl is None:
        out["h_low"] = {"status": "not_tested", "pass": False}
    else:
        band = [r for r in d if tier_route(r["lex"], r["high_ok"], tl, None) == "lex_low"]
        sup = sum(r["y"] for r in band)
        rate = sup / len(band) if band else None
        share = len(band) / len(d) if d else None
        out["h_low"] = {"status": "tested", "theta_low": tl, "band": len(band), "supported": sup,
                        "supported_rate": rate, "share": share,
                        "pass": bool(band) and rate <= G["h_low"]["supported_rate_max"]
                        and share >= G["h_low"]["share_min"] - _EPS}
    if th is None:
        out["h_high"] = {"status": "not_tested", "pass": False}
    else:
        band = [r for r in d if tier_route(r["lex"], r["high_ok"], None, th) == "lex_high"]
        correct = sum(r["y"] for r in band)
        band_prec = correct / len(band) if band else None
        tier_prec = precision(decide(rows, dict(cfg, theta_low=None)), "tier_decision")
        sys_prec = precision(d, "sys_decision")
        out["h_high"] = {"status": "tested", "theta_high": th, "band": len(band), "correct": correct,
                         "precision": band_prec, "tier_precision": tier_prec, "sys_precision": sys_prec,
                         "pass": bool(band) and band_prec >= G["h_high"]["precision_min"]
                         and tier_prec is not None and sys_prec is not None
                         and tier_prec >= sys_prec - G["h_high"]["max_drop_vs_sys"] - _EPS}

    adopted = [n for n, k in (("low", "h_low"), ("high", "h_high")) if out[k]["pass"]]
    if not adopted:
        out["h_tier"] = {"status": "not_applicable", "adopted": [], "pass": False}
    else:
        tcfg = dict(cfg, theta_low=tl if "low" in adopted else None, theta_high=th if "high" in adopted else None)
        dt = decide(rows, tcfg)
        p, lo2, hi2 = cluster_bootstrap(dt, _auc_diff, n=n_boot, seed=seed)
        red = call_reduction(pool, tcfg["theta_low"], tcfg["theta_high"])
        out["h_tier"] = {"status": "tested", "adopted": adopted, "auc_diff": {"point": p, "lo": lo2, "hi": hi2},
                         "call_reduction": red, "tier_precision": precision(dt, "tier_decision"),
                         "pass": (lo2 is not None and hi2 is not None and lo2 >= G["h_tier"]["auc_diff_lo_min"]
                                  and hi2 <= G["h_tier"]["auc_diff_hi_max"] and red is not None
                                  and red >= G["h_tier"]["call_reduction_min"])}
    return out


def policy_a2_v1(gates: dict, cfg: dict) -> dict:
    """spec 6.4 반영 규칙 표: H-prec 실패면 τ_s 0.85, 통과한 구간만 켠다(H-tier는 보고만 하고 표를 바꾸지 않는다)."""
    ok = gates["h_prec"]["pass"]
    return {"version": "a2-v1", "tau_s": cfg["tau_s"] if ok else TAU_S_FALLBACK, "tau_c": cfg.get("tau_c", TAU_C),
            "theta_low": cfg.get("theta_low") if gates["h_low"]["pass"] else None,
            "theta_high": cfg.get("theta_high") if gates["h_high"]["pass"] else None,
            "precision_target_confirmed": ok}


def check_budget(used: int, estimate: int, cap: int, reserve: int = 0) -> None:
    """사용량 + 이번 추정 + 뒤 단계 예약이 상한을 넘으면 호출 전에 멈춘다."""
    if used + estimate + reserve > cap:
        raise SystemExit(f"token budget: used {used} + estimate {estimate} + reserve {reserve} > cap {cap}")


def latency_summary(rows: list[dict]) -> dict:
    """생성 지연 p50·p95(모델 적재가 낀 첫 호출 제외)와 spec 7.1의 타임아웃 재설정값(p95 × 2)."""
    from lab.evidence.metrics import percentile

    warm = [r["latency_ms"] for r in rows if r.get("latency_ms") is not None and not r.get("cold")]
    cold = sum(1 for r in rows if r.get("cold"))
    if not warm:
        return {"n": 0, "cold_excluded": cold, "p50_ms": None, "p95_ms": None, "max_ms": None, "timeout_s": None}
    p95 = percentile(warm, 95)
    return {"n": len(warm), "cold_excluded": cold, "p50_ms": percentile(warm, 50), "p95_ms": p95,
            "max_ms": max(warm), "timeout_s": 2 * p95 / 1000}


def not_claim_audit(rows: list[dict]) -> dict:
    """spec 5.2: 비주장 규칙에 걸린 문장 중 AI 라벨이 주장(non_claim 아님)인 비율. 갈린 라벨은 뺀다.
    표현별로도 세고, 5%를 넘는 표현은 a2-v1에서 규칙에서 뺄 후보로 낸다."""
    flagged = [r for r in rows if r.get("not_claim_rule") and r.get("label") not in (None, "disputed")]
    claim = [r for r in flagged if r["label"] != "non_claim"]
    by: dict[str, dict] = {}
    for r in flagged:
        b = by.setdefault(not_claim_reason(r["text"]) or "none", {"n": 0, "claim": 0})
        b["n"] += 1
        b["claim"] += r["label"] != "non_claim"
    share = len(claim) / len(flagged) if flagged else None
    return {"flagged": len(flagged), "claim": len(claim), "claim_share": share,
            "over_limit": share is not None and share > NOT_CLAIM_MAX_CLAIM_SHARE, "by_reason": by,
            "drop_phrases": sorted(k[len("phrase:"):] for k, v in by.items()
                                   if k.startswith("phrase:") and v["claim"] / v["n"] > NOT_CLAIM_MAX_CLAIM_SHARE)}
