"""走真实 report.generate 的回归测试：合成K线，不联网。"""
from datetime import datetime as _dt
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from market_lab import brief, data, market, report, sectors, stock

DAYS = pd.bdate_range(end="2026-11-04", periods=300)


def _bars(seed, days=DAYS):
    r = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(r.normal(0.0005, 0.02, len(days))))
    return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c, "Volume": 1e6 + r.integers(0, 1000, len(days))}, index=days)


@pytest.fixture
def gen(monkeypatch, tmp_path):
    def run(session="postmarket", now="2026-11-04 16:30", drop=(), bad=(), cal=(), cal_fail=(), holdings=("AAA",), scale=None):
        fixed = _dt.strptime(now, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("America/Chicago"))
        class FD(_dt):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
        def direct(t, period="2y"):
            if t in bad:
                raise RuntimeError("429")
            df = _bars(sum(map(ord, t)))
            if t in (scale or {}):       # 从倒数第 ago 根起价格乘上 k：模拟暴跌或拆股没调整
                k, ago = scale[t]
                df.iloc[len(df) - 1 - ago:, df.columns.get_indexer(["Open", "High", "Low", "Close"])] *= k
            return df.iloc[:-1] if t in drop else df
        monkeypatch.setattr(data, "_now_ct", lambda: fixed)
        monkeypatch.setattr(report, "datetime", FD)
        run.n = getattr(run, "n", 0) + 1     # 每次一个新缓存目录：日线按天缓存
        monkeypatch.setattr(data, "CACHE_DIR", tmp_path / session / f"c{run.n}")
        monkeypatch.setattr(report, "REPORTS_DIR", tmp_path / session)
        (tmp_path / session).mkdir(exist_ok=True)
        monkeypatch.setattr(data, "_yahoo_direct_daily", direct)
        monkeypatch.setattr(data.yf, "Ticker", lambda t: type("Y", (), {"history": lambda self, *a, **k: pd.DataFrame(), "news": []})())
        monkeypatch.setattr(data, "get_quote", lambda t: {})
        monkeypatch.setattr(data, "get_news", lambda t, n=6: [])
        monkeypatch.setattr(data, "get_market_news", lambda limit=8: [])
        monkeypatch.setattr(data, "get_earnings_calendar", lambda d=14, problems=None: (problems.extend(cal_fail) if problems is not None else None) or list(cal))
        monkeypatch.setattr(data, "get_next_earnings", lambda t, d=45, problems=None: None)
        monkeypatch.setattr(data, "has_polygon", lambda: False)
        monkeypatch.setattr(data, "polygon_status", lambda: "free")
        monkeypatch.setattr(market, "BREADTH_UNIVERSE", ["AAA", "BBB"])
        monkeypatch.setattr(report, "load_watchlist", lambda: [{"symbol": t, "sector": "存储"} for t in ("AAA", "BBB", "CCC")])
        monkeypatch.setattr(report, "load_holdings", lambda: list(holdings))
        monkeypatch.setattr(report, "holding_since", lambda h: {t: None for t in h})
        return report.generate(session)
    return run


def test_spy_alone_lagging_is_not_market_closed(gen):
    p = gen(drop=["SPY"])
    assert "market_closed" not in p and "as_of=2026-11-04" in p["data"] and "failed=0" in p["data"]
    md = p["brief"].read_text(encoding="utf-8")
    assert md.startswith("数据日期：2026-11-04 收盘") and "SPY 日线只到" in md


def test_whole_market_lagging_is_still_market_closed(gen):
    p = gen(now="2026-11-05 16:30")
    assert "market_closed" in p and "as_of=2026-11-04" in p["data"]


def test_one_stock_lagging_still_counts_as_failed(gen):
    assert "ok=2 failed=1" in gen(drop=["BBB"])["data"]


def test_lagging_reference_series_are_left_out(gen, monkeypatch):
    p = gen(drop=["^VIX", "SMH", "QQQ"])
    assert "VIX" not in p["push2"].read_text(encoding="utf-8") and "SMH" not in p["brief"].read_text(encoding="utf-8")
    assert "QQQ 纳指100 关键位" not in p["html"].read_text(encoding="utf-8")


def test_html_labels_and_no_leverage_card(gen):
    cal = [{"symbol": "AAA", "date": "2026-11-12", "hour": "", "eps_estimate": None}]
    h = gen(session="premarket", now="2026-11-05 07:50", cal=cal)["html"].read_text(encoding="utf-8")
    assert "站上200日均线，上一交易日" in h and "<th>上一日</th>" in h and "未来45天" in h and "<th>距20日低点</th>" in h
    assert "杠杆红绿灯" not in h and "当前调度模式" not in h


def test_html_all_failed_does_not_claim_no_lows(gen):
    p = gen(bad=["AAA", "BBB", "CCC"])
    h = p["html"].read_text(encoding="utf-8")
    assert "没有处在低位" not in h and "全部取数失败" in h


def test_vix_failure_is_named_in_the_card(gen):
    h = gen(bad=["^VIX"])["html"].read_text(encoding="utf-8")
    assert "VIX 取数失败" in h


def test_two_calendar_dates_keep_the_nearest(gen):
    cal = [{"symbol": "AAA", "date": "2026-11-12", "hour": "", "eps_estimate": None},
           {"symbol": "AAA", "date": "2026-12-03", "hour": "", "eps_estimate": None}]
    p = gen(cal=cal, session="premarket", now="2026-11-05 07:50")
    import json
    J = json.loads(p["json"].read_text(encoding="utf-8"))
    assert [s["earnings_date"] for s in J["stocks"] if s["ticker"] == "AAA"] == ["2026-11-12"]


def test_calendar_problem_reaches_the_brief_and_data_line(gen):
    p = gen(session="premarket", now="2026-11-05 07:50", cal_fail=["2026-11-05~2026-11-10 取数失败"])
    assert "财报日历不全" in p["brief"].read_text(encoding="utf-8") and "财报日历不全=1处" in p["data"]
    q = gen(session="premarket", now="2026-11-05 07:50")
    assert "财报日历不全" not in q["brief"].read_text(encoding="utf-8") and "财报日历" not in q["data"]


def test_dip_score_needs_twenty_bars():
    df = _bars(1).iloc[-17:].copy()
    df["Close"] = df["Close"].iloc[0] * np.linspace(1, 0.5, 17)
    out = stock.dip_score(df, [], 0.0)
    assert out["score"] == 0 and out["band"] == "数据不足"


def _high(t, tier=True):
    return {"ticker": t, "sector": "存储", "score": 65, "prev_score": 30, "tier": "高位", "chg_pct": 1.0, "runup20_pct": 49.1,
            "dist52h_pct": -1.0, "reasons": [], "notes": []}


def test_brief_labels_gain_from_low_and_stars_near_low():
    lows = [{"ticker": "AAA", "held": True, "sector": "存储", "tier": "接近低位", "dip": 50.0, "chg_pct": -1.0},
            {"ticker": "BBB", "held": False, "sector": "存储", "tier": "接近低位", "dip": 46.0, "chg_pct": -1.0}]
    md = brief.brief_md("postmarket", [_high("CCC")], {}, lows, {"vix_line": ""}, None, {"up": [], "down": [], "sectors": []})
    assert "距20日低点+49.1%" in md and "20日涨幅" not in md and "接近低位：AAA★ BBB" in md


def test_etf_changes_skip_older_bar(monkeypatch):
    monkeypatch.setattr(data, "get_daily", lambda t, p="2y": _bars(2))
    tax = {"X": {"etf": "SMH", "subs": ["s"]}}
    assert "SMH" in sectors.etf_changes(tax, as_of="2026-11-04")
    assert "SMH" not in sectors.etf_changes(tax, as_of="2026-11-05")


def test_unadjusted_split_counts_as_failed(gen):
    """回归：拆股后历史价还没调整时，持仓票看起来跌了 87%，会被判成深度低位。"""
    for ago in (0, 4):       # 当天，或者 Yahoo 过了几天还没调好
        p = gen(scale={"AAA": (1 / 8, ago)}, session="postmarket")
        assert "ok=2 failed=1" in p["data"]
        md = p["brief"].read_text(encoding="utf-8")
        assert "AAA 取数失败" in md and "拆股" in md


def test_real_crash_is_not_mistaken_for_a_split(gen):
    assert "ok=3 failed=0" in gen(scale={"AAA": (0.546, 0)})["data"]    # 观察池两年内最大单日跌幅 RGTI -45.4%
    assert "ok=3 failed=0" in gen(scale={"AAA": (0.4, 5)})["data"]      # 真腰斩的票过几天不再算取数失败


def _high(t, tier=True):
    return {"ticker": t, "sector": "存储", "score": 65, "prev_score": 30, "tier": "高位", "chg_pct": 1.0, "runup20_pct": 49.1,
            "dist52h_pct": -1.0, "reasons": [], "notes": []}


def test_brief_labels_gain_from_low_and_stars_near_low():
    lows = [{"ticker": "AAA", "held": True, "sector": "存储", "tier": "接近低位", "dip": 50.0, "chg_pct": -1.0},
            {"ticker": "BBB", "held": False, "sector": "存储", "tier": "接近低位", "dip": 46.0, "chg_pct": -1.0}]
    md = brief.brief_md("postmarket", [_high("CCC")], {}, lows, {"vix_line": ""}, None, {"up": [], "down": [], "sectors": []})
    assert "距20日低点+49.1%" in md and "20日涨幅" not in md and "接近低位：AAA★ BBB" in md


def test_etf_changes_skip_older_bar(monkeypatch):
    monkeypatch.setattr(data, "get_daily", lambda t, p="2y": _bars(2))
    tax = {"X": {"etf": "SMH", "subs": ["s"]}}
    assert "SMH" in sectors.etf_changes(tax, as_of="2026-11-04")
    assert "SMH" not in sectors.etf_changes(tax, as_of="2026-11-05")


def test_unadjusted_split_counts_as_failed(gen):
    """回归：拆股后历史价还没调整时，持仓票看起来跌了 87%，会被判成深度低位。"""
    for ago in (0, 4):       # 当天，或者 Yahoo 过了几天还没调好
        p = gen(scale={"AAA": (1 / 8, ago)}, session="postmarket")
        assert "ok=2 failed=1" in p["data"]
        md = p["brief"].read_text(encoding="utf-8")
        assert "AAA 取数失败" in md and "拆股" in md


def test_real_crash_is_not_mistaken_for_a_split(gen):
    assert "ok=3 failed=0" in gen(scale={"AAA": (0.546, 0)})["data"]    # 观察池两年内最大单日跌幅 RGTI -45.4%
    assert "ok=3 failed=0" in gen(scale={"AAA": (0.4, 5)})["data"]      # 真腰斩的票过几天不再算取数失败


def test_estimated_earnings_dates_are_footnoted(gen):
    cal = [{"symbol": "AAA", "date": "2026-11-12", "hour": "", "eps_estimate": None}]
    md = gen(session="premarket", now="2026-11-05 07:50", cal=cal)["brief"].read_text(encoding="utf-8")
    assert ("📅2026-11-12" in md) == ("部分是估算" in md)


def test_estimated_earnings_dates_are_footnoted():
    row = {**_high("CCC"), "days_to_earnings": 3, "earnings": "2026-11-26"}
    args = ({}, [], {"vix_line": ""}, None, {"up": [], "down": [], "sectors": []})
    assert "📅2026-11-26" in (md := brief.brief_md("postmarket", [row], *args)) and "部分是估算" in md
    assert "部分是估算" not in brief.brief_md("postmarket", [_high("CCC")], *args)
