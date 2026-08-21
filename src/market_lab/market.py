"""大盘分析：指数/期指/VIX、市场宽度、市场状态判定、大盘级抄底温度计。"""
from __future__ import annotations

import numpy as np

from . import data
from .indicators import atr, rsi, sma
from .levels import build_zones, detect_events, split_zones
from .universe import BREADTH_UNIVERSE

INDEX_TICKERS = [
    ("SPY", "标普500 ETF"),
    ("QQQ", "纳指100 ETF"),
    ("IWM", "罗素2000 ETF"),
    ("DIA", "道指 ETF"),
]
OVERNIGHT_TICKERS = [
    ("ES=F", "标普期指"),
    ("NQ=F", "纳指期指"),
    ("^VIX", "VIX恐慌指数"),
    ("^TNX", "10年期美债收益率"),
]


def breadth() -> dict:
    """市场宽度：大盘股样本中站上50/200日均线的比例、当日上涨比例。"""
    bars = data.get_daily_batch(BREADTH_UNIVERSE, period="1y")
    above50 = above200 = up = total = 0
    for t, df in bars.items():
        close = df["Close"].dropna()
        if len(close) < 200:
            continue
        total += 1
        p = float(close.iloc[-1])
        if p > float(close.rolling(50).mean().iloc[-1]):
            above50 += 1
        if p > float(close.rolling(200).mean().iloc[-1]):
            above200 += 1
        if p > float(close.iloc[-2]):
            up += 1
    if total == 0:
        return {}
    return {
        "sample": total,
        "pct_above_50dma": round(above50 / total * 100, 1),
        "pct_above_200dma": round(above200 / total * 100, 1),
        "pct_up_today": round(up / total * 100, 1),
    }


def vix_context() -> dict:
    df = data.get_daily("^VIX", period="2y")
    close = df["Close"]
    v = float(close.iloc[-1])
    pct_rank = float((close < v).mean() * 100)
    out = {"value": round(v, 1), "percentile_2y": round(pct_rank, 0)}
    try:
        # 期限结构：VIX > 3月期VIX(倒挂) = 恐慌高潮的典型特征，比VIX绝对值更可靠的见底信号
        v3 = float(data.get_daily("^VIX3M", period="6mo")["Close"].iloc[-1])
        out["vix3m"] = round(v3, 1)
        out["term_ratio"] = round(v / v3, 3)
        out["backwardation"] = v / v3 >= 1.0
    except Exception:
        pass
    return out


def regime(spy_analysis: dict, vix: dict, br: dict) -> dict:
    """市场状态：多头/震荡/空头/恐慌。规则透明，报告里口径每天一致。"""
    ma = spy_analysis["ma"]
    above50 = ma.get(50, {}).get("above", False)
    above200 = ma.get(200, {}).get("above", False)
    v = vix.get("value", 20)

    if v >= 28:
        label, desc = "恐慌", "VIX 高于28，市场处于恐慌状态——正是你的策略需要盯紧的时候"
    elif above50 and above200 and v < 20:
        label, desc = "多头", "标普在50日和200日均线之上，波动率低，顺势环境"
    elif not above200:
        label, desc = "空头", "标普在200日均线之下，趋势偏空，反弹按反弹对待"
    else:
        label, desc = "震荡", "趋势信号混杂，区间思路，重点看关键位的争夺"
    return {"label": label, "desc": desc}


def market_dip_gauge(spy_df_close, vix: dict, br: dict) -> dict:
    """大盘级抄底温度计 0-100。恐慌越重、宽度越差、超跌越深，分数越高。"""
    score = 0.0
    reasons = []
    v, pct = vix["value"], vix["percentile_2y"]
    if pct >= 90:
        score += 35; reasons.append(f"VIX {v} 处于2年 {pct:.0f}% 分位，极端恐慌")
    elif pct >= 75:
        score += 22; reasons.append(f"VIX {v} 处于2年 {pct:.0f}% 分位")
    elif pct >= 60:
        score += 10

    r = float(rsi(spy_df_close).iloc[-1])
    if r < 30:
        score += 25; reasons.append(f"标普 RSI {r:.0f} 超卖")
    elif r < 40:
        score += 12; reasons.append(f"标普 RSI {r:.0f} 偏弱")

    b50 = br.get("pct_above_50dma", 50)
    if b50 < 20:
        score += 25; reasons.append(f"仅 {b50}% 的大盘股在50日均线上方，宽度崩塌(历史上常见于底部区域)")
    elif b50 < 35:
        score += 14; reasons.append(f"{b50}% 的大盘股在50日均线上方，宽度偏弱")

    ma20 = float(spy_df_close.rolling(20).mean().iloc[-1])
    p = float(spy_df_close.iloc[-1])
    if (p - ma20) / ma20 < -0.04:
        score += 15; reasons.append("标普偏离20日均线超过4%")

    if vix.get("backwardation"):
        score += 15
        reasons.append(
            f"VIX期限结构倒挂（{vix['value']} > 3月期 {vix['vix3m']}）——恐慌高潮的典型特征，历史上常出现在底部区域"
        )

    score = min(100.0, score)
    if score >= 65:
        band = "大盘级抄底窗口"
    elif score >= 40:
        band = "开始积累机会，分批观察"
    else:
        band = "不是恐慌底，别急"
    return {"score": round(score, 0), "band": band, "reasons": reasons}


def leverage_light(spy_a: dict, vix: dict, dip_g: dict) -> dict:
    """杠杆红绿灯（市场级）。规则透明：
    红 = 趋势和波动同时转坏，杠杆降为零；绿 = 恐慌+位置到位（抄底策略的杠杆唯一窗口）；黄 = 其余一切。
    个股级卸杠杆位 = 各票交易计划的无效位；财报前 3 个交易日内不留杠杆过夜。
    """
    ma = spy_a["ma"]
    above50 = ma.get(50, {}).get("above", True)
    above200 = ma.get(200, {}).get("above", True)
    v = vix.get("value", 20)

    if (not above50 and v >= 22) or not above200:
        return {
            "light": "红",
            "desc": "标普跌破关键均线且波动放大：杠杆先降为零，用现金等抄底窗口——趋势坏境里杠杆多头是给市场送钱",
        }
    if dip_g["score"] >= 65:
        return {
            "light": "绿",
            "desc": "恐慌+位置到位（抄底温度计≥65）：这是你策略里唯一适合有限杠杆的窗口；卸载位=各票无效位，破位即降杠杆，不商量",
        }
    return {
        "light": "黄",
        "desc": "正常环境：持有可以，但不是上杠杆的时机——杠杆只留给恐慌底，平时的杠杆是在为回撤付利息",
    }


def overview(session: str = "premarket") -> dict:
    """session: premarket | postmarket"""
    quotes = []
    for t, name in INDEX_TICKERS + OVERNIGHT_TICKERS:
        q = data.get_quote(t)
        if q:
            quotes.append({"ticker": t, "name": name, **{k: (round(x, 2) if isinstance(x, float) else x) for k, x in q.items()}})

    spy_df = data.get_daily("SPY", period="2y")
    qqq_df = data.get_daily("QQQ", period="2y")

    def index_levels(df):
        price = float(df["Close"].iloc[-1])
        zones, tol = build_zones(df)
        sup, res, inside = split_zones(zones, price)
        return {
            "price": round(price, 2),
            "supports": [z.to_dict(price) for z in sup[:3]],
            "resistances": [z.to_dict(price) for z in res[:3]],
            "inside_zone": inside.to_dict(price) if inside else None,
            "events": detect_events(df, zones, tol),
        }

    vix = vix_context()
    br = breadth()

    from .stock import analyze as stock_analyze  # 避免循环导入
    # SPY 只用于均线/状态判定，不取空头数据（ETF 的 SI 没有意义，还烧 Ortex credits）
    spy_a = stock_analyze("SPY", with_news=False, with_short=False)
    dip_g = market_dip_gauge(spy_df["Close"], vix, br)

    return {
        "session": session,
        "quotes": quotes,
        "vix": vix,
        "breadth": br,
        "regime": regime(spy_a, vix, br),
        "dip_gauge": dip_g,
        "leverage": leverage_light(spy_a, vix, dip_g),
        "spy": index_levels(spy_df),
        "qqq": index_levels(qqq_df),
        "market_news": data.get_news("SPY", limit=8),
    }
