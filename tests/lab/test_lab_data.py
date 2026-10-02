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


def test_resample_klines_to_5m_drops_incomplete_bars():
    import pandas as pd
    from tests.lab.bars import make_minute_bars
    k = make_minute_bars(20)                      # 00:00~00:19, 5분봉 4개
    k = k.drop(index=7).reset_index(drop=True)    # 00:05~00:09 봉에서 1분 누락
    r = data.resample_klines(k, 5)
    assert len(r) == 3
    first = k.iloc[:5]
    row = r.iloc[0]
    assert row["open"] == first["open"].iloc[0] and row["close"] == first["close"].iloc[-1]
    assert row["high"] == first["high"].max() and row["low"] == first["low"].min()
    assert row["volume"] == pytest.approx(first["volume"].sum())
    assert row["t_close"] - row["open_time"] == 5 * data.MINUTE_US
    assert (r["open_time"] % (5 * data.MINUTE_US) == 0).all()


def test_resample_klines_one_minute_is_identity():
    from tests.lab.bars import make_minute_bars
    k = make_minute_bars(10)
    assert data.resample_klines(k, 1) is k
