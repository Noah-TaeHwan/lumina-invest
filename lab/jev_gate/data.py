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


def resample_klines(k: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """1분봉을 UTC minutes분 경계로 묶는다. 1분봉이 minutes개 다 있는 봉만 남긴다(빠진 분이 있으면 버림)."""
    if minutes == 1:
        return k
    width = minutes * MINUTE_US
    g = k.assign(_bucket=k["open_time"] // width * width)
    out = g.groupby("_bucket", sort=True).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
        volume=("volume", "sum"), taker_buy_base=("taker_buy_base", "sum"), n=("open", "size"))
    out = out[out["n"] == minutes].drop(columns="n").reset_index(names="open_time")
    out["t_close"] = out["open_time"] + width
    return out
