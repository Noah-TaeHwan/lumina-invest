# Gate Lab — Stage 1 Plan 1 (판정력, H-predict) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 모든 구간의 5분봉 돌파 후보에 판정 모델 실패 확률을 받아, 지연 0 가상 거래의 손실 여부를 얼마나 잘 가려내는지(AUC)를 무작위·로지스틱 기준선과 비교하는 리포트를 만든다.

**Architecture:** `lab/jev_gate/`에 후보 표(`candidates.py`)와 판정력 통계(`predict.py`)를 더하고, CLI에 `stage1-call`·`stage1-freeze`·`stage1-report`를 붙인다. 판정 모델 캐시는 Stage 0 v3 호출 기록을 그대로 공유해 같은 입력을 다시 과금하지 않는다. 홀드아웃·공개 이후 구간은 동결 파일(`prereg_holdout.json`)이 사전등록과 일치할 때만 호출한다.

**Tech Stack:** Python 3.12, pandas 3.0.6, numpy 2.5.3, httpx 0.28.1, scikit-learn 1.9.1(컨테이너와 같은 버전). 새 의존성 없음(`requirements.txt`에 이미 있음).

**Spec:** `docs/superpowers/specs/2026-10-01-jev-gate-lab-design.md` (특히 15절 개정 v3)

범위 밖(Plan 2): aggTrades 기반 실측 지연 체결, 비교 갈래 6개의 수익 백테스트, τ 선택, 무작위 차단 1,000 시드, 오염 점검(원본 vs 익명 입력), 국면별 결과, 비용 민감도.

## Global Constraints

- 테스트 명령(저장소 루트): `uv run -q --no-project --python 3.12 --with pandas==3.0.6 --with numpy==2.5.3 --with httpx==0.28.1 --with scikit-learn==1.9.1 --with pytest python -m pytest tests/lab -p no:cacheprovider --basetemp=<workspace>/pytest-tmp`
- CLI 명령: `uv run -q --no-project --python 3.12 --with pandas==3.0.6 --with numpy==2.5.3 --with httpx==0.28.1 --with scikit-learn==1.9.1 python -m lab.jev_gate <command>`
- 저장소 전체 테스트는 앱 이미지 `lumina-portfolio-app`에서 읽기 전용 마운트로 돌린다(macOS lightgbm libomp 부재).
- 판정 모델 `jev-1.13.0`, 예산 하드 상한은 사전등록에 둔다(Stage 0 v3 호출 포함 누적, 금액은 문서에 적지 않는다), 순차 호출, 연속 실패 3회 중단.
- 홀드아웃·공개 이후 구간 후보는 동결 파일 없이 호출하지 않는다. 홀드아웃은 한 번만 연다.
- 지연 0 체결가 = 다음 5분봉 시가. 근거: 2026-03-10 하루 5분봉 마감 287개에서 aggTrades의 "마감 이후 첫 체결가"와 다음 1분봉 시가가 100% 일치(중앙값 163ms 뒤). spec 2절 체결 정의와 같은 값이다.
- 라벨: 같은 코드 청산(손절 2·ATR, 익절 3·ATR, 24봉 시간청산)으로 낸 비용 차감 수익률이 0 미만이면 1(실패). 모든 비교에 같은 라벨을 쓴다.
- 주장은 홀드아웃 결과로만 한다. AUC 옆에 95% 신뢰구간을 같이 적는다.
- Stage 1 코드는 별도 git worktree에서 작업한다(기본 checkout은 예약된 Stage 0 v3 세션 작업이 쓴다).
- 한국어 docstring, 테스트 파일명 `tests/lab/test_lab_*.py`, 네트워크 없는 테스트.

## Review Focus

1. **청산 체결 봉이 데이터 끝을 넘는 후보** — 마지막 몇 시간의 후보는 라벨을 만들 수 없다. 조용히 0으로 두지 말고 표에서 빼야 한다. → Task 2 `test_candidates_skip_when_exit_fill_missing`
2. **한 구간 안에서 라벨이 한 종류뿐** — AUC를 정의할 수 없다. 예외로 죽지 말고 `None`으로 보고해야 한다. → Task 3 `test_auc_single_class_is_none`
3. **판정 모델 응답 모델이 바뀜** — 별칭이 다른 버전을 가리키면 확률이 섞인다. 실패로 차단하고 캐시하지 않아야 한다. → Task 1 `test_model_mismatch_is_blocked_and_not_cached`
4. **동결 후 사전등록이 바뀜** — 동결 파일의 사전등록 해시가 현재와 다르면 홀드아웃 호출을 거부해야 한다. → Task 4 `test_holdout_requires_matching_freeze`
5. **일부 후보만 판정 모델 응답을 받은 상태에서 리포트** — 응답 없는 후보는 AUC에서 빠지고 리포트에 비율이 보여야 한다. → Task 5 `test_report_shows_coverage`

---

### Task 1: 응답 모델 불일치 차단

**Files:**
- Modify: `lab/jev_gate/gate.py` (`_call`의 스키마 검사 직후)
- Test: `tests/lab/test_lab_gate.py`

**Interfaces:**
- Produces: `GateResult.error == "model_mismatch"`(ok=False, status=200)

- [ ] **Step 1: 실패하는 테스트**

```python
def test_model_mismatch_is_blocked_and_not_cached(tmp_path):
    def other_model():
        return httpx.Response(200, json={"model": "jev-1.14.0", "answers": {"fail": {"type": "noul", "noul": 0.4}},
                                         "usage": {"input_tokens": 500, "output_tokens": 20}})
    rec = Recorder(other_model)
    g = _gate(tmp_path / "calls.jsonl", rec)
    r = g.ask(STATE)
    assert (r.ok, r.error) == (False, "model_mismatch")
    g.ask(STATE)
    assert len(rec.requests) == 2
```

- [ ] **Step 2: RED 확인** — 테스트 명령에 `tests/lab/test_lab_gate.py`. Expected: FAIL (`ok`가 True)
- [ ] **Step 3: 구현** — `gate.py` `_call`에서 `if not 0.0 <= p <= 1.0: raise ValueError(p)` 블록 다음, `return GateResult(... True ...)` 앞에:

```python
        if model != MODEL:
            return fail("model_mismatch", 200)
```

- [ ] **Step 4: GREEN 확인** — Expected: 14 passed
- [ ] **Step 5: 커밋** — `fix(lab): 응답 모델이 고정 버전과 다르면 차단`

---

### Task 2: 후보 표와 지연 0 라벨

**Files:**
- Create: `lab/jev_gate/candidates.py`
- Test: `tests/lab/test_lab_candidates.py`

**Interfaces:**
- Consumes: `features.compute_features`, `features.build_state`, `features.STATE_FEATURES`, `rule.find_candidates`, `rule.find_exit`, `rule.net_return`, `stage0.period_mask`
- Produces: `candidates.build_table(k: pd.DataFrame, pre: dict) -> pd.DataFrame` — 열: `period`(dev|validation|holdout|post_release|None), `bar`, `t_close`, `day`(UTC `YYYY-MM-DD`), `key`(gate.cache_key(state)), `state`(dict), STATE_FEATURES 11개, `exit_reason`, `net_ret`, `label`(int 0/1)

- [ ] **Step 1: 실패하는 테스트** — `tests/lab/test_lab_candidates.py`

```python
"""Stage 1 후보 표 검증."""
import numpy as np
import pandas as pd

from lab.jev_gate import candidates, features as ft, gate, rule
from tests.lab.bars import MINUTE_US, T0_US

PRE = {"rule": {"n_grid": [3]}, "costs": {"taker_fee_rate": 0.001, "slippage_bps": 1.0},
       "periods": {"dev": ["2025-10-01", "2025-10-01"], "holdout": ["2025-10-02", "2025-10-02"]}}


def _bars(closes, opens=None):
    closes = np.asarray(closes, dtype=float)
    opens = closes if opens is None else np.asarray(opens, dtype=float)
    n = len(closes)
    t = T0_US + np.arange(n, dtype=np.int64) * 5 * MINUTE_US
    vol = np.where(np.arange(n) % 2 == 0, 10.0, 20.0)
    return pd.DataFrame({"open_time": t, "open": opens, "high": np.maximum(opens, closes) + 0.5,
                         "low": np.minimum(opens, closes) - 0.5, "close": closes, "volume": vol,
                         "taker_buy_base": vol * 0.5, "t_close": t + 5 * MINUTE_US})


def test_entry_is_next_bar_open_and_label_matches_net_return():
    closes = [100.0] * 300 + [101.0] + [100.0] * 60
    opens = list(closes)
    opens[301] = 100.7                      # 다음 봉 시가가 진입가
    t = candidates.build_table(_bars(closes, opens), PRE)
    row = t.iloc[0]
    assert row["bar"] == 300
    exit_bar, reason = rule.find_exit(np.asarray(closes), 301, 100.7, row["atr14"])
    expected = rule.net_return(100.7, opens[exit_bar + 1], 0.001, 1.0)
    assert row["exit_reason"] == reason and row["net_ret"] == expected
    assert row["label"] == int(expected < 0)
    assert row["key"] == gate.cache_key(row["state"])
    assert set(row["state"]["features"]) == set(ft.STATE_FEATURES)


def test_candidates_skip_when_exit_fill_missing():
    closes = [100.0] * 300 + [101.0] + [100.0] * 5      # 24봉 시간청산 전에 데이터가 끝남
    assert len(candidates.build_table(_bars(closes), PRE)) == 0


def test_period_and_day_columns():
    closes = [100.0] * 300 + [101.0] + [100.0] * 60
    row = candidates.build_table(_bars(closes), PRE).iloc[0]
    assert row["day"] == "2025-10-02" and row["period"] == "holdout"   # 300번 봉 마감 = 10-02 01:05 UTC
```

- [ ] **Step 2: RED 확인** — Expected: ImportError(`candidates`)
- [ ] **Step 3: 구현** — `lab/jev_gate/candidates.py`

```python
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
    fee, slip = pre["costs"]["taker_fee_rate"], pre["costs"]["slippage_bps"]
    rows = []
    for i in range(len(c)):
        r = c.iloc[i]
        entry_bar = int(r["bar"]) + 1
        if entry_bar >= len(f):
            continue
        entry = opens[entry_bar]
        exit_bar, reason = rule.find_exit(closes, entry_bar, entry, float(r["atr14"]))
        if reason == "end" or exit_bar + 1 >= len(f):
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
```

- [ ] **Step 4: GREEN 확인** — Expected: 3 passed
- [ ] **Step 5: 커밋** — `feat(lab): Stage 1 후보 표와 지연 0 라벨`

---

### Task 3: 판정력 통계

**Files:**
- Create: `lab/jev_gate/predict.py`
- Test: `tests/lab/test_lab_predict.py`

**Interfaces:**
- Produces: `predict.auc(y, s) -> float | None`, `predict.brier(y, p) -> float`, `predict.calibration_table(y, p, bins=10) -> list[dict]` (`bin_lo, bin_hi, n, mean_p, fail_rate`), `predict.fit_logistic(X: np.ndarray, y) -> dict` (`mean, scale, coef, intercept` 리스트/실수), `predict.logistic_proba(model: dict, X) -> np.ndarray`, `predict.block_bootstrap_auc_diff(y, s1, s2, days, n_boot, seed) -> dict` (`diff, lo, hi, n_boot_used`). `s2=None`이면 s1의 AUC − 0.5.

- [ ] **Step 1: 실패하는 테스트** — `tests/lab/test_lab_predict.py`

```python
"""판정력 통계 검증."""
import numpy as np
import pytest

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
```

- [ ] **Step 2: RED 확인** — Expected: ImportError(`predict`)
- [ ] **Step 3: 구현** — `lab/jev_gate/predict.py`

```python
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
    lo, hi = (np.percentile(samples, [2.5, 97.5]) if samples else (None, None))
    return {"diff": point, "lo": None if lo is None else float(lo), "hi": None if hi is None else float(hi),
            "n_boot_used": len(samples)}
```

- [ ] **Step 4: GREEN 확인** — Expected: 5 passed
- [ ] **Step 5: 커밋** — `feat(lab): 판정력 통계(AUC·보정·로지스틱·블록 부트스트랩)`

---

### Task 4: 사전등록 v4와 Stage 1 CLI(호출·동결)

**Files:**
- Modify: `lab/jev_gate/prereg.json` (version 4, `stage1` 블록), `lab/jev_gate/__main__.py`
- Test: `tests/lab/test_lab_stage1_cli.py`, `tests/lab/test_lab_prereg.py`

**Interfaces:**
- Consumes: Task 1~3
- Produces: `cli.Stage1Paths(root, pre)`(`results`, `freeze`, `report`, `calls`(= Stage 0 v3 호출 기록 경로)), `cli.load_table(paths, pre) -> pd.DataFrame`, `cli.cmd_stage1_call(paths, pre, period, gate_factory=None, table=None)`, `cli.cmd_stage1_freeze(paths, pre, table=None)`, `cli.check_freeze(paths, pre)`

사전등록 v4 추가분:

```json
"stage1": {
  "results_dir": "lab/results/stage1",
  "report": "docs/lab/stage1-predict-report.md",
  "freeze_file": "lab/jev_gate/prereg_holdout.json",
  "calls_file": "lab/results/stage0-v3/jev_calls.jsonl",
  "bootstrap": {"n": 2000, "seed": 20261002},
  "calibration_bins": 10,
  "claim_rule": "홀드아웃 JEV AUC − 0.5의 95% 구간 하한 > 0 이고 JEV − 로지스틱 AUC 차의 점추정 ≥ 0 일 때만 판정력이 있다고 쓴다"
}
```

- [ ] **Step 1: 실패하는 테스트** — `tests/lab/test_lab_stage1_cli.py`

```python
"""Stage 1 CLI 검증 — 가짜 JEV와 합성 후보 표."""
import json

import httpx
import pandas as pd
import pytest

import lab.jev_gate.__main__ as cli
from lab.jev_gate import features as ft, gate


def _table(n_per=40):
    rows = []
    for period in ("dev", "validation", "holdout", "post_release"):
        for i in range(n_per):
            state = {"features": {"i": i, "p": period}}
            rows.append({"period": period, "bar": i, "t_close": i, "day": f"2025-10-{1 + i % 20:02d}",
                         "key": gate.cache_key(state), "state": state, "label": i % 2,
                         "net_ret": -0.01 if i % 2 else 0.01, "exit_reason": "time", "atr14": 1.0,
                         **{f: float(i % 7) for f in ft.STATE_FEATURES}})
    return pd.DataFrame(rows)


class Fake:
    def __init__(self):
        self.calls = 0

    def __call__(self, paths, pre):
        def handler(request):
            self.calls += 1
            body = json.loads(request.content)
            p = 0.7 if body["state"]["features"]["i"] % 2 else 0.3
            return httpx.Response(200, json={"model": "jev-1.13.0", "answers": {"fail": {"type": "noul", "noul": p}},
                                             "usage": {"input_tokens": 800, "output_tokens": 20}})
        return gate.JevGate(paths.calls, client=httpx.Client(transport=httpx.MockTransport(handler)), api_key="k")


def _paths(tmp_path):
    pre = cli.load_prereg()
    return cli.Stage1Paths(tmp_path, pre), pre


def test_holdout_requires_matching_freeze(tmp_path):
    paths, pre = _paths(tmp_path)
    t, fake = _table(), Fake()
    with pytest.raises(SystemExit, match="동결"):
        cli.cmd_stage1_call(paths, pre, "holdout", gate_factory=fake, table=t)
    assert fake.calls == 0 and not paths.calls.exists()
    cli.cmd_stage1_freeze(paths, pre, table=t)
    frozen = json.loads(paths.freeze.read_text())
    frozen["prereg_sha256"] = "0" * 64
    paths.freeze.write_text(json.dumps(frozen))
    with pytest.raises(SystemExit, match="동결"):
        cli.cmd_stage1_call(paths, pre, "holdout", gate_factory=fake, table=t)


def test_dev_call_then_freeze_then_holdout(tmp_path):
    paths, pre = _paths(tmp_path)
    t, fake = _table(), Fake()
    cli.cmd_stage1_call(paths, pre, "dev", gate_factory=fake, table=t)
    assert fake.calls == 40
    cli.cmd_stage1_freeze(paths, pre, table=t)
    frozen = json.loads(paths.freeze.read_text())
    assert frozen["question_sha256"] == gate.question_hash() and len(frozen["logistic"]["coef"]) == 11
    cli.cmd_stage1_call(paths, pre, "holdout", gate_factory=fake, table=t)
    cli.cmd_stage1_call(paths, pre, "holdout", gate_factory=fake, table=t)   # 재실행은 캐시
    assert fake.calls == 80


def test_freeze_refuses_to_overwrite(tmp_path):
    paths, pre = _paths(tmp_path)
    t = _table()
    cli.cmd_stage1_freeze(paths, pre, table=t)
    with pytest.raises(SystemExit, match="이미"):
        cli.cmd_stage1_freeze(paths, pre, table=t)
```

`tests/lab/test_lab_prereg.py`에 추가:

```python
def test_prereg_v4_stage1_block():
    assert PREREG["version"] == 4
    s1 = PREREG["stage1"]
    assert s1["calls_file"] == PREREG["stage0"]["results_dir"] + "/jev_calls.jsonl"
    assert set(s1) >= {"results_dir", "report", "freeze_file", "bootstrap", "claim_rule"}
```

(같은 파일의 `test_prereg_v3_five_minute_bars_and_separate_results`의 `== 3`은 `>= 3`으로 바꾼다.)

- [ ] **Step 2: RED 확인** — Expected: `AttributeError: Stage1Paths`, `KeyError: 'stage1'`
- [ ] **Step 3: 구현** — `prereg.json`: version 4, `amended`에 "v4 — Stage 1 Plan 1 설정 추가(홀드아웃 열기 전)" 덧붙임, 위 `stage1` 블록. `__main__.py`에 추가:

```python
from lab.jev_gate import candidates, predict  # 기존 import 줄에 합친다


class Stage1Paths:
    """Stage 1 입출력 경로. JEV 호출 기록은 Stage 0 v3와 공유한다(같은 입력 재과금 방지)."""

    def __init__(self, root: Path, pre: dict):
        s1 = pre["stage1"]
        self.raw = root / "lab/data/raw"
        self.results = root / s1["results_dir"]
        self.report = root / s1["report"]
        self.freeze = root / s1["freeze_file"]
        self.calls = root / s1["calls_file"]
        self.summary = self.results / "summary.json"


def load_table(paths, pre: dict):
    """전 구간 1분봉을 받아 봉 주기로 묶고 후보 표를 만든다."""
    periods = pre["periods"]
    months = data.month_range(periods["dev"][0][:7], periods["post_release"][1][:7])
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        k = data.load_klines_range(pre["symbol"], months, paths.raw, client)
    return candidates.build_table(data.resample_klines(k, pre.get("bar_minutes", 1)), pre)


def _prereg_sha() -> str:
    return hashlib.sha256(PREREG_PATH.read_bytes()).hexdigest()


def check_freeze(paths, pre: dict) -> dict:
    """동결 파일이 있고 현재 사전등록·질문과 일치해야 한다."""
    frozen = _read_json(paths.freeze, None)
    if frozen is None:
        raise SystemExit("홀드아웃 동결 파일이 없습니다. stage1-freeze를 먼저 실행하세요")
    if frozen["prereg_sha256"] != _prereg_sha() or frozen["question_sha256"] != gate.question_hash():
        raise SystemExit("동결 이후 사전등록이나 질문이 바뀌었습니다. 홀드아웃을 열 수 없습니다")
    return frozen


def cmd_stage1_call(paths, pre: dict, period: str, gate_factory=None, table=None) -> int:
    """한 구간의 모든 후보에 JEV를 순차 호출한다. 홀드아웃·공개 이후 구간은 동결 확인 후에만."""
    if period not in pre["periods"]:
        raise SystemExit(f"period는 {list(pre['periods'])} 중 하나여야 합니다")
    if period in ("holdout", "post_release"):
        check_freeze(paths, pre)
    t = load_table(paths, pre) if table is None else table
    states = list(t.loc[t["period"] == period, "state"])
    g = (gate_factory or _default_gate)(paths, pre)
    _ask_each(g, states, tag=f"stage1-{period}")
    return 0


def cmd_stage1_freeze(paths, pre: dict, table=None) -> int:
    """개발 구간으로 로지스틱 기준선을 학습하고, 사전등록·질문 해시와 함께 동결한다(덮어쓰지 않음)."""
    if paths.freeze.exists():
        raise SystemExit("이미 동결했습니다. 동결 파일은 덮어쓰지 않습니다")
    t = load_table(paths, pre) if table is None else table
    dev = t[t["period"] == "dev"]
    model = predict.fit_logistic(dev[list(features.STATE_FEATURES)].to_numpy(float), dev["label"].to_numpy())
    _write_json(paths.freeze, {"frozen_at": datetime.now(timezone.utc).isoformat(), "prereg_sha256": _prereg_sha(),
                               "question_sha256": gate.question_hash(), "dev_candidates": int(len(dev)),
                               "features": list(features.STATE_FEATURES), "logistic": model})
    print(f"동결: {paths.freeze} (dev {len(dev)}건)")
    return 0
```

`main()`에 서브커맨드 `stage1-call --period P`, `stage1-freeze`, `stage1-report`를 추가하고 `Stage1Paths(args.root, pre)`로 연결한다.

- [ ] **Step 4: GREEN 확인** — Expected: lab 전체 통과(신규 3 + prereg 1)
- [ ] **Step 5: 커밋** — `feat(lab): Stage 1 호출·홀드아웃 동결 CLI와 사전등록 v4`

---

### Task 5: Stage 1 판정력 리포트

**Files:**
- Modify: `lab/jev_gate/__main__.py` (`cmd_stage1_report`), `lab/jev_gate/predict.py` (`render_report`)
- Test: `tests/lab/test_lab_stage1_cli.py`

**Interfaces:**
- Produces: `cli.cmd_stage1_report(paths, pre, table=None) -> dict` (구간별 `n`, `coverage`, `auc_jev`, `auc_logistic`, `brier_jev`, 홀드아웃 `vs_half`·`vs_logistic` 부트스트랩, `calibration`, `claim`), `predict.render_report(summary: dict) -> str`

- [ ] **Step 1: 실패하는 테스트** — `tests/lab/test_lab_stage1_cli.py`에 추가

```python
def test_report_shows_coverage(tmp_path):
    paths, pre = _paths(tmp_path)
    t, fake = _table(), Fake()
    cli.cmd_stage1_call(paths, pre, "dev", gate_factory=fake, table=t)
    cli.cmd_stage1_freeze(paths, pre, table=t)
    cli.cmd_stage1_call(paths, pre, "holdout", gate_factory=fake, table=t)
    s = cli.cmd_stage1_report(paths, pre, table=t)
    assert s["periods"]["dev"]["coverage"] == 1.0
    assert s["periods"]["validation"]["coverage"] == 0.0 and s["periods"]["validation"]["auc_jev"] is None
    assert s["periods"]["holdout"]["auc_jev"] == 1.0          # 가짜 JEV가 라벨과 완전히 일치
    text = paths.report.read_text(encoding="utf-8")
    assert "커버리지" in text and "95%" in text and s["claim"] in text
```

- [ ] **Step 2: RED 확인** — Expected: AttributeError(`cmd_stage1_report`)
- [ ] **Step 3: 구현**

`__main__.py`:

```python
def cmd_stage1_report(paths, pre: dict, table=None) -> dict:
    """구간별 판정력과 홀드아웃 부트스트랩을 계산해 리포트를 쓴다."""
    s1 = pre["stage1"]
    t = load_table(paths, pre) if table is None else table
    p_by_key = {}
    for r in _read_jsonl(paths.calls):
        if r["ok"] and r["key"] not in p_by_key:
            p_by_key[r["key"]] = r["p_fail"]
    frozen = _read_json(paths.freeze, None)
    cols = list(features.STATE_FEATURES)
    out = {"periods": {}}
    for period in pre["periods"]:
        sub = t[t["period"] == period]
        have = sub[sub["key"].isin(p_by_key)]
        y = have["label"].to_numpy()
        p = have["key"].map(p_by_key).to_numpy(float)
        lg = predict.logistic_proba(frozen["logistic"], have[cols].to_numpy(float)) if frozen and len(have) else None
        out["periods"][period] = {
            "n": int(len(sub)), "coverage": float(len(have) / len(sub)) if len(sub) else 0.0,
            "fail_rate": float(sub["label"].mean()) if len(sub) else None,
            "auc_jev": predict.auc(y, p) if len(have) else None,
            "auc_logistic": predict.auc(y, lg) if lg is not None else None,
            "brier_jev": predict.brier(y, p) if len(have) else None}
    hold = t[(t["period"] == "holdout") & t["key"].isin(p_by_key)]
    b = s1["bootstrap"]
    if len(hold) and frozen:
        y, p = hold["label"].to_numpy(), hold["key"].map(p_by_key).to_numpy(float)
        lg = predict.logistic_proba(frozen["logistic"], hold[cols].to_numpy(float))
        days = hold["day"].to_numpy()
        out["holdout"] = {"vs_half": predict.block_bootstrap_auc_diff(y, p, None, days, b["n"], b["seed"]),
                          "vs_logistic": predict.block_bootstrap_auc_diff(y, p, lg, days, b["n"], b["seed"]),
                          "calibration": predict.calibration_table(y, p, s1["calibration_bins"])}
    out["claim"] = s1["claim_rule"]
    _write_json(paths.summary, out)
    paths.report.parent.mkdir(parents=True, exist_ok=True)
    paths.report.write_text(predict.render_report(out), encoding="utf-8")
    return out
```

`predict.py`:

```python
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
            lines.append(f"| {r['bin_lo']:.1f}~{r['bin_hi']:.1f} | {r['n']} | {_fmt(r['mean_p'])} | "
                         f"{_fmt(r['fail_rate'], True)} |")
    else:
        lines.append("- 아직 홀드아웃 응답이 없다.")
    lines += ["", "## 판단 규칙(사전등록)", "", s["claim"], ""]
    return "\n".join(lines)
```

- [ ] **Step 4: GREEN 확인** — lab 전체 통과, 저장소 전체(앱 이미지) 통과
- [ ] **Step 5: 커밋** — `feat(lab): Stage 1 판정력 리포트`

---

### Task 6: 실행 (Stage 0 v3 GO 이후)

- [ ] **Step 1:** Stage 0 v3 판정이 GO이고 PR #6이 병합됐는지 확인. 아니면 멈춘다.
- [ ] **Step 2:** `stage1-call --period dev`, `stage1-call --period validation` (약 1,620회, 순차 약 6분). 연속 실패 시 재실행으로 이어서.
- [ ] **Step 3:** `stage1-report`로 개발·검증 구간 판정력 확인(홀드아웃 열기 전 중간 확인). 이 결과를 보고 무엇도 바꾸지 않는다. 바꿔야 하면 사전등록 v5와 시도 원장을 남기고 동결 전에 처리.
- [ ] **Step 4:** `stage1-freeze` → `lab/jev_gate/prereg_holdout.json` 커밋(홀드아웃 열기 전 기록이 Git에 남아야 한다).
- [ ] **Step 5:** `stage1-call --period holdout`, `stage1-call --period post_release` (약 610회).
- [ ] **Step 6:** `stage1-report` → `docs/lab/stage1-predict-report.md`, `lab/results/stage1/summary.json`. API 키 노출 검사 후 커밋.
- [ ] **Step 7:** 최종 브랜치 리뷰(독립 리뷰어) → 수정 → PR 병합 → Vault 인계 갱신.
