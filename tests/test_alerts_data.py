"""盘中提醒和数据层的回归测试（2026-11-04：Polygon 掉到免费档后暴露的问题）。"""
import numpy as np
import pandas as pd
import pytest

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


def _crash(monkeypatch, df, bars):
    now = 2_000_000_000
    monkeypatch.setattr(alerts.time, "time", lambda: now)
    monkeypatch.setattr(data, "get_daily", lambda t, period="2y": df)
    monkeypatch.setattr(data, "get_intraday", lambda t: {"last": 70.0, "prev_close": 100.0, "chg_pct": -30.0,
                                                         "bars": [(now + dt, c) for dt, c in bars]})


def test_already_pushed_today_is_not_repeated(monkeypatch):
    _crash(monkeypatch, _df(), [(-3700, 99.0), (-100, 70.0)])
    a = alerts._check_one({"symbol": "VST"}, held=set(), sent={})
    assert a is not None
    assert alerts._check_one({"symbol": "VST"}, held=set(), sent={"VST": a["tier"]}) is None    # 这一档今天推过了
    assert alerts._check_one({"symbol": "VST"}, held=set(), sent={"VST": "深度低位"}) is None


def test_tier_upgrade_is_pushed_again(monkeypatch):
    _crash(monkeypatch, _df(), [(-3700, 99.0), (-100, 70.0)])
    monkeypatch.setattr(alerts, "_tier", lambda v: "深度低位" if v >= 55 else None)
    monkeypatch.setattr(alerts, "_live_dip_estimate", lambda *a, **k: 75)
    monkeypatch.setattr(alerts, "dip_score", lambda *a, **k: {"score": 10})
    assert alerts._check_one({"symbol": "VST"}, held=set(), sent={"VST": "低位"})["tier"] == "深度低位"


def test_earlier_dip_that_was_never_pushed_still_alerts(monkeypatch):
    """回归：上一小时分钟线已经跌到过这个价，但当时没推，这次必须推（2026-11-04 FIS、IREN 就这样全天没提醒）。"""
    _crash(monkeypatch, _df(), [(-7400, 70.0), (-3700, 72.0), (-100, 70.0)])
    assert alerts._check_one({"symbol": "FIS"}, held=set(), sent={}) is not None


def test_same_tier_at_yesterdays_close_is_not_new(monkeypatch):
    _crash(monkeypatch, _df(), [(-3700, 99.0), (-100, 70.0)])
    monkeypatch.setattr(alerts, "_tier", lambda v: "低位")
    monkeypatch.setattr(alerts, "_live_dip_estimate", lambda *a, **k: 60)
    monkeypatch.setattr(alerts, "dip_score", lambda *a, **k: {"score": 60})
    assert alerts._check_one({"symbol": "VST"}, held=set(), sent={}) is None


def test_todays_bar_is_cut_after_close(monkeypatch):
    """回归：美东 16:20 之后日线带上今天，"昨收"会变成今天的收盘，今天被算两遍。"""
    df = _df(end="2026-11-04")
    today = pd.Timestamp("2026-11-04 15:30").to_pydatetime()
    monkeypatch.setattr(data, "_now_ct", lambda: today)
    seen = []
    def fake(d, last, sup, tol, hi=None, lo=None):
        seen.append(d.index[-1])
        return 0.0
    monkeypatch.setattr(alerts, "_live_dip_estimate", fake)
    monkeypatch.setattr(data, "get_daily", lambda t, period="2y": df)
    monkeypatch.setattr(data, "get_intraday", lambda t: {"last": 100.0, "prev_close": 100.0, "chg_pct": 0.0, "bars": []})
    assert alerts._check_one({"symbol": "KO"}, held=set()) is None
    assert seen == [pd.Timestamp("2026-11-03")]


def test_synthetic_bar_uses_todays_high_low(monkeypatch):
    """回归：合成K线的高低点曾经取昨天的，ATR 算错，估分能差 10 分、跨过 55 线（CIEN 11/02 估 53，收盘 60）。"""
    df = _df()
    seen = {}
    def fake(d, sup, tol):
        seen.update(high=float(d["High"].iloc[-1]), low=float(d["Low"].iloc[-1]), close=float(d["Close"].iloc[-1]))
        return {"score": 1}
    monkeypatch.setattr(alerts, "dip_score", fake)
    alerts._live_dip_estimate(df, 90.0, [], 0, 95.0, 88.0)
    assert seen == {"high": 95.0, "low": 88.0, "close": 90.0}
    alerts._live_dip_estimate(df, 90.0, [], 0)                  # 取不到今天的高低点：用现价，不用昨天的
    assert seen == {"high": 90.0, "low": 90.0, "close": 90.0}


def test_rising_price_is_not_a_new_entry(monkeypatch):
    """"贴近强支撑"的加分会让价格上涨时分数反而变高；涨上去的不算"跌进低位"。"""
    df = _df()
    now = 2_000_000_000
    monkeypatch.setattr(alerts.time, "time", lambda: now)
    monkeypatch.setattr(data, "get_daily", lambda t, period="2y": df)
    monkeypatch.setattr(data, "get_intraday", lambda t: {"last": 70.0, "prev_close": 100.0, "chg_pct": -30.0,
                                                         "bars": [(now - 3700, 69.0), (now - 100, 70.0)]})
    monkeypatch.setattr(alerts, "_tier", lambda v: "低位" if v == 99 else None)     # 一小时前不在低位，现在在
    monkeypatch.setattr(alerts, "_live_dip_estimate", lambda d, p, sup, tol, hi=None, lo=None: 99 if p == 70.0 else 10)
    monkeypatch.setattr(alerts, "dip_score", lambda *a, **k: {"score": 10})
    assert alerts._check_one({"symbol": "GOOG"}, held=set()) is None


def test_unpushed_low_stock_that_bounced_a_little_still_alerts(monkeypatch):
    """回归：一小时前已经在这一档只是还没推过（上次放不下或没跑成），反弹一点也要推。2026-10-30 QBTS 跌 10.9% 全天没写出名字。"""
    _crash(monkeypatch, _df(), [(-3700, 69.5), (-100, 70.0)])
    a = alerts._check_one({"symbol": "QBTS"}, held=set(), sent={})
    assert a is not None and a["tier"] in ("低位", "深度低位")
    assert alerts._check_one({"symbol": "QBTS"}, held=set(), sent={"QBTS": "深度低位"}) is None


def test_up_on_the_day_is_not_a_new_entry(monkeypatch):
    """当天还在涨的票，从高点回落也不算跌进低位（回放 10/26：FN +5.8%、CRWV +4.6% 都曾被推成新进入低位）。"""
    df = _df()
    now = 2_000_000_000
    monkeypatch.setattr(alerts.time, "time", lambda: now)
    monkeypatch.setattr(data, "get_daily", lambda t, period="2y": df)
    monkeypatch.setattr(data, "get_intraday", lambda t: {"last": 105.0, "prev_close": 100.0, "chg_pct": 5.0,
                                                         "bars": [(now - 3700, 108.0), (now - 100, 105.0)]})
    monkeypatch.setattr(alerts, "_tier", lambda v: "低位" if v == 99 else None)
    monkeypatch.setattr(alerts, "_live_dip_estimate", lambda *a, **k: 99)
    monkeypatch.setattr(alerts, "dip_score", lambda *a, **k: {"score": 10})
    assert alerts._check_one({"symbol": "FN"}, held=set()) is None


def test_quiet_stock_skips_zone_work(monkeypatch):
    df = _df()
    monkeypatch.setattr(data, "get_daily", lambda t, period="2y": df)
    monkeypatch.setattr(data, "get_intraday", lambda t: {"last": 100.0, "prev_close": 100.0, "chg_pct": 0.0, "bars": []})
    def boom(*a, **k):
        raise AssertionError("不该算支撑区")
    monkeypatch.setattr(alerts, "build_zones", boom)
    assert alerts._check_one({"symbol": "KO"}, held=set()) is None


def _no_state(monkeypatch, sent=None):
    monkeypatch.setattr(alerts, "_market_open_today", lambda: True)
    monkeypatch.setattr(alerts.alertstate, "load_sent", lambda day: {} if sent is None else sent)


def test_scan_reports_failures_instead_of_hiding_them(monkeypatch):
    _no_state(monkeypatch)
    monkeypatch.setattr(data, "get_daily", lambda t, period="2y": (_ for _ in ()).throw(ValueError("x")))
    monkeypatch.setattr("market_lab.report.load_holdings", lambda: ["AAA"])
    res = alerts.scan([{"symbol": "BBB"}])
    assert res["alerts"] == [] and res["n_ok"] == 0 and sorted(res["failed"]) == ["AAA", "BBB"]
    assert res["sent_ok"] is True and res["market_closed"] is False


def test_scan_passes_sent_and_flags_unreadable_state(monkeypatch):
    _crash(monkeypatch, _df(), [(-3700, 99.0), (-100, 70.0)])
    monkeypatch.setattr(alerts, "_market_open_today", lambda: True)
    monkeypatch.setattr("market_lab.report.load_holdings", lambda: [])
    monkeypatch.setattr(alerts.alertstate, "load_sent", lambda day: {"VST": "深度低位"})
    assert alerts.scan([{"symbol": "VST"}])["alerts"] == []
    # 读不到记录：宁可重复也不漏，并且标出来
    monkeypatch.setattr(alerts.alertstate, "load_sent", lambda day: None)
    res = alerts.scan([{"symbol": "VST"}])
    assert [a["ticker"] for a in res["alerts"]] == ["VST"] and res["sent_ok"] is False


def test_market_closed_day_is_skipped(monkeypatch):
    now = pd.Timestamp("2026-11-26 11:00", tz="America/Chicago").to_pydatetime()
    monkeypatch.setattr(data, "_now_ct", lambda: now)
    wed = int(pd.Timestamp("2026-11-25 14:55", tz="America/Chicago").timestamp())
    monkeypatch.setattr(data, "get_intraday", lambda t: {"last": 1.0, "bars": [(wed, 1.0)]})
    res = alerts.scan([{"symbol": "VST"}])
    assert res["market_closed"] is True and res["alerts"] == [] and res["failed"] == []
    today = int(pd.Timestamp("2026-11-26 10:55", tz="America/Chicago").timestamp())
    monkeypatch.setattr(data, "get_intraday", lambda t: {"last": 1.0, "bars": [(today, 1.0)]})
    assert alerts._market_open_today() is True
    monkeypatch.setattr(data, "get_intraday", lambda t: {})         # 取不到不算休市：照常检查，失败会报出来
    assert alerts._market_open_today() is True


def _alerts(n, sector="存储", ticker="T"):
    return [{"ticker": f"{ticker}{i}", "held": i == 0, "sector": sector, "tier": "低位", "dip_est": 60.0 - i % 5,
             "chg_pct": -5.25, "last": 12.5} for i in range(n)]


def test_push_text_format():
    assert alerts.push_text([]) == ("", [])
    text, shown = alerts.push_text(_alerts(40))
    lines = text.split("\n")
    assert lines[0].startswith("📉今天新进入低位") and lines[1] == "T0★[存储] 低位60 -5.2% 现价12.5"
    assert lines[5].startswith("另有 T4[存储]") and lines[5].endswith("只") and len(text) <= 200
    # 只有写出名字的票才算推过；+N只 下一小时接着推
    assert shown[:5] == ["T0", "T1", "T2", "T3", "T4"] and all(t in text for t in shown)
    assert lines[5].endswith(f"+{40 - len(shown)}只")


def test_push_text_never_exceeds_budget():
    long_row = {"ticker": "BRK-B", "held": True, "sector": "计算芯片", "tier": "深度低位", "dip_est": 100.0,
                "chg_pct": -29.75, "last": 1739.88}
    for n in (1, 2, 3, 4, 5, 6, 8, 12, 40, 400):
        for warn in ((0, 231, True), (30, 231, True), (30, 231, False), (0, 231, False)):
            text, shown = alerts.push_text([dict(long_row) for _ in range(n)], *warn)
            assert len(text) <= alerts.PUSH_MAX, (n, warn, len(text))
            assert shown
            assert (n == len(shown)) == ("只" not in text.split("\n")[-1])


def test_mass_failure_is_pushed_even_without_alerts():
    """回归：Yahoo 全部失败时输出是空的，模型回复"无新进入低位"，用户分不清是没票还是没查成。"""
    text, shown = alerts.push_text([], 231, 231)
    assert "231/231" in text and "⚠" in text and shown == []
    assert alerts.push_text([], 20, 231) == ("", [])            # 个别失败只写 stderr
    text, _ = alerts.push_text(_alerts(3), 21, 231)
    assert text.startswith("⚠盘中检查21/231") and "T0" in text


def test_state_warning_is_shown_only_with_alerts():
    assert alerts.push_text([], 0, 231, sent_ok=False) == ("", [])
    assert alerts.push_text(_alerts(1), 0, 231, sent_ok=False)[0].startswith("⚠读不到")


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
    payload = {"meta": {"regularMarketPrice": 120.0, "chartPreviousClose": 140.0, "instrumentType": "EQUITY",
                        "regularMarketDayHigh": 128.0, "regularMarketDayLow": 117.0},
               "timestamp": [1, 2, 3], "indicators": {"quote": [{"close": [121.0, None, 120.0]}]}}
    monkeypatch.setattr(data, "_yahoo_get", lambda t, p: payload)
    q = data.get_intraday("VST")
    assert round(q["chg_pct"], 2) == -14.29 and q["bars"] == [(1, 121.0), (3, 120.0)]
    assert q["day_high"] == 128.0 and q["day_low"] == 117.0


def test_instrument_type_mapping(monkeypatch):
    monkeypatch.setattr(data, "has_polygon", lambda: False)
    monkeypatch.setattr(data, "get_intraday", lambda t: {"type": "ETF"})
    assert data.get_instrument_type("SOXX") == "ETF"
    monkeypatch.setattr(data, "get_intraday", lambda t: {"type": "EQUITY"})
    assert data.get_instrument_type("NVDA") == "CS"
    monkeypatch.setattr(data, "get_intraday", lambda t: {})
    assert data.get_instrument_type("XXXX") is None


def test_daily_missing_latest_bar_counts_as_failed(monkeypatch):
    """回归：日线缺最近一根时拿旧收盘算：涨 5% 的票被推成新进入低位，真跌的票反而漏掉。和盘后报告同一口径，算取数失败。"""
    full = _df()
    _crash(monkeypatch, full.iloc[:-1], [(-100, 70.0)])
    with pytest.raises(ValueError, match="日线只到"):
        alerts._check_one({"symbol": "VST"}, held=set(), sent={}, as_of=full.index[-1])
    assert alerts._check_one({"symbol": "VST"}, held=set(), sent={}, as_of=full.index[-2]) is not None    # 不缺就照常


def _quote(monkeypatch, df, last, prev):
    monkeypatch.setattr(data, "get_daily", lambda t, period="2y": df)
    monkeypatch.setattr(data, "get_intraday", lambda t: {"last": last, "prev_close": prev, "chg_pct": (last / prev - 1) * 100, "bars": []})


def test_split_day_with_unadjusted_data_counts_as_failed(monkeypatch):
    """回归：拆股当天日线和报价没调到同一口径，持仓票被推成深度低位，还带着 -86.8%。"""
    df = _df()
    close = float(df["Close"].iloc[-1])
    for last, prev in ((close / 8, close / 8), (close / 8, close), (close * 8, close / 8)):
        _quote(monkeypatch, df, last, prev)
        with pytest.raises(ValueError, match="拆股"):
            alerts._check_one({"symbol": "SNDK"}, held=set())
    adjusted = df.copy()
    adjusted[["Open", "High", "Low", "Close"]] /= 8
    _quote(monkeypatch, adjusted, close / 8 * 0.7, close / 8)                  # 都调好了：照常
    assert alerts._check_one({"symbol": "SNDK"}, held=set()) is not None


def test_ex_dividend_prev_close_is_not_a_failure(monkeypatch):
    """除息日 Yahoo 的 chartPreviousClose 减掉了分红（EQT 54.25→54.08），不能算日线对不上。"""
    df = _df()
    close = float(df["Close"].iloc[-1])
    _quote(monkeypatch, df, close * 0.70, close * (1 - 0.0597))                # 观察池一年内最大一次分红 PGR 5.97%
    assert alerts._check_one({"symbol": "PGR"}, held=set()) is not None


def test_real_crash_is_not_mistaken_for_a_split(monkeypatch):
    df = _df()
    close = float(df["Close"].iloc[-1])
    _quote(monkeypatch, df, close * 0.546, close)                              # 两年内观察池单日最大跌幅 RGTI -45.4%
    assert alerts._check_one({"symbol": "RGTI"}, held=set()) is not None


def test_ticker_with_no_bar_today_is_not_an_alert(monkeypatch):
    """回归：休市日 SPY 取不到时照常检查，日线收盘是 float32（76.9000015），报价是 76.9，比昨收"低"，上一交易日的跌幅被推成新进入低位。"""
    _crash(monkeypatch, _df(), [(-100, 70.0)])
    assert alerts._check_one({"symbol": "IBKR"}, held=set()) is not None
    _crash(monkeypatch, _df(), [(-90_000, 70.0)])
    assert alerts._check_one({"symbol": "IBKR"}, held=set()) is None


def test_lower_tier_unnamed_an_hour_ago_is_not_hidden_by_a_higher_score_now(monkeypatch):
    """回归：一小时前估分 62（低位）没写出名字，现在 75 反而被挡；现在要是还是 62 就会推。2026-10-30 COIN 跌 10.6%，全天没写出名字。"""
    monkeypatch.setattr(alerts, "dip_score", lambda d, sup, tol: {"score": 30.0})
    now = 2_000_000_000
    monkeypatch.setattr(alerts.time, "time", lambda: now)
    monkeypatch.setattr(data, "get_daily", lambda t, period="2y": _df())
    monkeypatch.setattr(data, "get_intraday", lambda t: {"last": 91.0, "prev_close": 100.0, "chg_pct": -9.0, "bars": [(now - 3700, 85.0), (now - 100, 90.5)]})
    monkeypatch.setattr(alerts, "_live_dip_estimate", lambda d, p, sup, tol, hi=None, lo=None: 75.0 if p >= 90 else 62.0)
    assert alerts._check_one({"symbol": "COIN"}, held=set(), sent={})["tier"] == "深度低位"
    assert alerts._check_one({"symbol": "COIN"}, held=set(), sent={"COIN": "低位"}) is None       # 低位推过了，深度是涨上来才有的
    monkeypatch.setattr(alerts, "_live_dip_estimate", lambda d, p, sup, tol, hi=None, lo=None: 63.0 if p >= 90 else 43.0)
    assert alerts._check_one({"symbol": "GOOG"}, held=set(), sent={}) is None                      # 一小时前够不上：还是拦
