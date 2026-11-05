"""盘中提醒和数据层的回归测试（2026-11-04：Polygon 掉到免费档后暴露的问题）。"""
import numpy as np
import pandas as pd

from market_lab import alerts, data


def _df(n=300, last=100.0, end="2026-11-03"):
    idx = pd.bdate_range(end=end, periods=n)
    c = np.full(n, last, dtype=float) + np.sin(np.arange(n)) * 0.5
    return pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": np.full(n, 1e6)}, index=idx)


def test_price_minutes_ago():
    bars = [(1000, 10.0), (1300, 11.0), (1600, 12.0)]
    assert alerts._price_minutes_ago(bars, minutes=5, now_ts=1600) == 11.0     # 5 分钟前
    assert alerts._price_minutes_ago(bars, minutes=60, now_ts=1600) is None    # 那时还没开盘
    assert alerts._price_minutes_ago([], minutes=60, now_ts=1600) is None


def test_partial_bar_is_dropped_only_before_close(monkeypatch):
    df = _df(end="2026-11-04")
    monkeypatch.setattr(data, "_now_ct", lambda: pd.Timestamp("2026-11-04 11:00").to_pydatetime())
    assert data._drop_partial_bar(df).index[-1] == pd.Timestamp("2026-11-03")
    monkeypatch.setattr(data, "_now_ct", lambda: pd.Timestamp("2026-11-04 16:30").to_pydatetime())
    assert data._drop_partial_bar(df).index[-1] == pd.Timestamp("2026-11-04")
    # 盘前或休市日：最后一根本来就是之前的，不动
    monkeypatch.setattr(data, "_now_ct", lambda: pd.Timestamp("2026-11-05 07:00").to_pydatetime())
    assert data._drop_partial_bar(df).index[-1] == pd.Timestamp("2026-11-04")


def test_crash_today_is_flagged_against_yesterdays_close(monkeypatch):
    """回归：今天暴跌的票必须判定为新进入低位。原先日线带今天的半根，和"昨收"比其实是和今天比，判不出来。"""
    df = _df()
    monkeypatch.setattr(data, "get_daily", lambda t, period="2y": df)
    monkeypatch.setattr(data, "get_intraday", lambda t: {"last": 70.0, "prev_close": 100.0, "chg_pct": -30.0, "bars": []})
    a = alerts._check_one({"symbol": "VST", "sector": "发电", "note": ""}, held=set())
    assert a is not None and a["sector"] == "发电"
    assert a["tier"] in ("低位", "深度低位") and a["chg_pct"] == -30.0 and a["held"] is False


def test_already_low_an_hour_ago_is_not_repeated(monkeypatch):
    df = _df()
    now = 2_000_000_000
    monkeypatch.setattr(alerts.time, "time", lambda: now)
    monkeypatch.setattr(data, "get_daily", lambda t, period="2y": df)
    monkeypatch.setattr(data, "get_intraday", lambda t: {"last": 70.0, "prev_close": 100.0, "chg_pct": -30.0,
                                                         "bars": [(now - 4000, 71.0), (now - 100, 70.0)]})
    assert alerts._check_one({"symbol": "VST"}, held=set()) is None    # 一小时前就在低位，这次不推


def test_reentry_later_the_same_day_is_not_repeated(monkeypatch):
    """两小时前跌进低位、一小时前弹回去、现在又跌进去：今天已经提醒过了，不再推。"""
    df = _df()
    now = 2_000_000_000
    monkeypatch.setattr(alerts.time, "time", lambda: now)
    monkeypatch.setattr(data, "get_daily", lambda t, period="2y": df)
    monkeypatch.setattr(data, "get_intraday", lambda t: {"last": 70.0, "prev_close": 100.0, "chg_pct": -30.0,
                                                         "bars": [(now - 7400, 70.0), (now - 3700, 99.0), (now - 100, 70.0)]})
    assert alerts._check_one({"symbol": "VST"}, held=set()) is None


def test_first_entry_after_open_is_flagged(monkeypatch):
    df = _df()
    now = 2_000_000_000
    monkeypatch.setattr(alerts.time, "time", lambda: now)
    monkeypatch.setattr(data, "get_daily", lambda t, period="2y": df)
    monkeypatch.setattr(data, "get_intraday", lambda t: {"last": 70.0, "prev_close": 100.0, "chg_pct": -30.0,
                                                         "bars": [(now - 7400, 99.0), (now - 3700, 98.0), (now - 100, 70.0)]})
    assert alerts._check_one({"symbol": "VST"}, held=set()) is not None


def test_rising_price_is_not_a_new_entry(monkeypatch):
    """"贴近强支撑"的加分会让价格上涨时分数反而变高；涨上去的不算"跌进低位"。"""
    df = _df()
    now = 2_000_000_000
    monkeypatch.setattr(alerts.time, "time", lambda: now)
    monkeypatch.setattr(data, "get_daily", lambda t, period="2y": df)
    monkeypatch.setattr(data, "get_intraday", lambda t: {"last": 70.0, "prev_close": 100.0, "chg_pct": -30.0,
                                                         "bars": [(now - 3700, 69.0), (now - 100, 70.0)]})
    monkeypatch.setattr(alerts, "_tier", lambda v: "低位" if v == 99 else None)     # 一小时前不在低位，现在在
    monkeypatch.setattr(alerts, "_live_dip_estimate", lambda d, p, sup, tol: 99 if p == 70.0 else 10)
    monkeypatch.setattr(alerts, "dip_score", lambda *a, **k: {"score": 10})
    assert alerts._check_one({"symbol": "GOOG"}, held=set()) is None


def test_quiet_stock_skips_zone_work(monkeypatch):
    df = _df()
    monkeypatch.setattr(data, "get_daily", lambda t, period="2y": df)
    monkeypatch.setattr(data, "get_intraday", lambda t: {"last": 100.0, "prev_close": 100.0, "chg_pct": 0.0, "bars": []})
    def boom(*a, **k):
        raise AssertionError("不该算支撑区")
    monkeypatch.setattr(alerts, "build_zones", boom)
    assert alerts._check_one({"symbol": "KO"}, held=set()) is None


def test_scan_reports_failures_instead_of_hiding_them(monkeypatch):
    monkeypatch.setattr(data, "get_daily", lambda t, period="2y": (_ for _ in ()).throw(ValueError("x")))
    monkeypatch.setattr("market_lab.report.load_holdings", lambda: ["AAA"])
    res = alerts.scan([{"symbol": "BBB"}])
    assert res["alerts"] == [] and res["n_ok"] == 0 and sorted(res["failed"]) == ["AAA", "BBB"]


def test_push_text_format():
    a = [{"ticker": f"T{i}", "held": i == 0, "sector": "存储", "tier": "低位", "dip_est": 60.0 - i, "chg_pct": -5.25, "last": 12.5}
         for i in range(40)]
    assert alerts.push_text([]) == ""
    text = alerts.push_text(a)
    lines = text.split("\n")
    assert lines[0].startswith("📉盘中新进入低位") and lines[1] == "T0★[存储] 低位60 -5.2% 现价12.5"
    assert lines[5].startswith("另有 T4[存储]56") and lines[5].endswith("只") and len(text) <= alerts.PUSH_MAX


def test_polygon_free_tier_is_not_used(monkeypatch):
    class R:
        status_code = 403
    monkeypatch.setenv("POLYGON_API_KEY", "x")
    monkeypatch.setattr(data, "_poly_state", None)
    monkeypatch.setattr(data.requests, "get", lambda *a, **k: R())
    assert data.polygon_status() == "free" and data.has_polygon() is False
    monkeypatch.setattr(data, "_poly_state", None)
    monkeypatch.delenv("POLYGON_API_KEY")
    assert data.polygon_status() == "no_key"


def test_intraday_prev_close_is_yesterday(monkeypatch):
    payload = {"meta": {"regularMarketPrice": 120.0, "chartPreviousClose": 140.0, "instrumentType": "EQUITY"},
               "timestamp": [1, 2, 3], "indicators": {"quote": [{"close": [121.0, None, 120.0]}]}}
    monkeypatch.setattr(data, "_yahoo_get", lambda t, p: payload)
    q = data.get_intraday("VST")
    assert round(q["chg_pct"], 2) == -14.29 and q["bars"] == [(1, 121.0), (3, 120.0)]


def test_instrument_type_mapping(monkeypatch):
    monkeypatch.setattr(data, "has_polygon", lambda: False)
    monkeypatch.setattr(data, "get_intraday", lambda t: {"type": "ETF"})
    assert data.get_instrument_type("SOXX") == "ETF"
    monkeypatch.setattr(data, "get_intraday", lambda t: {"type": "EQUITY"})
    assert data.get_instrument_type("NVDA") == "CS"
    monkeypatch.setattr(data, "get_intraday", lambda t: {})
    assert data.get_instrument_type("XXXX") is None
