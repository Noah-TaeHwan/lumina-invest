"""Stage 0 — 실현 가능성 확인. 지연·실패·반복 일관성·비용·후보 수만 보고 수익성은 판단하지 않는다."""
from __future__ import annotations

from collections import Counter

import numpy as np
import pandas as pd

from lab.jev_gate.gate import PRICE_PER_INPUT_TOKEN


def _utc_micros(day: str) -> int:
    return int(pd.Timestamp(day, tz="UTC").timestamp() * 1_000_000)


def period_mask(t_close: pd.Series, start: str, end: str) -> pd.Series:
    """UTC 날짜 start~end(양 끝 포함) 안에서 마감한 봉이면 True."""
    lo = _utc_micros(start)
    hi = _utc_micros(end) + 86_400_000_000
    return (t_close > lo) & (t_close <= hi)


def rule_stats(n_candidates: int, trades: pd.DataFrame) -> dict:
    """규칙 단독 근사 성과 요약."""
    net = trades["net_ret"].to_numpy(dtype=float)
    if len(net) == 0:
        return {"candidates": int(n_candidates), "trades": 0, "net_compound": 0.0, "net_mean": 0.0,
                "gross_mean": 0.0, "win_rate": 0.0, "reasons": {}}
    return {"candidates": int(n_candidates), "trades": int(len(net)),
            "net_compound": float(np.prod(1 + net) - 1), "net_mean": float(net.mean()),
            "gross_mean": float(trades["gross_ret"].astype(float).mean()),
            "win_rate": float((net > 0).mean()),
            "reasons": {str(k): int(v) for k, v in Counter(trades["reason"]).items()}}


def choose_n(stats: dict[int, dict], min_candidates: int) -> int:
    """후보 수 기준을 만족하는 N 중 비용 차감 누적 수익률이 가장 높은 N(동률이면 작은 N)."""
    eligible = sorted(n for n, s in stats.items() if s["candidates"] >= min_candidates)
    if not eligible:
        raise ValueError("후보 수 기준을 만족하는 N이 없습니다")
    return max(eligible, key=lambda n: stats[n]["net_compound"])


def sample_indices(n_total: int, size: int, seed: int) -> list[int]:
    """seed 고정 비복원 무작위 표본(오름차순)."""
    rng = np.random.default_rng(seed)
    return sorted(int(i) for i in rng.choice(n_total, size=min(size, n_total), replace=False))


def call_summary(records: list[dict]) -> dict:
    """호출 기록(JSONL 행)에서 지연 분위수·실패율·스키마 유효율·토큰·모델·오류를 요약한다."""
    lat = np.array([r["latency_ms"] for r in records], dtype=float)
    ok = np.array([r["ok"] for r in records], dtype=bool)
    http200 = np.array([r["status"] == 200 for r in records], dtype=bool)
    tokens = [r["input_tokens"] for r in records if r["ok"]]
    return {
        "calls": int(len(records)),
        "p50_ms": float(np.percentile(lat, 50)),
        "p95_ms": float(np.percentile(lat, 95)),
        "p99_ms": float(np.percentile(lat, 99)),
        "failure_rate": float(1 - ok.mean()),
        "schema_valid_rate": float(ok[http200].mean()) if http200.any() else 0.0,
        "mean_input_tokens": float(np.mean(tokens)) if tokens else 0.0,
        "models": sorted({r["model"] for r in records if r["model"]}),
        "errors": dict(Counter(r["error"] for r in records if r["error"])),
    }


def repeat_agreement(groups: dict[str, list[float]]) -> dict:
    """같은 입력 반복 호출의 0.5 기준 판정 일치율(다수 판정과 같은 비율의 평균)과 표준편차 평균."""
    agree, stds = [], []
    for ps in groups.values():
        labels = [p >= 0.5 for p in ps]
        majority = sum(labels) * 2 > len(labels)
        agree.append(sum(label == majority for label in labels) / len(labels))
        stds.append(float(np.std(ps)))
    return {"items": len(groups), "agreement": float(np.mean(agree)) if agree else 0.0,
            "mean_std": float(np.mean(stds)) if stds else 0.0}


def project_full_run(total_candidates: int, mean_input_tokens: float, p50_ms: float) -> dict:
    """모든 후보를 순차 호출할 때의 비용·시간 추정."""
    return {"calls": int(total_candidates),
            "cost_usd": total_candidates * mean_input_tokens * PRICE_PER_INPUT_TOKEN,
            "hours_sequential": total_candidates * p50_ms / 3_600_000}


def evaluate_go(summary: dict, th: dict) -> dict[str, bool]:
    """사전등록 기준별 통과 여부."""
    c = summary["calls"]
    return {
        "latency_p99": c["p99_ms"] <= th["latency_p99_ms"],
        "failure_rate": c["failure_rate"] <= th["failure_rate_max"],
        "schema_valid": c["schema_valid_rate"] >= th["schema_valid_min"],
        "repeat_agreement": summary["repeat"]["agreement"] >= th["repeat_agreement_min"],
        "dev_candidates": summary["dev_candidates"] >= th["dev_candidates_min"],
        "projected_cost": summary["projection"]["cost_usd"] <= th["projected_cost_max_usd"],
    }


def stage0_notes(selected_rule: dict) -> list[str]:
    """리포트에 남길 해석 메모. 선택 N의 규칙 단독 성과가 비용 전에도 0 이하이면 spec 7절 대응을 명시한다."""
    notes = ["표본 300건은 시간순으로 정렬돼 세션마다 개발 구간의 다른 시기(약 3분의 1씩)를 맡고, "
             "반복 측정 50건은 가장 이른 시기에서 뽑혔다. 세션 간 지연 차이를 해석할 때 이 배정을 함께 본다."]
    if selected_rule["gross_mean"] <= 0:
        notes.append("선택 N의 규칙 단독 성과가 비용 전에도 거래당 0 이하다. spec 7절에 따라 Stage 1 전에 규칙을 다시 "
                     "고르거나, 게이트 효과를 '손실 전략 차단'으로만 해석한다고 명시한다.")
    return notes


def render_report(summary: dict, go: dict[str, bool], th: dict) -> str:
    """Stage 0 결과 Markdown. 세션·반복 측정이 끝나기 전에는 PARTIAL로 표시한다."""
    verdict = "PARTIAL" if not summary["complete"] else ("GO" if all(go.values()) else "NO-GO")
    c, rep, proj = summary["calls"], summary["repeat"], summary["projection"]
    mark = {True: "통과", False: "미달"}
    rows = [
        ("지연 p99", f"{c['p99_ms']:.0f} ms", f"≤ {th['latency_p99_ms']} ms", go["latency_p99"]),
        ("실패·타임아웃율", f"{c['failure_rate']:.2%}", f"≤ {th['failure_rate_max']:.0%}", go["failure_rate"]),
        ("스키마 유효율", f"{c['schema_valid_rate']:.2%}", f"≥ {th['schema_valid_min']:.0%}", go["schema_valid"]),
        ("반복 판정 일치율", f"{rep['agreement']:.2%} ({rep['items']}건)", f"≥ {th['repeat_agreement_min']:.0%}",
         go["repeat_agreement"]),
        ("개발 구간 후보 수", f"{summary['dev_candidates']}", f"≥ {th['dev_candidates_min']}", go["dev_candidates"]),
        ("전체 예상 비용", f"${proj['cost_usd']:.4f}", f"≤ ${th['projected_cost_max_usd']}", go["projected_cost"]),
    ]
    lines = [
        "# JEV Gate Lab — Stage 0 리포트",
        "",
        f"판정: {verdict}",
        "",
        "Stage 0은 수익성이 아니라 실현 가능성(지연·실패·일관성·후보 수·비용)만 본다. "
        "지연은 서울 가정용 네트워크에서 순차 호출로 잰 값이다.",
        "",
        "## 통과 기준",
        "",
        "| 기준 | 측정값 | 사전등록 기준 | 결과 |",
        "|---|---|---|---|",
        *[f"| {name} | {value} | {limit} | {mark[ok]} |" for name, value, limit, ok in rows],
        "",
        "## JEV 호출",
        "",
        f"- 호출 {c['calls']}회, 지연 p50 {c['p50_ms']:.0f} ms / p95 {c['p95_ms']:.0f} ms / p99 {c['p99_ms']:.0f} ms",
        f"- 응답 모델: {', '.join(c['models']) or '없음'}, 호출당 평균 입력 토큰 {c['mean_input_tokens']:.0f}",
        f"- 오류: {c['errors'] or '없음'}",
        f"- 반복 측정 확률 표준편차 평균: {rep['mean_std']:.4f}",
        f"- 전체 후보 {proj['calls']}건 순차 호출 추정: ${proj['cost_usd']:.4f}, {proj['hours_sequential']:.2f}시간",
        "",
        "## 세션별 지연",
        "",
        "| 세션 | 시작(UTC) | 호출 | p50 | p99 |",
        "|---|---|---|---|---|",
    ]
    starts = {f"session-{s['session']}": s["started_at"] for s in summary["sessions"]}
    for tag, s in summary["by_session"].items():
        lines.append(f"| {tag} | {starts.get(tag, '-')} | {s['calls']} | {s['p50_ms']:.0f} ms | {s['p99_ms']:.0f} ms |")
    lines += ["", f"## 규칙 단독 근사 성과 (개발 구간, 선택 N = {summary['n_selected']})", "",
              "1분봉 종가 진입·청산 근사이며 지연·체결 모형은 Stage 1에서 적용한다.", "",
              "| N | 후보 | 거래 | 비용 차감 누적 | 거래당 순수익 평균 | 비용 전 평균 | 승률 |",
              "|---|---|---|---|---|---|---|"]
    for n, s in summary["rule"].items():
        lines.append(f"| {n} | {s['candidates']} | {s['trades']} | {s['net_compound']:.2%} | "
                     f"{s['net_mean']:.4%} | {s['gross_mean']:.4%} | {s['win_rate']:.1%} |")
    lines += ["", f"구간별 후보 수(선택 N): {summary['candidate_counts']}", ""]
    if summary.get("notes"):
        lines += ["## 해석 메모", "", *[f"- {note}" for note in summary["notes"]], ""]
    return "\n".join(lines)
