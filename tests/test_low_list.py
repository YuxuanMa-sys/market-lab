"""低位清单的口径测试（2026-10-17 起系统只输出"哪支票在低位"）。"""
import pandas as pd

import market_lab.report as report


def test_tier_boundaries():
    assert report._tier(44.9) is None
    assert report._tier(45) == "接近低位"
    assert report._tier(54.9) == "接近低位"
    assert report._tier(55) == "低位"
    assert report._tier(69.9) == "低位"
    assert report._tier(70) == "深度低位"


def _stock(t, dip, reasons):
    return {"ticker": t, "dip": {"score": dip, "reasons": reasons}, "price": 90.0, "chg_pct": -5.0,
            "rsi": 30.0, "atr": 3.0, "ma": {20: {"value": 99.0}}, "high_52w": 150.0, "earnings_date": None}


def test_low_list_filters_sorts_and_hides_zone_prices(monkeypatch):
    df = pd.DataFrame({"High": [100.0] * 30, "Close": [95.0] * 30})
    monkeypatch.setattr(report.data, "get_daily", lambda t, period="2y": df)
    stocks = [
        _stock("A", 40, ["RSI 40 偏弱"]),                                   # 低于 45：不进清单
        _stock("B", 60, ["RSI 33 偏弱", "贴近强支撑区 85.00-87.00"]),
        _stock("C", 82, ["RSI 28 超卖", "距强支撑区 80.00-82.00 约1.2个ATR"]),
    ]
    out = report.build_low_list(stocks, {"C": {"symbol": "C", "cost": 100.0}})
    assert [r["ticker"] for r in out] == ["C", "B"]               # 按抄底分从高到低
    assert out[0]["tier"] == "深度低位" and out[1]["tier"] == "低位"
    assert out[0]["held"] is True and out[1]["held"] is False
    # 支撑区与随机价位止跌力无差（2026-10-17 研究），理由里不显示区间价位，免得被当成买点
    assert all("支撑区" not in x for r in out for x in r["reasons"])
    assert out[1]["dd20_pct"] == -10.0                            # 90 / 100 - 1
    assert out[1]["dev_atr"] == -3.0                              # (90 - 99) / 3
