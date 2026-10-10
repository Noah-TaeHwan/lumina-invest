# Gate Lab — Stage 0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Binance BTCUSDT 1분봉으로 Donchian 돌파 후보를 만들고, 판정 모델 게이트를 실제로 호출해 Stage 0 통과 기준(지연·실패율·일관성·후보 수·비용)을 판정하는 리포트를 만든다.

**Architecture:** `lab/jev_gate/`는 FastAPI 앱과 분리된 독립 패키지다. 데이터 로더 → 익명 특징 → 규칙 → 판정 모델 게이트 → Stage 0 집계 → CLI 순서로 쌓고, 각 모듈은 순수 함수와 작은 클래스 하나로 끝낸다. 외부 호출(Binance, 판정 모델 API)은 `httpx.Client` 주입으로 테스트에서 MockTransport로 대체한다.

**Tech Stack:** Python 3.12, pandas 3.0.6, numpy 2.5.3, httpx 0.28.1, pytest (컨테이너 이미지와 같은 버전). 새 의존성 없음.

**Spec:** `docs/superpowers/specs/2026-10-01-jev-gate-lab-design.md`

이 계획은 spec의 Stage 0까지만 다룬다. aggTrades 체결, 이벤트 재생, 비교 갈래, 본 리포트(Stage 1)는 Stage 0 결과를 본 뒤 별도 계획으로 쓴다.

## Global Constraints

- 테스트 명령(저장소 루트): `uv run --no-project --python 3.12 --with pandas==3.0.6 --with numpy==2.5.3 --with httpx==0.28.1 --with pytest python -m pytest tests/lab -q`
- CLI 명령(저장소 루트): `uv run --no-project --python 3.12 --with pandas==3.0.6 --with numpy==2.5.3 --with httpx==0.28.1 python -m lab.jev_gate <command>`
- 새 의존성 추가 금지. `requirements.txt` 변경 없음.
- 판정 모델 버전 1.13.0 고정, 예산 하드 상한은 사전등록에 둔다(금액은 문서에 적지 않는다).
- API 키 값은 출력·로그·캐시·Git에 남기지 않는다.
- 금지 명칭: "HFT", "초단기", "알파", "수익 보장".
- 모든 모듈·공개 함수에 한국어 docstring(기존 `app/services/ta_utils.py` 형식).
- 테스트 파일명은 `tests/lab/test_lab_*.py`(다른 테스트와 basename 충돌 방지). 테스트 보조 모듈은 `tests/lab/bars.py`.
- 테스트는 네트워크를 쓰지 않는다.
- 커밋은 feature 브랜치 `feat/jev-gate-lab-stage0`에서만. 커밋 전 `/ponytail-review` 후 `bash ~/.agents/hooks/record-ponytail-review.sh`.

## Review Focus

1. **일별 파일이 빠진 달** — 월별 파일이 없고 일별 파일도 하루 빠져 있으면 조용히 건너뛰지 말고 오류로 멈춰야 한다. → Task 1 `test_download_month_missing_day_raises`
2. **거래량 0인 봉** — 나눗셈이 inf를 만들면 판정 모델 입력이 깨진다. NaN이 되어 후보에서 빠져야 한다. → Task 2 `test_zero_volume_bar_gives_nan_not_inf`
3. **세션 도중 중단 후 재실행** — 이미 호출한 입력은 캐시를 써서 다시 과금하지 않고, 누적 비용도 복원돼야 한다. → Task 4 `test_reload_restores_cache_and_spend`, Task 6 `test_session_rerun_uses_cache`
4. **권한이 열린 API 키 파일** — 0644 키 파일은 읽지 않고 거부해야 한다. → Task 4 `test_load_api_key_rejects_open_permissions`
5. **세션 간격 위반** — 2시간이 안 돼 다음 세션을 실행하면 거부해야 한다(사전등록 조건). → Task 6 `test_session_gap_is_enforced`

---

### Task 1: 패키지 골격과 Binance 1분봉 로더

**Files:**
- Create: `lab/__init__.py`, `lab/jev_gate/__init__.py`, `lab/jev_gate/data.py`
- Create: `tests/lab/bars.py`, `tests/lab/test_lab_data.py`
- Modify: `.gitignore` (끝에 `lab/data/` 추가)

**Interfaces:**
- Produces: `data.MINUTE_US: int`, `data.KLINE_COLUMNS: list[str]`, `data.ChecksumError`, `data.to_micros(values) -> np.ndarray`, `data.month_range(start: str, end: str) -> list[str]`, `data.kline_url(symbol: str, period: str) -> str`, `data.parse_checksum(text: str) -> str`, `data.download_verified(url: str, dest_dir: Path, client: httpx.Client) -> Path`, `data.download_month(symbol: str, month: str, dest_dir: Path, client: httpx.Client) -> list[Path]`, `data.load_klines(zip_path: Path) -> pd.DataFrame` (열: `open_time, open, high, low, close, volume, taker_buy_base, t_close`, 시간은 마이크로초 int64), `data.load_klines_range(symbol: str, months: list[str], raw_dir: Path, client: httpx.Client) -> pd.DataFrame`
- Produces (tests): `tests.lab.bars.make_minute_bars(n: int = 600, seed: int = 7, start: float = 100_000.0) -> pd.DataFrame` (위와 같은 열)

- [ ] **Step 1: 패키지 파일과 테스트 보조 모듈 작성**

`lab/__init__.py`:
```python
"""포트폴리오 실험 패키지(앱과 분리)."""
```

`lab/jev_gate/__init__.py`:
```python
"""Lumina JEV Gate Lab — 1분봉 재생 기반 JEV 진입 게이트의 지연·비용·효과 검증.

설계: docs/superpowers/specs/2026-10-01-jev-gate-lab-design.md
"""
```

`tests/lab/bars.py`:
```python
"""lab 테스트용 합성 1분봉(랜덤워크) — 외부 시세 없이 계산 로직만 검증한다."""
from __future__ import annotations

import numpy as np
import pandas as pd

MINUTE_US = 60_000_000
T0_US = 1_759_276_800_000_000  # 2025-10-01 00:00 UTC


def make_minute_bars(n: int = 600, seed: int = 7, start: float = 100_000.0) -> pd.DataFrame:
    """data.load_klines와 같은 열을 가진 합성 1분봉."""
    rng = np.random.default_rng(seed)
    close = start * np.exp(np.cumsum(rng.normal(0, 0.0008, n)))
    open_ = np.concatenate([[start], close[:-1]])
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.0003, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.0003, n)))
    volume = rng.uniform(5, 50, n)
    open_time = T0_US + np.arange(n, dtype=np.int64) * MINUTE_US
    return pd.DataFrame({"open_time": open_time, "open": open_, "high": high, "low": low, "close": close,
                         "volume": volume, "taker_buy_base": volume * rng.uniform(0.3, 0.7, n),
                         "t_close": open_time + MINUTE_US})
```

`.gitignore` 끝에 추가:
```
# JEV Gate Lab 원본 데이터·캐시
lab/data/
```

- [ ] **Step 2: 실패하는 테스트 작성** — `tests/lab/test_lab_data.py`

```python
"""Binance 1분봉 로더 검증 — 네트워크 없이 httpx.MockTransport로 확인한다."""
import hashlib
import io
import json
import zipfile

import httpx
import pytest

from lab.jev_gate import data

ROWS = ("1759276800000,100,101,99,100.5,10,1759276859999,1000,5,4,400,0\n"
        "1759276860000,100.5,102,100,101,12,1759276919999,1200,6,7,700,0\n")


def _zip_bytes(csv_text: str, name: str = "BTCUSDT-1m-2025-10.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, csv_text)
    return buf.getvalue()


def _client(files: dict[str, bytes], calls: list[str]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        body = files.get(request.url.path)
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def _checksum(body: bytes) -> bytes:
    return f"{hashlib.sha256(body).hexdigest()}  x.zip".encode()


def test_to_micros_unifies_ms_and_us():
    assert data.to_micros([1759276800000, 1759276800000000]).tolist() == [1759276800000000] * 2


def test_to_micros_rejects_unknown_scale():
    with pytest.raises(ValueError):
        data.to_micros([123456])


def test_month_range_inclusive():
    assert data.month_range("2025-11", "2026-02") == ["2025-11", "2025-12", "2026-01", "2026-02"]


def test_kline_url_monthly_and_daily():
    assert data.kline_url("BTCUSDT", "2025-10").endswith("/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2025-10.zip")
    assert data.kline_url("BTCUSDT", "2026-09-30").endswith("/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2026-09-30.zip")


def test_parse_checksum():
    digest = "a" * 64
    assert data.parse_checksum(f"{digest}  BTCUSDT-1m-2025-10.zip\n") == digest
    with pytest.raises(ValueError):
        data.parse_checksum("not-a-hash file.zip")


def test_download_verified_writes_ledger_and_reuses(tmp_path):
    body = _zip_bytes(ROWS)
    url = data.kline_url("BTCUSDT", "2025-10")
    path = httpx.URL(url).path
    calls: list[str] = []
    client = _client({path: body, path + ".CHECKSUM": _checksum(body)}, calls)
    first = data.download_verified(url, tmp_path, client)
    second = data.download_verified(url, tmp_path, client)
    assert first == second and first.read_bytes() == body
    assert calls.count(path) == 1
    ledger = [json.loads(line) for line in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    assert len(ledger) == 1 and ledger[0]["sha256"] == hashlib.sha256(body).hexdigest()


def test_download_verified_rejects_mismatch(tmp_path):
    url = data.kline_url("BTCUSDT", "2025-10")
    path = httpx.URL(url).path
    files = {path: b"tampered", path + ".CHECKSUM": ("0" * 64 + "  x.zip").encode()}
    with pytest.raises(data.ChecksumError):
        data.download_verified(url, tmp_path, _client(files, []))
    assert list(tmp_path.glob("*.zip*")) == []


def test_download_month_falls_back_to_daily(tmp_path):
    body = _zip_bytes(ROWS)
    files = {}
    for day in range(1, 29):
        p = httpx.URL(data.kline_url("BTCUSDT", f"2026-02-{day:02d}")).path
        files[p] = body
        files[p + ".CHECKSUM"] = _checksum(body)
    assert len(data.download_month("BTCUSDT", "2026-02", tmp_path, _client(files, []))) == 28


def test_download_month_missing_day_raises(tmp_path):
    with pytest.raises(httpx.HTTPStatusError):
        data.download_month("BTCUSDT", "2026-02", tmp_path, _client({}, []))


def test_load_klines_normalizes_units_and_skips_header(tmp_path):
    zpath = tmp_path / "BTCUSDT-1m-2025-10.zip"
    zpath.write_bytes(_zip_bytes(",".join(data.KLINE_COLUMNS) + "\n" + ROWS))
    k = data.load_klines(zpath)
    assert k["open_time"].tolist() == [1759276800000000, 1759276860000000]
    assert (k["t_close"] - k["open_time"]).eq(data.MINUTE_US).all()
    assert k["taker_buy_base"].tolist() == [4.0, 7.0]
```

- [ ] **Step 3: 실패 확인**

Run: 테스트 명령에 `tests/lab/test_lab_data.py`를 붙여 실행.
Expected: FAIL — `ImportError: cannot import name 'data' from 'lab.jev_gate'`

- [ ] **Step 4: 구현** — `lab/jev_gate/data.py`

```python
"""Binance 공개 아카이브(data.binance.vision) 1분봉 로더.

- 월별 zip을 받아 `.CHECKSUM`의 SHA-256과 대조한다. 월별 파일이 아직 공개 전(404)이면 일별 zip으로 대체한다.
- 다운로드 기록(URL·해시·크기·시각)을 raw 디렉터리의 ledger.jsonl에 남긴다.
- 2025-01-01 이후 현물 타임스탬프는 마이크로초, 이전은 밀리초다. 자릿수로 판별해 마이크로초 정수로 통일한다.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

BASE_URL = "https://data.binance.vision"
MINUTE_US = 60_000_000
KLINE_COLUMNS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
                 "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"]


class ChecksumError(RuntimeError):
    """받은 파일의 SHA-256이 공식 CHECKSUM과 다르다."""


def to_micros(values) -> np.ndarray:
    """밀리초(13자리)·마이크로초(16자리) epoch를 마이크로초 int64로 통일한다. 판별 불가 값이 있으면 ValueError."""
    arr = np.asarray(values, dtype=np.int64)
    is_ms = (arr >= 10**12) & (arr < 10**13)
    is_us = (arr >= 10**15) & (arr < 10**16)
    if not np.all(is_ms | is_us):
        raise ValueError("epoch 단위를 판별할 수 없는 값이 있습니다")
    return np.where(is_ms, arr * 1000, arr)


def month_range(start: str, end: str) -> list[str]:
    """'YYYY-MM' 두 값을 양 끝 포함한 월 목록."""
    return [str(p) for p in pd.period_range(start, end, freq="M")]


def kline_url(symbol: str, period: str) -> str:
    """period가 'YYYY-MM'이면 월별, 'YYYY-MM-DD'이면 일별 1분봉 zip 주소."""
    scope = "monthly" if len(period) == 7 else "daily"
    return f"{BASE_URL}/data/spot/{scope}/klines/{symbol}/1m/{symbol}-1m-{period}.zip"


def parse_checksum(text: str) -> str:
    """CHECKSUM 내용('<sha256>  <파일명>')에서 소문자 해시만 꺼낸다."""
    parts = text.split()
    digest = parts[0].lower() if parts else ""
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("CHECKSUM 형식이 아닙니다")
    return digest


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_verified(url: str, dest_dir: Path, client: httpx.Client) -> Path:
    """zip을 받아 CHECKSUM과 대조한다. 이미 받은 파일의 해시가 맞으면 다시 받지 않는다."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / url.rsplit("/", 1)[1]
    resp = client.get(url + ".CHECKSUM")
    resp.raise_for_status()
    expected = parse_checksum(resp.text)
    if path.exists() and _sha256(path) == expected:
        return path
    tmp = path.with_name(path.name + ".part")
    with client.stream("GET", url) as r:
        r.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    actual = _sha256(tmp)
    if actual != expected:
        tmp.unlink()
        raise ChecksumError(f"{path.name}: CHECKSUM {expected} ≠ 실제 {actual}")
    tmp.replace(path)
    record = {"url": url, "sha256": actual, "bytes": path.stat().st_size,
              "downloaded_at": datetime.now(timezone.utc).isoformat()}
    with (dest_dir / "ledger.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    return path


def download_month(symbol: str, month: str, dest_dir: Path, client: httpx.Client) -> list[Path]:
    """월별 zip을 받는다. 월별 파일이 공개 전(404)이면 그 달 일별 zip을 모두 받고, 하루라도 없으면 오류."""
    try:
        return [download_verified(kline_url(symbol, month), dest_dir, client)]
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:
            raise
    days = pd.period_range(f"{month}-01", periods=pd.Period(month).days_in_month, freq="D")
    return [download_verified(kline_url(symbol, str(d)), dest_dir, client) for d in days]


def load_klines(zip_path: Path) -> pd.DataFrame:
    """1분봉 zip을 읽어 시간 단위를 통일하고 봉 마감 시각 t_close를 붙인다(헤더 행이 있어도 된다)."""
    raw = pd.read_csv(zip_path, header=None, names=KLINE_COLUMNS)
    raw = raw[pd.to_numeric(raw["open_time"], errors="coerce").notna()]
    out = pd.DataFrame({"open_time": to_micros(pd.to_numeric(raw["open_time"]).astype("int64"))})
    for col in ("open", "high", "low", "close", "volume", "taker_buy_base"):
        out[col] = pd.to_numeric(raw[col]).astype(float).to_numpy()
    out["t_close"] = out["open_time"] + MINUTE_US
    return out


def load_klines_range(symbol: str, months: list[str], raw_dir: Path, client: httpx.Client) -> pd.DataFrame:
    """여러 달의 1분봉을 받아 시간순으로 이어 붙이고 중복 봉을 제거한다."""
    frames = [load_klines(p) for m in months for p in download_month(symbol, m, raw_dir, client)]
    df = pd.concat(frames, ignore_index=True)
    return df.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
```

- [ ] **Step 5: 통과 확인**

Run: 테스트 명령에 `tests/lab/test_lab_data.py`.
Expected: 10 passed

- [ ] **Step 6: 커밋**

```bash
git add -- lab/__init__.py lab/jev_gate/__init__.py lab/jev_gate/data.py tests/lab/bars.py tests/lab/test_lab_data.py .gitignore
git commit -m "feat(lab): Binance 1분봉 로더와 체크섬 검증" -- lab/__init__.py lab/jev_gate/__init__.py lab/jev_gate/data.py tests/lab/bars.py tests/lab/test_lab_data.py .gitignore
```

---

### Task 2: 익명 특징

**Files:**
- Create: `lab/jev_gate/features.py`
- Create: `tests/lab/test_lab_features.py`

**Interfaces:**
- Consumes: `app.services.ta_utils.atr(high, low, close, period) -> pd.Series`, `ta_utils.ema(close, span) -> pd.Series`, Task 1의 1분봉 열
- Produces: `features.STATE_FEATURES: tuple[str, ...]` (11개), `features.FEATURE_DEFINITIONS: dict[str, str]`, `features.compute_features(k: pd.DataFrame) -> pd.DataFrame` (입력 열 + `ret_1, ret_5, ret_15, ret_60, atr14, atr_regime, volume_z, taker_buy_ratio, range_pos_240, trend_slope_atr`), `features.build_state(row) -> dict` (`{"features": {...11개}, "feature_definitions": {...}}`)

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/lab/test_lab_features.py`

```python
"""익명 특징 검증 — 미래 데이터 미사용, 알려진 값, 익명성."""
import json

import numpy as np
import pandas as pd

from lab.jev_gate import features as ft
from tests.lab.bars import MINUTE_US, T0_US, make_minute_bars

COLS = ["ret_1", "ret_5", "ret_15", "ret_60", "atr14", "atr_regime", "volume_z",
        "taker_buy_ratio", "range_pos_240", "trend_slope_atr"]


def _flat_bars(n: int = 300) -> pd.DataFrame:
    t = T0_US + np.arange(n, dtype=np.int64) * MINUTE_US
    vol = np.where(np.arange(n) % 2 == 0, 10.0, 20.0)
    return pd.DataFrame({"open_time": t, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                         "volume": vol, "taker_buy_base": vol * 0.4, "t_close": t + MINUTE_US})


def test_features_are_causal():
    bars = make_minute_bars(600)
    full = ft.compute_features(bars)
    part = ft.compute_features(bars.iloc[:450])
    for col in COLS:
        pd.testing.assert_series_equal(full[col].iloc[:450], part[col], check_names=False)


def test_known_values_on_flat_market():
    last = ft.compute_features(_flat_bars()).iloc[-1]
    assert last["ret_1"] == 0 and last["ret_60"] == 0
    assert last["atr14"] == 2.0 and last["atr_regime"] == 1.0
    assert last["range_pos_240"] == 0.5
    assert last["taker_buy_ratio"] == 0.4
    assert abs(last["trend_slope_atr"]) < 1e-9
    assert np.isfinite(last["volume_z"])


def test_zero_volume_bar_gives_nan_not_inf():
    bars = _flat_bars()
    bars.loc[299, ["volume", "taker_buy_base"]] = 0.0
    last = ft.compute_features(bars).iloc[-1]
    assert np.isnan(last["taker_buy_ratio"])
    assert not np.isinf(last["volume_z"])


def test_build_state_is_anonymous_and_rounded():
    row = {name: 1.23456 for name in ft.STATE_FEATURES}
    row.update(close=123456.7, open_time=1759276800000000, t_close=1759276860000000)
    state = ft.build_state(row)
    assert set(state["features"]) == set(ft.STATE_FEATURES)
    assert set(state["feature_definitions"]) == set(ft.STATE_FEATURES)
    assert state["features"]["ret_1"] == 1.235
    text = json.dumps(state)
    assert "123456" not in text and "1759276" not in text
```

- [ ] **Step 2: 실패 확인**

Run: 테스트 명령에 `tests/lab/test_lab_features.py`.
Expected: FAIL — `ImportError: cannot import name 'features'`

- [ ] **Step 3: 구현** — `lab/jev_gate/features.py`

```python
"""진입 신호 시점의 익명 특징.

모든 값은 해당 봉 마감 시점까지의 데이터만 쓴다(shift·rolling·ewm만 사용).
JEV 입력(state)에는 종목명·날짜·시각·절대 가격을 넣지 않는다.
breakout_margin_atr·bars_since_prev_signal은 돌파 채널이 필요해서 rule.find_candidates가 붙인다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.services import ta_utils as ta

STATE_FEATURES = ("ret_1", "ret_5", "ret_15", "ret_60", "breakout_margin_atr", "atr_regime",
                  "volume_z", "taker_buy_ratio", "range_pos_240", "trend_slope_atr", "bars_since_prev_signal")

FEATURE_DEFINITIONS = {
    "ret_1": "log return over the last 1 bar, percent",
    "ret_5": "log return over the last 5 bars, percent",
    "ret_15": "log return over the last 15 bars, percent",
    "ret_60": "log return over the last 60 bars, percent",
    "breakout_margin_atr": "(close - prior channel high) / ATR(14)",
    "atr_regime": "ATR(14) / ATR(240); above 1 means volatility is higher than usual",
    "volume_z": "z-score of this bar's volume against the previous 60 bars",
    "taker_buy_ratio": "taker buy volume / total volume of this bar",
    "range_pos_240": "position of close within the last 240 bars' low-high range, 0 to 1",
    "trend_slope_atr": "(EMA(60) now - EMA(60) 15 bars ago) / ATR(14)",
    "bars_since_prev_signal": "bars since the previous breakout signal, capped at 240",
}


def compute_features(k: pd.DataFrame) -> pd.DataFrame:
    """1분봉에 지표 열을 더한 새 DataFrame. 0으로 나누는 경우는 NaN으로 둔다."""
    f = k.copy()
    close, high, low, vol = f["close"], f["high"], f["low"], f["volume"]
    logc = np.log(close)
    for n in (1, 5, 15, 60):
        f[f"ret_{n}"] = (logc - logc.shift(n)) * 100
    f["atr14"] = ta.atr(high, low, close, 14)
    f["atr_regime"] = f["atr14"] / ta.atr(high, low, close, 240)
    prev_vol = vol.shift(1).rolling(60)
    f["volume_z"] = (vol - prev_vol.mean()) / prev_vol.std().replace(0, np.nan)
    f["taker_buy_ratio"] = f["taker_buy_base"] / vol.replace(0, np.nan)
    lo, hi = low.rolling(240).min(), high.rolling(240).max()
    f["range_pos_240"] = (close - lo) / (hi - lo).replace(0, np.nan)
    ema60 = ta.ema(close, 60)
    f["trend_slope_atr"] = (ema60 - ema60.shift(15)) / f["atr14"]
    return f


def build_state(row) -> dict:
    """후보 한 행을 JEV state(익명 특징 + 정의)로 바꾼다. 값은 소수 셋째 자리로 반올림한다."""
    return {"features": {name: round(float(row[name]), 3) for name in STATE_FEATURES},
            "feature_definitions": FEATURE_DEFINITIONS}
```

- [ ] **Step 4: 통과 확인**

Run: 테스트 명령에 `tests/lab/test_lab_features.py`.
Expected: 4 passed

- [ ] **Step 5: 커밋**

```bash
git add -- lab/jev_gate/features.py tests/lab/test_lab_features.py
git commit -m "feat(lab): 진입 시점 익명 특징 계산" -- lab/jev_gate/features.py tests/lab/test_lab_features.py
```

---

### Task 3: 돌파 후보와 청산 규칙

**Files:**
- Create: `lab/jev_gate/rule.py`
- Create: `tests/lab/test_lab_rule.py`

**Interfaces:**
- Consumes: `features.compute_features`, `features.STATE_FEATURES`
- Produces: `rule.STOP_ATR = 2.0`, `rule.TAKE_ATR = 3.0`, `rule.MAX_BARS = 120`, `rule.PREV_SIGNAL_CAP = 240`, `rule.find_candidates(f: pd.DataFrame, n: int) -> pd.DataFrame` (특징 열 + `bar: int`(행 위치), `channel_high`, `breakout_margin_atr`, `bars_since_prev_signal`), `rule.find_exit(close: np.ndarray, first_bar: int, entry_price: float, atr: float) -> tuple[int, str]` (사유 `stop|take|time|end`), `rule.net_return(entry: float, exit_: float, fee_rate: float, slip_bps: float) -> float`, `rule.rule_only_close_approx(f: pd.DataFrame, cands: pd.DataFrame, fee_rate: float, slip_bps: float) -> pd.DataFrame` (열 `bar, exit_bar, reason, gross_ret, net_ret`)

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/lab/test_lab_rule.py`

```python
"""돌파 후보·청산 규칙 검증."""
import numpy as np
import pandas as pd
import pytest

from lab.jev_gate import features as ft
from lab.jev_gate import rule
from tests.lab.bars import MINUTE_US, T0_US


def _bars(closes) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    t = T0_US + np.arange(n, dtype=np.int64) * MINUTE_US
    vol = np.where(np.arange(n) % 2 == 0, 10.0, 20.0)
    return pd.DataFrame({"open_time": t, "open": closes, "high": closes + 0.5, "low": closes - 0.5,
                         "close": closes, "volume": vol, "taker_buy_base": vol * 0.5, "t_close": t + MINUTE_US})


def test_fresh_breakout_fires_once():
    c = rule.find_candidates(ft.compute_features(_bars([100.0] * 300 + [101.0, 102.0, 103.0])), n=30)
    assert c["bar"].tolist() == [300]
    assert c.loc[0, "breakout_margin_atr"] > 0


def test_bars_since_prev_signal_is_gap_then_capped():
    closes = [100.0] * 300 + [101.0] + [100.0] * 49 + [101.0] + [100.0] * 10
    c = rule.find_candidates(ft.compute_features(_bars(closes)), n=30)
    assert c["bar"].tolist() == [300, 350]
    assert c["bars_since_prev_signal"].tolist() == [240, 50]


@pytest.mark.parametrize("path,expected", [
    ([100.0, 99.0, 97.9], (2, "stop")),    # 진입 100, ATR 1 → 손절선 98
    ([100.0, 101.0, 103.1], (2, "take")),  # 익절선 103
])
def test_find_exit_stop_and_take(path, expected):
    assert rule.find_exit(np.array(path), 1, 100.0, 1.0) == expected


def test_find_exit_time_and_end():
    flat = np.full(200, 100.0)
    assert rule.find_exit(flat, 1, 100.0, 1.0) == (120, "time")
    assert rule.find_exit(flat[:50], 1, 100.0, 1.0) == (49, "end")


def test_net_return_applies_fees_and_slippage():
    assert rule.net_return(100.0, 100.0, 0.0, 0.0) == 0.0
    expected = (110 * 0.9999) / (100 * 1.0001) * 0.999 ** 2 - 1
    assert rule.net_return(100.0, 110.0, 0.001, 1.0) == pytest.approx(expected)


def test_rule_only_holds_one_position():
    closes = [100.0] * 300 + [101.0] + [100.0] * 4 + [101.0] + [100.0] * 124 + [101.0] + [100.0] * 200
    f = ft.compute_features(_bars(closes))
    c = rule.find_candidates(f, n=3)
    trades = rule.rule_only_close_approx(f, c, fee_rate=0.001, slip_bps=1.0)
    assert c["bar"].tolist() == [300, 305, 430]
    assert trades["bar"].tolist() == [300, 430]          # 305는 보유 중이라 건너뜀
    assert trades["reason"].tolist() == ["time", "time"]
    assert (trades["net_ret"] < trades["gross_ret"]).all()
```

- [ ] **Step 2: 실패 확인**

Run: 테스트 명령에 `tests/lab/test_lab_rule.py`.
Expected: FAIL — `ImportError: cannot import name 'rule'`

- [ ] **Step 3: 구현** — `lab/jev_gate/rule.py`

```python
"""Donchian 돌파 진입 후보와 코드 청산 규칙.

진입 후보: close[t] > max(high[t-N..t-1])이고 직전 봉에서는 같은 조건이 거짓인 봉(새 돌파).
청산: 봉 마감 종가로 손절(진입가 − 2·ATR)·익절(진입가 + 3·ATR)·시간청산(120봉)을 판정한다.
ATR은 신호 봉의 ATR14로 고정한다. 손절·익절은 봉 안의 가격 경로를 보지 않는다(모든 갈래 공통 단순화).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from lab.jev_gate.features import STATE_FEATURES

STOP_ATR = 2.0
TAKE_ATR = 3.0
MAX_BARS = 120
PREV_SIGNAL_CAP = 240


def find_candidates(f: pd.DataFrame, n: int) -> pd.DataFrame:
    """특징 DataFrame에서 새 돌파 봉만 골라 bar·channel_high·breakout_margin_atr·bars_since_prev_signal을 붙인다.

    bar는 f의 행 위치다. 특징이 하나라도 비어 있는 워밍업 구간 후보는 뺀다.
    """
    f = f.reset_index(drop=True)
    channel = f["high"].shift(1).rolling(n).max()
    above = (f["close"] > channel).to_numpy()
    bars = np.flatnonzero(above & ~np.concatenate([[False], above[:-1]]))
    c = f.iloc[bars].copy()
    c["bar"] = bars
    c["channel_high"] = channel.iloc[bars].to_numpy()
    c["breakout_margin_atr"] = (c["close"] - c["channel_high"]) / c["atr14"]
    gaps = np.diff(bars, prepend=bars[0] - PREV_SIGNAL_CAP) if len(bars) else bars
    c["bars_since_prev_signal"] = np.minimum(gaps, PREV_SIGNAL_CAP)
    return c.dropna(subset=list(STATE_FEATURES)).reset_index(drop=True)


def find_exit(close: np.ndarray, first_bar: int, entry_price: float, atr: float) -> tuple[int, str]:
    """first_bar부터 종가로 청산 봉과 사유(stop·take·time·end)를 찾는다. end는 데이터가 먼저 끝난 경우."""
    stop = entry_price - STOP_ATR * atr
    take = entry_price + TAKE_ATR * atr
    for i in range(first_bar, min(first_bar + MAX_BARS, len(close))):
        if close[i] <= stop:
            return i, "stop"
        if close[i] >= take:
            return i, "take"
    if first_bar + MAX_BARS <= len(close):
        return first_bar + MAX_BARS - 1, "time"
    return len(close) - 1, "end"


def net_return(entry: float, exit_: float, fee_rate: float, slip_bps: float) -> float:
    """슬리피지(매수 +, 매도 −)와 양방향 수수료를 뺀 거래 수익률."""
    slip = slip_bps / 10_000
    return (exit_ * (1 - slip)) / (entry * (1 + slip)) * (1 - fee_rate) ** 2 - 1


def rule_only_close_approx(f: pd.DataFrame, cands: pd.DataFrame, fee_rate: float, slip_bps: float) -> pd.DataFrame:
    """Stage 0용 근사 백테스트: 신호 봉 종가 진입, 청산 봉 종가 청산, 포지션 1개.

    f의 마지막 행 이후는 보지 않으므로, 구간 끝에서 자른 f를 넘기면 다음 구간 가격을 쓰지 않는다.
    """
    close = f["close"].to_numpy(dtype=float)
    rows, busy_until = [], -1
    for c in cands.itertuples(index=False):
        if c.bar <= busy_until or c.bar + 1 >= len(close):
            continue
        exit_bar, reason = find_exit(close, c.bar + 1, c.close, c.atr14)
        rows.append({"bar": c.bar, "exit_bar": exit_bar, "reason": reason,
                     "gross_ret": close[exit_bar] / c.close - 1,
                     "net_ret": net_return(c.close, close[exit_bar], fee_rate, slip_bps)})
        busy_until = exit_bar
    return pd.DataFrame(rows, columns=["bar", "exit_bar", "reason", "gross_ret", "net_ret"])
```

- [ ] **Step 4: 통과 확인**

Run: 테스트 명령에 `tests/lab/test_lab_rule.py`.
Expected: 7 passed

- [ ] **Step 5: 커밋**

```bash
git add -- lab/jev_gate/rule.py tests/lab/test_lab_rule.py
git commit -m "feat(lab): Donchian 돌파 후보와 코드 청산 규칙" -- lab/jev_gate/rule.py tests/lab/test_lab_rule.py
```

---

### Task 4: 판정 모델 게이트 클라이언트

**Files:**
- Create: `lab/jev_gate/gate.py`
- Create: `tests/lab/test_lab_gate.py`

**Interfaces:**
- Produces: `gate.API_URL`, `gate.MODEL = "jev-1.13.0"`, `gate.PRICE_PER_INPUT_TOKEN`, `gate.TIMEOUT_S = 10.0`, `gate.QUESTION_ID = "fail"`, `gate.QUESTION: dict`, `gate.question_hash() -> str`, `gate.cache_key(state: dict) -> str`, `gate.load_api_key() -> str`, `gate.BudgetExceeded`, `gate.GateResult` (dataclass: `key, ok, p_fail, latency_ms, model, input_tokens, status, error, called_at, cached`), `gate.is_blocked(result: GateResult, tau: float) -> bool`, `gate.JevGate(cache_path: Path, budget_usd: float = 5.0, client: httpx.Client | None = None, api_key: str | None = None)` with `.ask(state: dict, use_cache: bool = True, tag: str | None = None) -> GateResult` and `.spent_usd: float`
- 캐시 JSONL 한 줄 = `asdict(GateResult)` + `"state"` + `"tag"`

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/lab/test_lab_gate.py`

```python
"""JEV 게이트 클라이언트 검증 — 실제 API를 호출하지 않는다."""
import json

import httpx
import pytest

from lab.jev_gate import gate

STATE = {"features": {"ret_1": 0.1}, "feature_definitions": {"ret_1": "x"}}


class Recorder:
    """MockTransport 핸들러: 요청을 기록하고 make()가 돌려준 응답을 반환한다(예외면 그대로 발생)."""

    def __init__(self, make):
        self.make = make
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.make()


def ok_response(p: float = 0.7, tokens: int = 500) -> httpx.Response:
    return httpx.Response(200, json={"model": "jev-1.13.0", "answers": {"fail": {"type": "noul", "noul": p}},
                                     "usage": {"input_tokens": tokens, "output_tokens": 20}})


def _gate(path, rec, **kw) -> gate.JevGate:
    return gate.JevGate(path, client=httpx.Client(transport=httpx.MockTransport(rec)), api_key="test-key", **kw)


def test_success_parses_and_logs_without_key(tmp_path):
    rec = Recorder(ok_response)
    r = _gate(tmp_path / "calls.jsonl", rec).ask(STATE, tag="session-1")
    assert (r.ok, r.p_fail, r.input_tokens, r.model, r.cached) == (True, 0.7, 500, "jev-1.13.0", False)
    body = json.loads(rec.requests[0].content)
    assert body["model"] == "jev-1.13.0" and body["questions"]["fail"]["type"] == "noul"
    assert body["state"] == STATE
    assert rec.requests[0].headers["Authorization"] == "Bearer test-key"
    text = (tmp_path / "calls.jsonl").read_text()
    assert "test-key" not in text
    line = json.loads(text)
    assert line["state"] == STATE and line["tag"] == "session-1"


def test_cache_hit_and_forced_repeat(tmp_path):
    rec = Recorder(ok_response)
    g = _gate(tmp_path / "calls.jsonl", rec)
    g.ask(STATE)
    assert g.ask(STATE).cached is True and len(rec.requests) == 1
    assert g.ask(STATE, use_cache=False).cached is False and len(rec.requests) == 2


def test_cache_key_ignores_dict_order():
    assert gate.cache_key({"a": 1, "b": 2}) == gate.cache_key({"b": 2, "a": 1})


def _raise(exc):
    def make():
        raise exc
    return make


@pytest.mark.parametrize("make,error", [
    (lambda: httpx.Response(500), "http_500"),
    (lambda: ok_response(p=1.5), "schema"),
    (lambda: httpx.Response(200, json={"model": "jev-1.13.0", "answers": {}}), "schema"),
    (_raise(httpx.ReadTimeout("slow")), "timeout"),
    (_raise(httpx.ConnectError("down")), "ConnectError"),
])
def test_failures_are_blocked_and_not_cached(tmp_path, make, error):
    rec = Recorder(make)
    g = _gate(tmp_path / "calls.jsonl", rec)
    r = g.ask(STATE)
    assert r.ok is False and r.error == error and gate.is_blocked(r, 0.99)
    g.ask(STATE)
    assert len(rec.requests) == 2


def test_budget_stops_calls(tmp_path):
    rec = Recorder(ok_response)
    g = _gate(tmp_path / "calls.jsonl", rec, budget_usd=1e-9)
    g.ask(STATE)
    with pytest.raises(gate.BudgetExceeded):
        g.ask({"features": {"ret_1": 0.2}})
    assert len(rec.requests) == 1


def test_reload_restores_cache_and_spend(tmp_path):
    path = tmp_path / "calls.jsonl"
    _gate(path, Recorder(ok_response)).ask(STATE)
    rec2 = Recorder(ok_response)
    g2 = _gate(path, rec2)
    assert g2.ask(STATE).cached is True and rec2.requests == []
    assert g2.spent_usd == pytest.approx(500 * gate.PRICE_PER_INPUT_TOKEN)


def test_is_blocked_threshold():
    base = dict(key="k", ok=True, latency_ms=1.0, model="jev-1.13.0", input_tokens=1, status=200, error=None,
                called_at="t")
    assert gate.is_blocked(gate.GateResult(p_fail=0.5, **base), 0.5) is True
    assert gate.is_blocked(gate.GateResult(p_fail=0.49, **base), 0.5) is False


def test_load_api_key_rejects_open_permissions(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    key = tmp_path / ".config/typesafe/api_key"
    key.parent.mkdir(parents=True)
    key.write_text("secret-value\n")
    key.chmod(0o644)
    with pytest.raises(PermissionError):
        gate.load_api_key()
    key.chmod(0o600)
    assert gate.load_api_key() == "secret-value"
```

- [ ] **Step 2: 실패 확인**

Run: 테스트 명령에 `tests/lab/test_lab_gate.py`.
Expected: FAIL — `ImportError: cannot import name 'gate'`

- [ ] **Step 3: 구현** — `lab/jev_gate/gate.py`

```python
"""TypeSafe JEV 진입 게이트 클라이언트.

- 모델은 jev-1.13.0으로 고정하고, 응답의 실제 모델 ID를 매 호출 기록한다.
- 한 번에 하나씩 순차 호출한다(동시 호출로 지연이 부풀지 않게). 타임아웃 10초, 재시도 없음.
- 모든 호출을 JSONL에 남긴다(익명 state·결과·태그). 같은 입력은 캐시를 쓰고, 반복 측정은 use_cache=False.
- 실패(HTTP 오류·타임아웃·스키마 위반)는 차단으로 처리하고 캐시하지 않는다.
- 누적 입력 토큰 비용이 예산에 닿으면 호출하지 않는다. API 키 값은 어디에도 기록하지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import httpx

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.13.0"
PRICE_PER_INPUT_TOKEN = 0.0  # 단가는 문서에 적지 않는다(사전등록 값 참조)
TIMEOUT_S = 10.0
QUESTION_ID = "fail"
QUESTION = {
    "type": "noul",
    "instructions": (
        "A long breakout entry signal just fired on a 1-minute chart of a liquid market. "
        "Using only the anonymized features in the state, will this trade more likely fail than succeed?"
    ),
    "criteria": {
        "true": (
            "Price is more likely to close 2 ATR below entry, or reach the 120-minute limit without "
            "closing 3 ATR above entry, before it closes 3 ATR above entry."
        ),
        "false": "Price is more likely to close 3 ATR above entry before closing 2 ATR below entry, within 120 minutes.",
    },
}


class BudgetExceeded(RuntimeError):
    """누적 JEV 비용이 예산 상한에 닿았다."""


@dataclass
class GateResult:
    """JEV 호출 한 번의 결과. ok=False면 게이트는 차단한다."""

    key: str
    ok: bool
    p_fail: float | None
    latency_ms: float
    model: str | None
    input_tokens: int
    status: int | None
    error: str | None
    called_at: str
    cached: bool = False


def question_hash() -> str:
    """질문 정의의 SHA-256(사전등록 대조용)."""
    return hashlib.sha256(json.dumps(QUESTION, sort_keys=True).encode()).hexdigest()


def cache_key(state: dict) -> str:
    """state·질문·모델로 만든 캐시 키. dict 키 순서와 무관하다."""
    payload = json.dumps({"state": state, "question": QUESTION, "model": MODEL}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def load_api_key() -> str:
    """TYPESAFE_API_KEY 환경 변수, 없으면 소유자 전용(0600) ~/.config/typesafe/api_key에서 읽는다."""
    key = os.environ.get("TYPESAFE_API_KEY")
    if key:
        return key.strip()
    path = Path.home() / ".config/typesafe/api_key"
    st = path.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
        raise PermissionError(f"{path}는 소유자 전용(0600) 일반 파일이어야 합니다")
    return path.read_text().strip()


def is_blocked(result: GateResult, tau: float) -> bool:
    """실패는 차단, 성공은 p_fail ≥ τ이면 차단."""
    return (not result.ok) or result.p_fail >= tau


class JevGate:
    """캐시·예산 상한이 있는 순차 JEV 호출기."""

    def __init__(self, cache_path: Path, budget_usd: float = 5.0, client: httpx.Client | None = None,
                 api_key: str | None = None):
        self.cache_path = cache_path
        self.budget_usd = budget_usd
        self._client = client if client is not None else httpx.Client(timeout=TIMEOUT_S)
        self._api_key = api_key
        self._cache: dict[str, GateResult] = {}
        self.spent_usd = 0.0
        if cache_path.exists():
            for line in cache_path.read_text(encoding="utf-8").splitlines():
                rec = json.loads(line)
                rec.pop("state", None)
                rec.pop("tag", None)
                r = GateResult(**rec)
                self.spent_usd += r.input_tokens * PRICE_PER_INPUT_TOKEN
                if r.ok and r.key not in self._cache:
                    self._cache[r.key] = r

    def ask(self, state: dict, use_cache: bool = True, tag: str | None = None) -> GateResult:
        """state에 대한 p_fail을 묻는다. 캐시가 있으면 호출하지 않는다."""
        key = cache_key(state)
        if use_cache and key in self._cache:
            return replace(self._cache[key], cached=True)
        if self.spent_usd >= self.budget_usd:
            raise BudgetExceeded(f"누적 ${self.spent_usd:.6f} ≥ 예산 ${self.budget_usd}")
        result = self._call(key, state)
        self.spent_usd += result.input_tokens * PRICE_PER_INPUT_TOKEN
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with self.cache_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({**asdict(result), "state": state, "tag": tag}) + "\n")
        if result.ok and key not in self._cache:
            self._cache[key] = result
        return result

    def _call(self, key: str, state: dict) -> GateResult:
        if self._api_key is None:
            self._api_key = load_api_key()
        body = {"model": MODEL, "state": state, "questions": {QUESTION_ID: QUESTION}}
        called_at = datetime.now(timezone.utc).isoformat()
        t0 = time.perf_counter()

        def fail(error: str, status: int | None = None) -> GateResult:
            return GateResult(key, False, None, (time.perf_counter() - t0) * 1000, None, 0, status, error, called_at)

        try:
            resp = self._client.post(API_URL, json=body, headers={"Authorization": f"Bearer {self._api_key}"},
                                     timeout=TIMEOUT_S)
        except httpx.TimeoutException:
            return fail("timeout")
        except httpx.HTTPError as e:
            return fail(type(e).__name__)
        latency = (time.perf_counter() - t0) * 1000
        if resp.status_code != 200:
            return fail(f"http_{resp.status_code}", resp.status_code)
        try:
            payload = resp.json()
            p = float(payload["answers"][QUESTION_ID]["noul"])
            tokens = int(payload["usage"]["input_tokens"])
            model = str(payload["model"])
            if not 0.0 <= p <= 1.0:
                raise ValueError(p)
        except (ValueError, KeyError, TypeError):
            return fail("schema", 200)
        return GateResult(key, True, p, latency, model, tokens, 200, None, called_at)
```

- [ ] **Step 4: 통과 확인**

Run: 테스트 명령에 `tests/lab/test_lab_gate.py`.
Expected: 12 passed

- [ ] **Step 5: 커밋**

```bash
git add -- lab/jev_gate/gate.py tests/lab/test_lab_gate.py
git commit -m "feat(lab): 캐시·예산 상한이 있는 JEV 게이트 클라이언트" -- lab/jev_gate/gate.py tests/lab/test_lab_gate.py
```

---

### Task 5: 사전등록과 Stage 0 집계

**Files:**
- Create: `lab/jev_gate/prereg.json`, `lab/jev_gate/stage0.py`
- Create: `tests/lab/test_lab_prereg.py`, `tests/lab/test_lab_stage0.py`

**Interfaces:**
- Consumes: `gate.MODEL`, `gate.question_hash()`, `gate.PRICE_PER_INPUT_TOKEN`, `rule.STOP_ATR/TAKE_ATR/MAX_BARS`
- Produces: `stage0.period_mask(t_close: pd.Series, start: str, end: str) -> pd.Series`, `stage0.rule_stats(n_candidates: int, trades: pd.DataFrame) -> dict`, `stage0.choose_n(stats: dict[int, dict], min_candidates: int) -> int`, `stage0.sample_indices(n_total: int, size: int, seed: int) -> list[int]`, `stage0.call_summary(records: list[dict]) -> dict` (`calls, p50_ms, p95_ms, p99_ms, failure_rate, schema_valid_rate, mean_input_tokens, models, errors`), `stage0.repeat_agreement(groups: dict[str, list[float]]) -> dict` (`items, agreement, mean_std`), `stage0.project_full_run(total_candidates: int, mean_input_tokens: float, p50_ms: float) -> dict` (`calls, cost_usd, hours_sequential`), `stage0.evaluate_go(summary: dict, th: dict) -> dict[str, bool]`, `stage0.render_report(summary: dict, go: dict[str, bool], th: dict) -> str`

- [ ] **Step 1: 사전등록 파일 작성** — `lab/jev_gate/prereg.json`

```json
{
  "version": 1,
  "created": "2026-10-01",
  "spec": "docs/superpowers/specs/2026-10-01-jev-gate-lab-design.md",
  "symbol": "BTCUSDT",
  "periods": {
    "dev": ["2025-10-01", "2026-04-30"],
    "validation": ["2026-05-01", "2026-06-30"],
    "holdout": ["2026-07-01", "2026-09-10"],
    "post_release": ["2026-09-11", "2026-09-30"]
  },
  "rule": {
    "n_grid": [30, 60, 120],
    "n_selection": "개발 구간 후보가 dev_candidates_min 이상인 N 중 1분봉 종가 근사 비용 차감 누적 수익률이 가장 높은 N(동률이면 작은 N)",
    "stop_atr": 2.0,
    "take_atr": 3.0,
    "max_bars": 120
  },
  "costs": {"taker_fee_rate": 0.001, "slippage_bps": 1.0, "notional_usdt": 1000},
  "stale_latency_ms": 5000,
  "jev": {
    "model": "jev-1.13.0",
    "question_sha256": "df13cbf68dce6784b48544c5ba01efa2b8484f4de450428ee3e52be2f5d159f1",
    "timeout_s": 10,
    "budget_usd": 5.0
  },
  "tau_grid": [0.3, 0.4, 0.5, 0.6, 0.7],
  "tau_max_block_rate": 0.6,
  "regime": {"lookback_days": 30, "up_threshold": 0.05, "down_threshold": -0.05},
  "stage0": {
    "sample_size": 300,
    "sample_seed": 20261001,
    "sessions": 3,
    "session_min_gap_hours": 2,
    "repeat_items": 50,
    "repeat_calls": 5,
    "thresholds": {
      "latency_p99_ms": 5000,
      "failure_rate_max": 0.02,
      "schema_valid_min": 0.99,
      "repeat_agreement_min": 0.9,
      "dev_candidates_min": 300,
      "projected_cost_max_usd": 5.0
    }
  }
}
```

- [ ] **Step 2: 실패하는 테스트 작성**

`tests/lab/test_lab_prereg.py`:
```python
"""사전등록 파일이 코드와 일치하는지 검증한다(질문·모델·규칙을 바꾸면 여기서 깨진다)."""
import json
from pathlib import Path

from lab.jev_gate import gate, rule

PREREG = json.loads((Path(__file__).parents[2] / "lab/jev_gate/prereg.json").read_text(encoding="utf-8"))


def test_question_and_model_match_code():
    assert PREREG["jev"]["model"] == gate.MODEL
    assert PREREG["jev"]["question_sha256"] == gate.question_hash()
    assert PREREG["jev"]["timeout_s"] == gate.TIMEOUT_S


def test_rule_constants_match_code():
    r = PREREG["rule"]
    assert (r["stop_atr"], r["take_atr"], r["max_bars"]) == (rule.STOP_ATR, rule.TAKE_ATR, rule.MAX_BARS)


def test_stage0_thresholds_present():
    assert set(PREREG["stage0"]["thresholds"]) == {
        "latency_p99_ms", "failure_rate_max", "schema_valid_min", "repeat_agreement_min",
        "dev_candidates_min", "projected_cost_max_usd"}
```

`tests/lab/test_lab_stage0.py`:
```python
"""Stage 0 집계 함수 검증."""
import pandas as pd
import pytest

from lab.jev_gate import gate, stage0
from tests.lab.bars import MINUTE_US, T0_US

TH = {"latency_p99_ms": 5000, "failure_rate_max": 0.02, "schema_valid_min": 0.99,
      "repeat_agreement_min": 0.9, "dev_candidates_min": 300, "projected_cost_max_usd": 5.0}


def test_period_mask_boundaries():
    t_close = pd.Series([T0_US, T0_US + MINUTE_US, T0_US + 86_400_000_000, T0_US + 86_400_000_000 + MINUTE_US])
    assert stage0.period_mask(t_close, "2025-10-01", "2025-10-01").tolist() == [False, True, True, False]


def test_rule_stats():
    trades = pd.DataFrame({"bar": [1, 5], "exit_bar": [3, 7], "reason": ["take", "stop"],
                           "gross_ret": [0.02, -0.01], "net_ret": [0.0178, -0.0122]})
    s = stage0.rule_stats(10, trades)
    assert (s["candidates"], s["trades"], s["win_rate"]) == (10, 2, 0.5)
    assert s["net_compound"] == pytest.approx(1.0178 * 0.9878 - 1)
    assert s["reasons"] == {"take": 1, "stop": 1}


def test_rule_stats_empty():
    empty = pd.DataFrame(columns=["bar", "exit_bar", "reason", "gross_ret", "net_ret"])
    assert stage0.rule_stats(0, empty)["trades"] == 0


def test_choose_n_eligibility_and_tie():
    stats = {30: {"candidates": 900, "net_compound": 0.1}, 60: {"candidates": 500, "net_compound": 0.1},
             120: {"candidates": 200, "net_compound": 0.9}}
    assert stage0.choose_n(stats, 300) == 30
    with pytest.raises(ValueError):
        stage0.choose_n(stats, 1000)


def test_sample_indices_deterministic_sorted_capped():
    a = stage0.sample_indices(1000, 300, 7)
    assert a == stage0.sample_indices(1000, 300, 7) and a == sorted(a) and len(set(a)) == 300
    assert stage0.sample_indices(5, 300, 7) == [0, 1, 2, 3, 4]


def _rec(lat, ok=True, status=200, error=None, tokens=600, model="jev-1.13.0"):
    return {"latency_ms": lat, "ok": ok, "status": status, "error": error,
            "input_tokens": tokens if ok else 0, "model": model if ok else None}


def test_call_summary():
    recs = [_rec(100), _rec(200), _rec(300), _rec(10_000, ok=False, status=None, error="timeout")]
    s = stage0.call_summary(recs)
    assert s["calls"] == 4 and s["failure_rate"] == 0.25 and s["schema_valid_rate"] == 1.0
    assert s["p50_ms"] == 250 and s["mean_input_tokens"] == 600
    assert s["models"] == ["jev-1.13.0"] and s["errors"] == {"timeout": 1}


def test_repeat_agreement():
    out = stage0.repeat_agreement({"a": [0.6, 0.7, 0.4, 0.8, 0.9], "b": [0.1] * 5})
    assert out["items"] == 2 and out["agreement"] == pytest.approx(0.9)


def test_project_full_run():
    p = stage0.project_full_run(10_000, 700, 360)
    assert p["cost_usd"] == pytest.approx(10_000 * 700 * gate.PRICE_PER_INPUT_TOKEN)
    assert p["hours_sequential"] == pytest.approx(1.0)


def _summary(**over):
    s = {"calls": {"p99_ms": 900, "failure_rate": 0.0, "schema_valid_rate": 1.0},
         "repeat": {"agreement": 0.95}, "dev_candidates": 400, "projection": {"cost_usd": 0.5}}
    s.update(over)
    return s


def test_evaluate_go_boundaries():
    assert all(stage0.evaluate_go(_summary(), TH).values())
    go = stage0.evaluate_go(_summary(dev_candidates=299, repeat={"agreement": 0.89}), TH)
    assert go["dev_candidates"] is False and go["repeat_agreement"] is False and go["latency_p99"] is True


def test_render_report_verdicts():
    base = {"n_selected": 60, "rule": {"60": {"candidates": 400, "trades": 300, "net_compound": -0.1,
                                              "net_mean": -0.001, "gross_mean": 0.0005, "win_rate": 0.4,
                                              "reasons": {"stop": 1}}},
            "candidate_counts": {"dev": 400}, "sessions": [], "by_session": {},
            "calls": {"calls": 500, "p50_ms": 300, "p95_ms": 600, "p99_ms": 900, "failure_rate": 0.0,
                      "schema_valid_rate": 1.0, "mean_input_tokens": 700, "models": ["jev-1.13.0"], "errors": {}},
            "repeat": {"items": 50, "agreement": 0.95, "mean_std": 0.01}, "dev_candidates": 400,
            "projection": {"calls": 600, "cost_usd": 0.02, "hours_sequential": 0.05}, "complete": True}
    go = stage0.evaluate_go(base, TH)
    assert "판정: GO" in stage0.render_report(base, go, TH)
    assert "판정: PARTIAL" in stage0.render_report({**base, "complete": False}, go, TH)
    assert "판정: NO-GO" in stage0.render_report(base, {**go, "dev_candidates": False}, TH)
```

- [ ] **Step 3: 실패 확인**

Run: 테스트 명령에 `tests/lab/test_lab_prereg.py tests/lab/test_lab_stage0.py`.
Expected: prereg 3 passed(이미 일치), stage0 FAIL — `ImportError: cannot import name 'stage0'`

- [ ] **Step 4: 구현** — `lab/jev_gate/stage0.py`

```python
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
    return "\n".join(lines)
```

- [ ] **Step 5: 통과 확인**

Run: 테스트 명령에 `tests/lab/test_lab_prereg.py tests/lab/test_lab_stage0.py`.
Expected: 13 passed

- [ ] **Step 6: 커밋**

```bash
git add -- lab/jev_gate/prereg.json lab/jev_gate/stage0.py tests/lab/test_lab_prereg.py tests/lab/test_lab_stage0.py
git commit -m "feat(lab): Stage 0 사전등록과 집계·판정" -- lab/jev_gate/prereg.json lab/jev_gate/stage0.py tests/lab/test_lab_prereg.py tests/lab/test_lab_stage0.py
```

---

### Task 6: CLI

**Files:**
- Create: `lab/jev_gate/__main__.py`
- Create: `tests/lab/test_lab_cli.py`

**Interfaces:**
- Consumes: Task 1~5 전체
- Produces: `python -m lab.jev_gate [--root PATH] stage0-rule | stage0-call --session K | stage0-repeat | stage0-report`; 함수 `cli.Paths(root)`, `cli.cmd_stage0_call(paths, pre, session, gate_factory=None, now=None)`, `cli.cmd_stage0_repeat(paths, pre, gate_factory=None)`, `cli.cmd_stage0_report(paths, pre, now=None) -> dict`, `cli.main(argv=None) -> int`
- 산출 파일: `lab/results/stage0/{rule_stats.json, sample.json, sessions.json, jev_calls.jsonl, summary.json}`, `docs/lab/stage0-report.md`, `lab/attempts.jsonl`

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/lab/test_lab_cli.py`

```python
"""CLI 흐름 검증 — 가짜 JEV로 세션 3회·반복·리포트까지 돌린다."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import lab.jev_gate.__main__ as cli
from lab.jev_gate import gate

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def _setup(tmp_path):
    paths = cli.Paths(tmp_path)
    paths.results.mkdir(parents=True)
    paths.rule.write_text(json.dumps({
        "n_selected": 60,
        "stats": {"60": {"candidates": 400, "trades": 300, "net_compound": -0.1, "net_mean": -0.001,
                         "gross_mean": 0.0005, "win_rate": 0.4, "reasons": {"stop": 300}}},
        "candidate_counts": {"dev": 400, "validation": 100, "holdout": 100, "post_release": 30}}))
    sample = [{"bar": i, "t_close": i, "state": {"features": {"ret_1": i / 1000}}} for i in range(300)]
    paths.sample.write_text(json.dumps(sample))
    return paths


class FakeFactory:
    """JevGate를 MockTransport로 만드는 팩토리. 호출 수를 센다."""

    def __init__(self):
        self.calls = 0

    def __call__(self, paths, pre):
        def handler(request):
            self.calls += 1
            return httpx.Response(200, json={"model": "jev-1.13.0", "answers": {"fail": {"type": "noul", "noul": 0.3}},
                                             "usage": {"input_tokens": 700, "output_tokens": 20}})
        return gate.JevGate(paths.calls, budget_usd=pre["jev"]["budget_usd"],
                            client=httpx.Client(transport=httpx.MockTransport(handler)), api_key="k")


def test_full_stage0_flow_reaches_go(tmp_path):
    paths, pre, fake = _setup(tmp_path), cli.load_prereg(), FakeFactory()
    for k in (1, 2, 3):
        cli.cmd_stage0_call(paths, pre, k, gate_factory=fake, now=T0 + timedelta(hours=3 * (k - 1)))
    cli.cmd_stage0_repeat(paths, pre, gate_factory=fake)
    assert fake.calls == 300 + 50 * 4
    summary = cli.cmd_stage0_report(paths, pre, now=T0 + timedelta(hours=7))
    assert summary["complete"] is True
    assert "판정: GO" in paths.report.read_text(encoding="utf-8")
    attempt = json.loads(paths.attempts.read_text().splitlines()[-1])
    assert attempt["verdict"] == "GO" and attempt["stage"] == "stage0"


def test_session_gap_is_enforced(tmp_path):
    paths, pre, fake = _setup(tmp_path), cli.load_prereg(), FakeFactory()
    cli.cmd_stage0_call(paths, pre, 1, gate_factory=fake, now=T0)
    with pytest.raises(SystemExit):
        cli.cmd_stage0_call(paths, pre, 2, gate_factory=fake, now=T0 + timedelta(hours=1))
    with pytest.raises(SystemExit):
        cli.cmd_stage0_call(paths, pre, 3, gate_factory=fake, now=T0 + timedelta(hours=5))


def test_session_rerun_uses_cache(tmp_path):
    paths, pre, fake = _setup(tmp_path), cli.load_prereg(), FakeFactory()
    cli.cmd_stage0_call(paths, pre, 1, gate_factory=fake, now=T0)
    cli.cmd_stage0_call(paths, pre, 1, gate_factory=fake, now=T0 + timedelta(minutes=5))
    assert fake.calls == 100
    assert len(json.loads(paths.sessions.read_text())) == 1


def test_report_before_completion_is_partial(tmp_path):
    paths, pre, fake = _setup(tmp_path), cli.load_prereg(), FakeFactory()
    cli.cmd_stage0_call(paths, pre, 1, gate_factory=fake, now=T0)
    assert cli.cmd_stage0_report(paths, pre, now=T0)["complete"] is False
    assert "판정: PARTIAL" in paths.report.read_text(encoding="utf-8")


def test_main_dispatches_report(tmp_path):
    paths, pre, fake = _setup(tmp_path), cli.load_prereg(), FakeFactory()
    cli.cmd_stage0_call(paths, pre, 1, gate_factory=fake, now=T0)
    assert cli.main(["--root", str(tmp_path), "stage0-report"]) == 0
```

- [ ] **Step 2: 실패 확인**

Run: 테스트 명령에 `tests/lab/test_lab_cli.py`.
Expected: FAIL — `ModuleNotFoundError: No module named 'lab.jev_gate.__main__'`

- [ ] **Step 3: 구현** — `lab/jev_gate/__main__.py`

```python
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


class Paths:
    """Stage 0 입출력 경로 모음."""

    def __init__(self, root: Path):
        self.raw = root / "lab/data/raw"
        self.results = root / "lab/results/stage0"
        self.rule = self.results / "rule_stats.json"
        self.sample = self.results / "sample.json"
        self.sessions = self.results / "sessions.json"
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
    """표본의 session번째 묶음을 순차 호출한다. 이전 세션 시작 후 최소 간격이 지나야 한다."""
    s0 = pre["stage0"]
    if not 1 <= session <= s0["sessions"]:
        raise SystemExit(f"session은 1~{s0['sessions']} 사이여야 합니다")
    now = now or datetime.now(timezone.utc)
    sessions = _read_json(paths.sessions, [])
    if session > 1:
        prev = next((s for s in sessions if s["session"] == session - 1), None)
        if prev is None:
            raise SystemExit(f"세션 {session - 1}을 먼저 실행하세요")
        gap = now - datetime.fromisoformat(prev["started_at"])
        if gap < timedelta(hours=s0["session_min_gap_hours"]):
            raise SystemExit(f"세션 간격이 {s0['session_min_gap_hours']}시간 미만입니다(경과 {gap})")
    if not any(s["session"] == session for s in sessions):
        sessions.append({"session": session, "started_at": now.isoformat()})
        _write_json(paths.sessions, sessions)
    sample = _read_json(paths.sample, None)
    size = len(sample) // s0["sessions"]
    end = None if session == s0["sessions"] else session * size
    batch = sample[(session - 1) * size: end]
    g = (gate_factory or _default_gate)(paths, pre)
    for i, item in enumerate(batch, 1):
        r = g.ask(item["state"], tag=f"session-{session}")
        print(f"[session {session}] {i}/{len(batch)} ok={r.ok} p_fail={r.p_fail} "
              f"{r.latency_ms:.0f}ms cached={r.cached}", flush=True)
    print(f"누적 비용 ${g.spent_usd:.6f}")
    return 0


def cmd_stage0_repeat(paths: Paths, pre: dict, gate_factory=None) -> int:
    """표본 앞 repeat_items건을 입력당 총 repeat_calls회가 되도록 더 호출한다(중단 후 재실행 시 이어서)."""
    s0 = pre["stage0"]
    if not any(s["session"] == 1 for s in _read_json(paths.sessions, [])):
        raise SystemExit("세션 1을 먼저 실행하세요")
    done = Counter(r["key"] for r in _read_jsonl(paths.calls) if r.get("tag") == "repeat")
    g = (gate_factory or _default_gate)(paths, pre)
    for item in _read_json(paths.sample, None)[: s0["repeat_items"]]:
        for _ in range(max(0, s0["repeat_calls"] - 1 - done[gate.cache_key(item["state"])])):
            r = g.ask(item["state"], use_cache=False, tag="repeat")
            print(f"[repeat] ok={r.ok} p_fail={r.p_fail} {r.latency_ms:.0f}ms", flush=True)
    print(f"누적 비용 ${g.spent_usd:.6f}")
    return 0


def cmd_stage0_report(paths: Paths, pre: dict, now: datetime | None = None) -> dict:
    """호출 기록을 요약해 통과 여부를 판정하고 리포트·summary·시도 원장을 쓴다."""
    s0, th = pre["stage0"], pre["stage0"]["thresholds"]
    rs, sample = _read_json(paths.rule, None), _read_json(paths.sample, None)
    records, sessions = _read_jsonl(paths.calls), _read_json(paths.sessions, [])
    if not records:
        raise SystemExit("호출 기록이 없습니다")
    repeat_keys = [gate.cache_key(it["state"]) for it in sample[: s0["repeat_items"]]]
    by_key = defaultdict(list)
    for r in records:
        if r["ok"]:
            by_key[r["key"]].append(r["p_fail"])
    groups = {k: by_key[k][: s0["repeat_calls"]] for k in repeat_keys if len(by_key[k]) >= s0["repeat_calls"]}
    called = {r["key"] for r in records if str(r.get("tag", "")).startswith("session-")}
    calls = stage0.call_summary(records)
    summary = {
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
        "n_selected": rs["n_selected"], "rule": rs["stats"], "candidate_counts": rs["candidate_counts"],
        "dev_candidates": rs["candidate_counts"].get("dev", 0),
        "calls": calls, "repeat": stage0.repeat_agreement(groups),
        "projection": stage0.project_full_run(sum(rs["candidate_counts"].values()), calls["mean_input_tokens"],
                                              calls["p50_ms"]),
        "sessions": sessions,
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
```

- [ ] **Step 4: 통과 확인**

Run: 테스트 명령에 `tests/lab/test_lab_cli.py`, 그다음 `tests/lab` 전체.
Expected: cli 5 passed, 전체 51 passed

- [ ] **Step 5: 커밋**

```bash
git add -- lab/jev_gate/__main__.py tests/lab/test_lab_cli.py
git commit -m "feat(lab): Stage 0 명령줄(규칙·세션 호출·반복·리포트)" -- lab/jev_gate/__main__.py tests/lab/test_lab_cli.py
```

---

### Task 7: Stage 0 실행 — 규칙 통계·세션 1·반복 측정

네트워크와 실제 판정 모델 호출을 쓴다. 예상 비용은 사전등록한 예산 안이다.

**Files:**
- Create(생성물): `lab/results/stage0/{rule_stats.json, sample.json, sessions.json, jev_calls.jsonl, summary.json}`, `docs/lab/stage0-report.md`, `lab/attempts.jsonl`

- [ ] **Step 1: 규칙 통계와 표본**

Run: CLI 명령 `stage0-rule`
Expected: `bars=` 약 52만, `N=` 선택값, 구간별 후보 수, `sample=300`. 종료 코드 0. 2면 후보 부족으로 NO-GO이며 spec 7절 대응(개발 구간 안에서 규칙 재선택)을 시도 원장에 남긴다.

- [ ] **Step 2: 표본 상태가 익명인지 확인**

Run: `python3 -c "import json;s=json.load(open('lab/results/stage0/sample.json'));print(sorted(s[0]['state']['features']))"`
Expected: 특징 11개 이름만 출력. 가격·시각 필드 없음.

- [ ] **Step 3: 세션 1 호출**

Run: CLI 명령 `stage0-call --session 1`
Expected: 100줄 진행 로그, `누적 비용 …`

- [ ] **Step 4: 반복 측정**

Run: CLI 명령 `stage0-repeat`
Expected: 200줄 진행 로그

- [ ] **Step 5: 중간 리포트**

Run: CLI 명령 `stage0-report`
Expected: `판정: PARTIAL` (세션 2·3 전)

- [ ] **Step 6: 키 노출 검사 후 커밋**

Run: `python3 - <<'PY'` 블록에서 `~/.config/typesafe/api_key` 값을 읽어 `lab/results/stage0/jev_calls.jsonl`·`docs/lab/stage0-report.md`에 포함되는지만 검사하고 True/False만 출력한다(값은 출력하지 않는다).
Expected: False

```bash
git add -- lab/results/stage0 docs/lab/stage0-report.md lab/attempts.jsonl
git commit -m "chore(lab): Stage 0 세션 1·반복 측정 결과(PARTIAL)" -- lab/results/stage0 docs/lab/stage0-report.md lab/attempts.jsonl
```

### Task 8: Stage 0 세션 2·3과 최종 판정 (세션 1 시작 후 2시간·4시간 이후)

- [ ] **Step 1:** CLI 명령 `stage0-call --session 2` (세션 1 시작 후 2시간 이후)
- [ ] **Step 2:** CLI 명령 `stage0-call --session 3` (세션 2 시작 후 2시간 이후)
- [ ] **Step 3:** CLI 명령 `stage0-report` → `판정: GO` 또는 `NO-GO`
- [ ] **Step 4:** Task 7 Step 6과 같은 키 노출 검사 후 결과 커밋, PR 본문에 판정·지연·비용 요약 갱신
- [ ] **Step 5:** NO-GO면 spec 7절 대응을 골라 시도 원장에 남기고, GO면 Stage 1 계획(aggTrades 체결·재생·비교 갈래·τ 선택)을 쓴다.
