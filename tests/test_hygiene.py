import numpy as np
import pandas as pd

from market_lab import data, hygiene
from market_lab.hygiene import find_splices, window_stats


def _df(dates, close, vol):
    return pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close, "Volume": vol},
                        index=pd.DatetimeIndex(dates))


def test_splice_detected_on_gap_and_price_jump():
    a = pd.bdate_range("2026-01-01", periods=60)
    b = pd.bdate_range("2026-06-12", periods=60)
    df = _df(a.append(b), [22.0] * 60 + [160.0] * 60, [1e5] * 60 + [5e7] * 60)
    s = find_splices(df)
    assert len(s) == 1 and s[0]["after"] == "2026-06-12"


def test_plain_halt_is_not_a_splice():
    a = pd.bdate_range("2026-01-01", periods=60)
    b = pd.bdate_range("2026-04-01", periods=60)
    df = _df(a.append(b), [50.0] * 60 + [45.0] * 60, [1e6] * 120)
    assert find_splices(df) == []


def test_window_counts_missing_days_as_zero_volume():
    cal = pd.bdate_range("2026-09-01", periods=30)
    keep = cal.delete([25, 27])                       # 最近 20 天里缺 2 天
    df = _df(keep, [10.0] * len(keep), [6e6] * len(keep))
    w = window_stats(df, cal)
    assert w["traded20"] == 18
    assert abs(w["dvol20_m"] - 54.0) < 1e-6           # 18 天 × $60M / 20
    assert w["fails"] == ["3 近20个交易日只有 18 天有成交"]


def test_price_and_liquidity_floors():
    cal = pd.bdate_range("2026-09-01", periods=30)
    df = _df(cal, [4.0] * 30, [1e6] * 30)
    fails = window_stats(df, cal)["fails"]
    assert any(f.startswith("2 ") for f in fails) and any(f.startswith("3 20日均成交额") for f in fails)
    assert np.isclose(window_stats(df, cal[:-5])["price"], 4.0)


def test_unknown_type_blocks_new_entry_but_does_not_remove(monkeypatch):
    """回归：类型查询被限流时 fails_new 是空的，ETF 能混进观察池（SOXX 就这样进来过）。"""
    cal = pd.bdate_range("2024-09-01", periods=520)
    df = _df(cal, [500.0] * len(cal), [1e7] * len(cal))
    df["High"] = 505.0                                # ATR 要大于 0
    monkeypatch.setattr(data, "get_daily", lambda t, period="2y": df)
    monkeypatch.setattr(data, "get_instrument_type", lambda t: None)
    r = hygiene._check_one({"symbol": "SMH"}, set(), cal)
    assert r["fails_new"] == ["1 类型未知（没查成）"] and r["remove_eligible"] is False
    monkeypatch.setattr(data, "get_instrument_type", lambda t: "ETF")
    r = hygiene._check_one({"symbol": "SMH"}, set(), cal)
    assert r["fails_new"] == ["1 类型 ETF 不是个股"] and r["remove_eligible"] is True
    monkeypatch.setattr(data, "get_instrument_type", lambda t: "CS")
    assert hygiene._check_one({"symbol": "NVDA"}, set(), cal)["fails_new"] == []


def test_not_found_is_separate_from_a_failed_fetch(monkeypatch):
    """回归：退市的票（HES）和一次限流输出一样，周度任务永远剔不掉，还算进覆盖。"""
    cal = pd.bdate_range("2026-09-01", periods=30)
    good = _df(cal, [100.0] * 30, [1e7] * 30)
    def fake_daily(t, period="2y"):
        if t == "DEAD":
            raise data.SymbolNotFound("Yahoo 没有 DEAD")
        if t == "BUSY":
            raise ValueError("取不到 BUSY 的日线")
        return good
    monkeypatch.setattr(data, "get_daily", fake_daily)
    monkeypatch.setattr(data, "get_instrument_type", lambda t: "CS")
    wl = [{"symbol": "AAA", "sector": "油气"}, {"symbol": "DEAD", "sector": "油气"}, {"symbol": "BUSY", "sector": "油气"}]
    res = hygiene.check(wl)
    assert res["not_found"] == ["DEAD"]
    assert set(res["failed"]) == {"DEAD", "BUSY"}
    assert res["coverage"]["counts"]["油气"] == 2       # DEAD 不算，BUSY 只是这次没查成
