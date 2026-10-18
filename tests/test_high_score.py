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


# ---- 2026-10-18 对抗审查确认的问题的回归测试 ----

def test_unscorable_holding_becomes_labelled_row_and_template_renders():
    """不足 20 根日线的持仓：原来模板 "%.1f"|format(None) 抛错，整份报告写不出来。"""
    from market_lab.report import TEMPLATE, build_high_list
    s = {"ticker": "NEWIPO", "price": 10.0, "chg_pct": 1.0, "dip": {"score": 0},
         "high": high_score(_df(np.full(15, 10.0), n_flat=0))}
    rows = build_high_list([s], ["NEWIPO"])
    assert rows[0]["score"] is None and rows[0]["band"] == "数据不足" and rows[0]["error"]
    html = TEMPLATE.render(title="t", generated_at="", session="postmarket",
                           mkt={"quotes": [], "vix": {}, "breadth": None, "regime": {}, "leverage": None, "market_news": [],
                                "dip_gauge": {"score": 0, "band": "", "reasons": []},
                                "spy": {"price": 0, "events": [], "supports": [], "resistances": []},
                                "qqq": {"price": 0, "events": [], "supports": [], "resistances": []}},
                           mmode_desc="", urgent_actions=[], order_sheet=[], stop_sheet=[], n_noop=0, risk={},
                           stocks=[], card_stocks=[], low_list=[], low_context={}, high_list=rows,
                           high_context={"definition": "", "market_line": "", "footnote": ""},
                           show_trading=False, err_stocks=[], earnings=[], data_sources="")
    assert "数据不足：只有 15 根日线" in html


def test_first_high_since_counts_from_holding_date():
    from market_lab.report import first_high_since
    c = np.r_[_zigzag(0.03, n=30), _zigzag(-0.01, n=30, start=230.0), _zigzag(0.03, n=30, start=170.0)]
    df = _df(c)
    s_now = high_score(df)["score"]
    assert s_now >= 55
    early = str(df.index[300].date())          # 第一段上涨之前就持有 → 之前已经 ≥55 过，不是第一次
    late = str(df.index[-5].date())            # 第三段上涨中途才写进持仓 → 之后这几天也都 ≥55，不是第一次
    today = str(df.index[-1].date())           # 今天才写进持仓 → 是第一次
    assert first_high_since(df, early, s_now) is False
    assert first_high_since(df, today, s_now) is True
    assert first_high_since(df, None, s_now) is False
    assert first_high_since(df, late, s_now) is (all(high_score(df.loc[:d])["score"] < 55 for d in df.index[-5:-1]))


def test_required_caveats_present():
    from market_lab.report import HIGH_TEXT
    assert "不是卖出信号" in HIGH_TEXT["tier"]["高位"] and "不代表会回落" in HIGH_TEXT["tier"]["高位"]
    assert "前半段 58%" in HIGH_TEXT["pullback"]
    assert "不是卖出信号" in HIGH_TEXT["definition"]
    assert "未经前瞻验证" in HIGH_TEXT["extreme"]
    assert "{since}" in HIGH_TEXT["new_entry"] and "如果这是你低位买进后第一次" in HIGH_TEXT["new_entry"]


def test_splice_is_cut_before_scoring():
    """同代码换了证券：只用拼接点之后的历史（SPCX：旧 ETF ~$22 → 新票 ~$160）。"""
    old = pd.DataFrame({"Open": 22.0, "High": 22.1, "Low": 21.9, "Close": 22.0, "Volume": 1e3},
                       index=pd.bdate_range("2025-06-02", periods=200))
    new_idx = pd.bdate_range("2026-06-12", periods=10)
    new = pd.DataFrame({"Open": 160.0, "High": 161.0, "Low": 159.0, "Close": 160.0, "Volume": 5e7}, index=new_idx)
    h = high_score(pd.concat([old, new]))
    assert h["score"] is None and h["facts"]["n_bars"] == 10      # 不会再出现"20日涨了 600%"


def test_high_vol_gate_uses_unrounded_atr():
    """高波动句门槛 ≥4%：0.03996 不能因为取整成 0.04 而触发。"""
    from market_lab import report
    h = high_score(_df(_zigzag(0.03)))
    h["facts"]["atr_pct"] = 0.03996
    s = {"ticker": "X", "price": 10.0, "chg_pct": 0.0, "dip": {"score": 0}, "high": h}
    orig = report.data.get_daily
    report.data.get_daily = lambda t, p="2y": _df(_zigzag(0.03))
    try:
        rows = report.build_high_list([s], ["X"])
    finally:
        report.data.get_daily = orig
    assert h["score"] >= 55 and report.HIGH_TEXT["high_vol"] not in rows[0]["notes"]
