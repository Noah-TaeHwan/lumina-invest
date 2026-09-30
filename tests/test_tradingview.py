"""TradingView 연동: 알림 파싱 · 거래 목록 CSV 성과 계산 · LEAN 비교 판정."""
import pytest

from app.services import tradingview as tv


@pytest.mark.parametrize("raw,expected", [
    ("KRX:005930", "005930"), ("NASDAQ:AAPL", "AAPL"), ("005930.KS", "005930.KS"), (" aapl ", "AAPL"),
])
def test_normalize_ticker(raw, expected):
    assert tv.normalize_ticker(raw) == expected


def test_parse_alert_buy_sell_alert():
    a = tv.parse_alert({"ticker": "KRX:005930", "action": "buy", "contracts": "3", "price": "274,500", "strategy": "MA"})
    assert (a["symbol"], a["side"], a["quantity"], a["price"]) == ("005930", "BUY", 3, 274500.0)
    assert tv.parse_alert({"ticker": "AAPL", "action": "sell", "qty": 2})["side"] == "SELL"
    assert tv.parse_alert({"ticker": "AAPL"})["side"] == "ALERT"
    assert tv.parse_alert({"ticker": "AAPL", "action": "buy", "contracts": 1, "dry_run": "true"})["dry_run"]


CSV = """Trade #,Type,Signal,Date/Time,Price,Contracts,Profit,Profit %,Cum. Profit,Cum. Profit %
1,Entry Long,L,2025-01-10,55000,1,,,
1,Exit Long,S,2025-02-10,58000,1,3000,10.00,3000,10.00
2,Entry Long,L,2025-03-01,57000,1,,,
2,Exit Long,S,2025-04-01,54000,1,-3000,-10.00,0,0
3,Entry Long,L,2025-05-01,57000,1,,,
3,Exit Long,S,2025-06-01,60000,1,3000,5.00,3000,5.00
"""


def test_parse_trade_list_csv_metrics():
    m = tv.parse_trade_list_csv(CSV)
    assert m["total_trades"] == 3
    assert m["win_rate_pct"] == pytest.approx(66.67, abs=0.01)
    # 1.10 * 0.90 * 1.05 - 1 = 3.95%
    assert m["net_profit_pct"] == pytest.approx(3.95, abs=0.01)
    assert m["max_drawdown_pct"] == pytest.approx(-10.0, abs=0.01)


def test_parse_trade_list_csv_errors():
    with pytest.raises(ValueError):
        tv.parse_trade_list_csv("")
    with pytest.raises(ValueError):
        tv.parse_trade_list_csv("a,b,c\n1,2,3\n")


def test_compare_metrics_verdicts():
    lean = {"net_profit_pct": 10.0, "max_drawdown_pct": -8.0, "total_trades": 5, "win_rate_pct": 60, "sharpe_ratio": 1.0, "lean_ok": True}
    assert tv.compare_metrics({"net_profit_pct": 11.0, "max_drawdown_pct": -9.0, "total_trades": 5}, lean)["verdict"] == "일치"
    assert tv.compare_metrics({"net_profit_pct": 17.0, "max_drawdown_pct": -8.0, "total_trades": 5}, lean)["verdict"] == "부분 일치"
    r = tv.compare_metrics({"net_profit_pct": 40.0, "max_drawdown_pct": -8.0, "total_trades": 9}, lean)
    assert r["verdict"] == "불일치"
    assert any("수수료" in c for c in r["causes"]) and any("거래 횟수" in c for c in r["causes"])
    assert tv.compare_metrics({}, {})["verdict"] == "비교 불가"
