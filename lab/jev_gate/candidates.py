"""Stage 1 후보 표 — 모든 구간의 진입 후보, 익명 state, 지연 0 라벨.

지연 0 체결가는 신호 봉 다음 5분봉의 시가다. 5분봉 시가는 그 봉 첫 1분봉의 시가이고, Binance 1분봉 시가는
그 분의 첫 체결이므로 "봉 마감 직후 첫 체결가"와 같다(2026-03-10 하루 287개 마감에서 aggTrades와 100% 일치).
청산도 같은 방식으로 청산 봉 다음 봉 시가에 체결한다. 라벨은 비용 차감 수익률이 0 미만이면 1(실패)이다.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from lab.jev_gate import features, gate, rule, stage0


def build_table(k: pd.DataFrame, pre: dict) -> pd.DataFrame:
    """봉(5분봉) DataFrame에서 후보마다 state·특징·지연 0 라벨을 만든다. 청산 체결 봉이 없는 후보는 뺀다."""
    f = features.compute_features(k)
    c = rule.find_candidates(f, pre["rule"]["n_grid"][0])
    opens, closes = f["open"].to_numpy(float), f["close"].to_numpy(float)
    open_t, close_t = f["open_time"].to_numpy(), f["t_close"].to_numpy()
    fee, slip = pre["costs"]["taker_fee_rate"], pre["costs"]["slippage_bps"]
    rows = []
    for i in range(len(c)):
        r = c.iloc[i]
        entry_bar = int(r["bar"]) + 1
        if entry_bar >= len(f) or open_t[entry_bar] != close_t[entry_bar - 1]:
            continue  # 다음 봉이 없거나 시간상 이어지지 않음
        entry = opens[entry_bar]
        exit_bar, reason = rule.find_exit(closes, entry_bar, entry, float(r["atr14"]))
        if reason == "end" or exit_bar + 1 >= len(f) or open_t[exit_bar + 1] != close_t[exit_bar]:
            continue
        net = rule.net_return(entry, opens[exit_bar + 1], fee, slip)
        state = features.build_state(r)
        rows.append({"bar": int(r["bar"]), "t_close": int(r["t_close"]),
                     "day": datetime.fromtimestamp(int(r["t_close"]) / 1e6, tz=timezone.utc).strftime("%Y-%m-%d"),
                     "key": gate.cache_key(state), "state": state,
                     **{name: float(r[name]) for name in features.STATE_FEATURES},
                     "atr14": float(r["atr14"]), "exit_reason": reason, "net_ret": net, "label": int(net < 0)})
    t = pd.DataFrame(rows)
    if t.empty:
        return t
    t["period"] = None
    for name, (start, end) in pre["periods"].items():
        t.loc[stage0.period_mask(t["t_close"], start, end), "period"] = name
    return t
