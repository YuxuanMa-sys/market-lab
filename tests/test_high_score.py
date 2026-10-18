import numpy as np
import pandas as pd

from market_lab.report import _high_tier
from market_lab.stock import high_score


def _df(close, vol=None, n_flat=300):
    c = np.r_[np.full(n_flat, close[0]), close].astype(float)
    v = np.full(len(c), 1e6) if vol is None else np.r_[np.full(n_flat, 1e6), vol]
    return pd.DataFrame({"Open": c, "High": c * 1.005, "Low": c * 0.995, "Close": c, "Volume": v},
                        index=pd.bdate_range("2024-01-01", periods=len(c)))


def _zigzag(drift, n=30, start=100.0):
    steps = np.where(np.arange(n) % 3 == 2, -0.01, drift)      # 每 3 天跌一天，RSI 才有定义
    return start * np.cumprod(1 + steps)


def test_strong_rally_scores_high_and_sideways_scores_low():
    side = high_score(_df(np.r_[np.full(300, 120.0), _zigzag(0.005)], n_flat=0))
    assert side["score"] < 45 and side["band"] == "不在高位"
    rally = high_score(_df(_zigzag(0.03)))
    assert rally["score"] >= 70
    assert any("52周高" in r for r in rally["reasons"])
    assert rally["facts"]["extreme_runup"] is False


def test_short_history_skips_52w_term_and_too_short_returns_none():
    short = high_score(_df(100 * 1.02 ** np.arange(30), n_flat=50))
    assert any("上市不足一年" in r for r in short["reasons"])
    assert high_score(_df(np.full(5, 10.0), n_flat=5))["score"] is None


def test_volume_climax_needs_up_day():
    c = 100 * 1.004 ** np.arange(30)
    v = np.full(30, 1e6); v[-1] = 5e6
    up = high_score(_df(c, v))
    c2 = c.copy(); c2[-1] = c2[-2] * 0.99
    down = high_score(_df(c2, v))
    assert any("放量上涨" in r for r in up["reasons"])
    assert not any("放量上涨" in r for r in down["reasons"])


def test_high_tiers():
    assert [_high_tier(x) for x in (None, 44, 45, 54, 55, 69, 70, 100)] == \
        [None, None, "接近高位", "接近高位", "高位", "高位", "深度高位", "深度高位"]
