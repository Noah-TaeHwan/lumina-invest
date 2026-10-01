"""Lumina JEV Gate Lab 명령줄 (Stage 0).

  python -m lab.jev_gate stage0-rule               1분봉 수집, N별 규칙 근사 통계, N 선택, 표본 추출
  python -m lab.jev_gate stage0-call --session K   표본의 K번째 묶음을 순차 호출(세션 간 최소 간격 강제)
  python -m lab.jev_gate stage0-repeat             표본 앞부분을 반복 호출(입력당 총 repeat_calls회)
  python -m lab.jev_gate stage0-report             요약·통과 판정 리포트와 시도 원장 기록

--root로 저장소 루트를 바꿀 수 있다(테스트용).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np

from lab.jev_gate import data, features, gate, rule, stage0

PREREG_PATH = Path(__file__).with_name("prereg.json")
REPO_ROOT = Path(__file__).resolve().parents[2]
MAX_CONSECUTIVE_FAILURES = 3  # 연속 실패가 이만큼이면 세션을 멈춘다(로컬 장애가 실패율을 오염시키지 않게)


class Paths:
    """Stage 0 입출력 경로 모음. 세션 시작·종료 시각은 파일로 두지 않고 호출 기록에서 계산한다."""

    def __init__(self, root: Path):
        self.raw = root / "lab/data/raw"
        self.results = root / "lab/results/stage0"
        self.rule = self.results / "rule_stats.json"
        self.sample = self.results / "sample.json"
        self.calls = self.results / "jev_calls.jsonl"
        self.summary = self.results / "summary.json"
        self.report = root / "docs/lab/stage0-report.md"
        self.attempts = root / "lab/attempts.jsonl"


def load_prereg() -> dict:
    """사전등록 파일을 읽는다."""
    return json.loads(PREREG_PATH.read_text(encoding="utf-8"))


def _read_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _default_gate(paths: Paths, pre: dict) -> gate.JevGate:
    return gate.JevGate(paths.calls, budget_usd=pre["jev"]["budget_usd"])


def _session_calls(records: list[dict]) -> dict[int, list[dict]]:
    """session-K 태그가 붙은 호출 기록을 세션 번호별로 묶는다."""
    out: dict[int, list[dict]] = defaultdict(list)
    for r in records:
        tag = str(r.get("tag") or "")
        if tag.startswith("session-"):
            out[int(tag.split("-", 1)[1])].append(r)
    return out


def _ok_keys(records: list[dict]) -> set[str]:
    return {r["key"] for r in records if r["ok"]}


def _batch(sample: list[dict], s0: dict, session: int) -> list[dict]:
    """표본을 세션 수로 나눈 session번째 묶음(마지막 세션이 나머지를 가진다)."""
    size = len(sample) // s0["sessions"]
    end = None if session == s0["sessions"] else session * size
    return sample[(session - 1) * size: end]


def _require_sample(paths: Paths) -> list[dict]:
    sample = _read_json(paths.sample, None)
    if sample is None:
        raise SystemExit("표본이 없습니다. stage0-rule을 먼저 실행하세요")
    return sample


def _ask_each(g: gate.JevGate, states: list[dict], tag: str, use_cache: bool = True) -> None:
    """순차 호출한다. 연속 실패가 한도에 닿으면 남은 호출을 하지 않고 멈춘다(재실행하면 이어서 채운다)."""
    streak = 0
    for i, state in enumerate(states, 1):
        r = g.ask(state, use_cache=use_cache, tag=tag)
        print(f"[{tag}] {i}/{len(states)} ok={r.ok} p_fail={r.p_fail} {r.latency_ms:.0f}ms "
              f"cached={r.cached} error={r.error}", flush=True)
        streak = 0 if r.ok else streak + 1
        if streak >= MAX_CONSECUTIVE_FAILURES:
            raise SystemExit(f"연속 실패 {streak}회로 중단했습니다. 원인을 확인한 뒤 같은 명령을 다시 실행하세요")
    print(f"누적 비용 ${g.spent_usd:.6f}")


def cmd_stage0_rule(paths: Paths, pre: dict) -> int:
    """전 구간 1분봉을 받고, 개발 구간에서만 N별 규칙 근사 성과를 내어 N을 고른 뒤 표본을 뽑는다."""
    periods, costs, s0 = pre["periods"], pre["costs"], pre["stage0"]
    months = data.month_range(periods["dev"][0][:7], periods["post_release"][1][:7])
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        k = data.load_klines_range(pre["symbol"], months, paths.raw, client)
    f = features.compute_features(k)
    dev_rows = np.flatnonzero(stage0.period_mask(f["t_close"], *periods["dev"]).to_numpy())
    f_dev = f.iloc[: dev_rows[-1] + 1]  # 개발 구간 끝에서 잘라 다음 구간 가격을 쓰지 않는다
    stats, cands_by_n = {}, {}
    for n in pre["rule"]["n_grid"]:
        cands = rule.find_candidates(f, n)
        dev_c = cands[stage0.period_mask(cands["t_close"], *periods["dev"])]
        trades = rule.rule_only_close_approx(f_dev, dev_c, costs["taker_fee_rate"], costs["slippage_bps"])
        stats[n], cands_by_n[n] = stage0.rule_stats(len(dev_c), trades), cands
    out = {"bars": int(len(k)), "months": months, "stats": {str(n): s for n, s in stats.items()}}
    try:
        n_sel = stage0.choose_n(stats, s0["thresholds"]["dev_candidates_min"])
    except ValueError as e:
        _write_json(paths.rule, {**out, "n_selected": None, "candidate_counts": {}})
        print(f"NO-GO: {e}")
        return 2
    cands = cands_by_n[n_sel]
    counts = {name: int(stage0.period_mask(cands["t_close"], *rng).sum()) for name, rng in periods.items()}
    dev_c = cands[stage0.period_mask(cands["t_close"], *periods["dev"])].reset_index(drop=True)
    idx = stage0.sample_indices(len(dev_c), s0["sample_size"], s0["sample_seed"])
    sample = [{"bar": int(dev_c.loc[i, "bar"]), "t_close": int(dev_c.loc[i, "t_close"]),
               "state": features.build_state(dev_c.loc[i])} for i in idx]
    _write_json(paths.rule, {**out, "n_selected": n_sel, "candidate_counts": counts})
    _write_json(paths.sample, sample)
    print(f"bars={len(k)} N={n_sel} counts={counts} sample={len(sample)}")
    for n, s in stats.items():
        print(f"  N={n}: {s}")
    return 0


def cmd_stage0_call(paths: Paths, pre: dict, session: int, gate_factory=None, now: datetime | None = None) -> int:
    """표본의 session번째 묶음을 순차 호출한다.

    이전 세션의 묶음이 모두 성공 응답을 받았고, 그 세션의 마지막 호출 후 최소 간격이 지나야 한다.
    시각은 실제 호출 기록(called_at)으로만 판단하므로, 호출 전에 죽은 실행은 세션 시작으로 남지 않는다.
    """
    s0 = pre["stage0"]
    if not 1 <= session <= s0["sessions"]:
        raise SystemExit(f"session은 1~{s0['sessions']} 사이여야 합니다")
    now = now or datetime.now(timezone.utc)
    sample = _require_sample(paths)
    if session > 1:
        prev_calls = _session_calls(_read_jsonl(paths.calls)).get(session - 1, [])
        prev_ok = _ok_keys(prev_calls)
        if not prev_calls or not all(gate.cache_key(it["state"]) in prev_ok
                                     for it in _batch(sample, s0, session - 1)):
            raise SystemExit(f"세션 {session - 1}을 먼저 끝까지 실행하세요")
        gap = now - max(datetime.fromisoformat(r["called_at"]) for r in prev_calls)
        if gap < timedelta(hours=s0["session_min_gap_hours"]):
            raise SystemExit(f"세션 {session - 1} 마지막 호출 후 {s0['session_min_gap_hours']}시간이 지나지 않았습니다"
                             f"(경과 {gap})")
    g = (gate_factory or _default_gate)(paths, pre)
    _ask_each(g, [it["state"] for it in _batch(sample, s0, session)], tag=f"session-{session}")
    return 0


def cmd_stage0_repeat(paths: Paths, pre: dict, gate_factory=None) -> int:
    """표본 앞 repeat_items건이 입력당 성공 응답 repeat_calls개가 되도록 더 호출한다.

    실패한 시도는 기록·실패율에 그대로 남고, 재실행하면 모자란 성공 응답만 채운다.
    대상 입력이 모두 세션 1에서 성공 응답을 받은 뒤에만 실행한다.
    """
    s0 = pre["stage0"]
    sample, records = _require_sample(paths), _read_jsonl(paths.calls)
    targets = sample[: s0["repeat_items"]]
    s1_ok = _ok_keys(_session_calls(records).get(1, []))
    if not all(gate.cache_key(it["state"]) in s1_ok for it in targets):
        raise SystemExit("반복 대상이 세션 1에서 모두 성공한 뒤에 실행하세요")
    done = Counter(r["key"] for r in records if r.get("tag") == "repeat" and r["ok"])
    states = [it["state"] for it in targets
              for _ in range(max(0, s0["repeat_calls"] - 1 - done[gate.cache_key(it["state"])]))]
    g = (gate_factory or _default_gate)(paths, pre)
    _ask_each(g, states, tag="repeat", use_cache=False)
    return 0


def cmd_stage0_report(paths: Paths, pre: dict, now: datetime | None = None) -> dict:
    """호출 기록을 요약해 통과 여부를 판정하고 리포트·summary·시도 원장을 쓴다."""
    s0, th = pre["stage0"], pre["stage0"]["thresholds"]
    rs, sample, records = _read_json(paths.rule, None), _require_sample(paths), _read_jsonl(paths.calls)
    if not records:
        raise SystemExit("호출 기록이 없습니다")
    sessions = [{"session": k, "started_at": min(r["called_at"] for r in v), "ended_at": max(r["called_at"] for r in v)}
                for k, v in sorted(_session_calls(records).items())]
    repeat_keys = [gate.cache_key(it["state"]) for it in sample[: s0["repeat_items"]]]
    by_key = defaultdict(list)
    for r in records:
        if r["ok"]:
            by_key[r["key"]].append(r["p_fail"])
    groups = {k: by_key[k][: s0["repeat_calls"]] for k in repeat_keys if len(by_key[k]) >= s0["repeat_calls"]}
    called = _ok_keys([r for v in _session_calls(records).values() for r in v])
    calls = stage0.call_summary(records)
    summary = {
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
        "n_selected": rs["n_selected"], "rule": rs["stats"], "candidate_counts": rs["candidate_counts"],
        "dev_candidates": rs["candidate_counts"].get("dev", 0),
        "calls": calls, "repeat": stage0.repeat_agreement(groups),
        "projection": stage0.project_full_run(sum(rs["candidate_counts"].values()), calls["mean_input_tokens"],
                                              calls["p50_ms"]),
        "sessions": sessions,
        "notes": stage0.stage0_notes(rs["stats"][str(rs["n_selected"])]),
        "by_session": {tag: stage0.call_summary([r for r in records if r.get("tag") == tag])
                       for tag in sorted({r.get("tag") for r in records if r.get("tag")})},
        "complete": (len(sessions) >= s0["sessions"]
                     and all(gate.cache_key(it["state"]) in called for it in sample)
                     and len(groups) >= min(s0["repeat_items"], len(sample))),
    }
    go = stage0.evaluate_go(summary, th)
    verdict = "PARTIAL" if not summary["complete"] else ("GO" if all(go.values()) else "NO-GO")
    _write_json(paths.summary, {**summary, "go": go, "verdict": verdict})
    paths.report.parent.mkdir(parents=True, exist_ok=True)
    paths.report.write_text(stage0.render_report(summary, go, th), encoding="utf-8")
    paths.attempts.parent.mkdir(parents=True, exist_ok=True)
    with paths.attempts.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": summary["generated_at"], "stage": "stage0", "verdict": verdict,
                            "prereg_sha256": hashlib.sha256(PREREG_PATH.read_bytes()).hexdigest(),
                            "n_selected": rs["n_selected"], "calls": calls["calls"]}) + "\n")
    print(f"판정: {verdict} {go}")
    return summary


def main(argv: list[str] | None = None) -> int:
    """명령줄 진입점."""
    p = argparse.ArgumentParser(prog="python -m lab.jev_gate")
    p.add_argument("--root", type=Path, default=REPO_ROOT)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stage0-rule")
    call = sub.add_parser("stage0-call")
    call.add_argument("--session", type=int, required=True)
    sub.add_parser("stage0-repeat")
    sub.add_parser("stage0-report")
    args = p.parse_args(argv)
    paths, pre = Paths(args.root), load_prereg()
    if args.cmd == "stage0-rule":
        return cmd_stage0_rule(paths, pre)
    if args.cmd == "stage0-call":
        return cmd_stage0_call(paths, pre, args.session)
    if args.cmd == "stage0-repeat":
        return cmd_stage0_repeat(paths, pre)
    cmd_stage0_report(paths, pre)
    return 0


if __name__ == "__main__":
    sys.exit(main())
