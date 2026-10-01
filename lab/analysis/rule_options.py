"""Stage 1 규칙 재선택 사전 분석 — 개발 구간(2025-10-01~2026-04-30)만 쓰고 JEV는 호출하지 않는다.

실행: PYTHONPATH=. uv run -q --no-project --python 3.12 --with pandas==3.0.6 --with numpy==2.5.3 \
      --with httpx==0.28.1 python lab/analysis/rule_options.py
결과 정리: docs/lab/stage1-rule-options.md
"""
import json
from pathlib import Path
import httpx, numpy as np, pandas as pd
from lab.jev_gate import data, features, rule, stage0

pre = json.load(open("lab/jev_gate/prereg.json"))
fee, slip = pre["costs"]["taker_fee_rate"], pre["costs"]["slippage_bps"]
cost = 2 * fee + 2 * slip / 10_000
with httpx.Client(timeout=60) as c:
    k1 = data.load_klines_range("BTCUSDT", data.month_range("2025-10", "2026-04"), Path("lab/data/raw"), c)
k1 = k1[stage0.period_mask(k1["t_close"], *pre["periods"]["dev"])].reset_index(drop=True)

def resample(k, minutes):
    g = np.arange(len(k)) // minutes
    out = k.groupby(g).agg(open_time=("open_time", "first"), open=("open", "first"), high=("high", "max"),
                           low=("low", "min"), close=("close", "last"), volume=("volume", "sum"),
                           taker_buy_base=("taker_buy_base", "sum"), t_close=("t_close", "last"))
    return out.reset_index(drop=True)

def evaluate(k, n, max_bars, min_take_cost=None):
    rule.MAX_BARS = max_bars
    f = features.compute_features(k)
    c = rule.find_candidates(f, n)
    ratio = 3 * c["atr14"] / c["close"]
    if min_take_cost:
        c = c[ratio >= min_take_cost * cost].reset_index(drop=True)
        ratio = 3 * c["atr14"] / c["close"]
    t = rule.rule_only_close_approx(f, c, fee, slip)
    s = stage0.rule_stats(len(c), t)
    return {"candidates": s["candidates"], "trades": s["trades"], "gross_mean_pct": s["gross_mean"] * 100,
            "net_mean_pct": s["net_mean"] * 100, "win_rate": s["win_rate"],
            "take_over_cost_median": float((ratio / cost).median()) if len(c) else None}

rows = []
rows.append(("1분봉 N=120 (현재)", evaluate(k1, 120, 120)))
rows.append(("1분봉 N=120 + 익절폭≥비용×2 필터", evaluate(k1, 120, 120, min_take_cost=2)))
k5 = resample(k1, 5)
for n in (12, 24, 48):
    rows.append((f"5분봉 N={n} ({n*5}분 채널), 시간청산 24봉(2시간)", evaluate(k5, n, 24)))
k15 = resample(k1, 15)
rows.append(("15분봉 N=16 (4시간 채널), 시간청산 16봉(4시간)", evaluate(k15, 16, 16)))
print(json.dumps({"cost_round_trip_pct": cost * 100, "rows": rows}, ensure_ascii=False, indent=1))
