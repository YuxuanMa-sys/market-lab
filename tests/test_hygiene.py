import numpy as np
import pandas as pd

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
