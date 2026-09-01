"""个股分析：位置图谱 + 站稳/破位状态 + 抄底评分。"""
from __future__ import annotations

import pandas as pd

from . import data
from .indicators import atr, rsi, sma
from .levels import build_zones, detect_events, split_zones
from .plan import trade_plan


def dip_score(df: pd.DataFrame, supports: list, tol: float) -> dict:
    """抄底评分 0-100：分数高 = 更接近"恐慌+靠近强支撑"的抄底条件。

    这不是买入信号，是把"这跌得够不够狠、位置好不好"量化出来。
    """
    close = df["Close"]
    price = float(close.iloc[-1])
    a = float(atr(df).iloc[-1])
    r = float(rsi(close).iloc[-1])
    ma20 = float(sma(close, 20).iloc[-1])
    high20 = float(df["High"].tail(20).max())
    vol_ratio = float(df["Volume"].iloc[-1] / df["Volume"].tail(20).mean())

    score = 0.0
    reasons = []

    if r < 25:
        score += 30; reasons.append(f"RSI {r:.0f} 深度超卖")
    elif r < 32:
        score += 22; reasons.append(f"RSI {r:.0f} 超卖")
    elif r < 40:
        score += 10; reasons.append(f"RSI {r:.0f} 偏弱")

    dev = (price - ma20) / a if a else 0  # 偏离20日均线多少个ATR
    if dev < -3:
        score += 25; reasons.append(f"低于20日均线 {abs(dev):.1f} 个ATR，超跌")
    elif dev < -2:
        score += 18; reasons.append(f"低于20日均线 {abs(dev):.1f} 个ATR")
    elif dev < -1:
        score += 8

    dd = (price / high20 - 1) * 100
    if dd < -15:
        score += 15; reasons.append(f"20日内回撤 {dd:.0f}%")
    elif dd < -8:
        score += 10; reasons.append(f"20日内回撤 {dd:.0f}%")

    strong = [z for z in supports if z.score >= 4.0]
    if strong:
        near = strong[0]
        gap_atr = (price - near.high) / a if a else 99
        if gap_atr <= 1.0:
            score += 20; reasons.append(f"贴近强支撑区 {near.low:.2f}-{near.high:.2f}")
        elif gap_atr <= 2.0:
            score += 10; reasons.append(f"距强支撑区 {near.low:.2f}-{near.high:.2f} 约{gap_atr:.1f}个ATR")

    if vol_ratio > 1.8 and float(close.iloc[-1]) < float(close.iloc[-2]):
        score += 10; reasons.append(f"放量下跌(量比{vol_ratio:.1f})，可能是恐慌抛售")

    score = min(100.0, score)
    if score >= 70:
        band = "深度恐慌区(回测:并不比55-69更优,别加倍)"
    elif score >= 55:
        band = "达进场线(回测胜率52.5%)"
    elif score >= 45:
        band = "接近(回测该段胜率仅38%,别动手)"
    else:
        band = "不在抄底区"
    return {"score": round(score, 0), "band": band, "reasons": reasons}


def analyze(ticker: str, with_news: bool = True, with_short: bool = True) -> dict:
    df = data.get_daily(ticker, period="2y")
    close = df["Close"]
    price = float(close.iloc[-1])
    prev = float(close.iloc[-2])
    a = float(atr(df).iloc[-1])
    r = float(rsi(close).iloc[-1])

    walls = data.get_option_walls(ticker, price)
    extra = []
    if walls:
        max_oi = max((w["oi"] for w in walls["call_walls"] + walls["put_walls"]), default=1)
        for w in walls["put_walls"] + walls["call_walls"]:
            extra.append((float(w["strike"]), 0.8 + 1.2 * w["oi"] / max_oi, "期权持仓墙"))
    # 分钟级密集区(2026-09-01裁定)：不参与区间计分——它带强新近性偏好，与被回测
    # 否决的"成交量半衰期"同一性质且无法walk-forward验证；仅作展示参考(minute_profile字段)
    minprof = data.get_minute_profile_candidates(ticker)

    zones, tol = build_zones(df, extra=extra)
    supports, resistances, inside = split_zones(zones, price)
    events = detect_events(df, zones, tol)

    ma = {}
    for n in (20, 50, 200):
        if len(df) >= n:
            v = float(sma(close, n).iloc[-1])
            ma[n] = {"value": round(v, 2), "above": price > v}

    if ma.get(200) and ma.get(50):
        if price > ma[50]["value"] > ma[200]["value"]:
            trend = "多头排列"
        elif price < ma[50]["value"] < ma[200]["value"]:
            trend = "空头排列"
        else:
            trend = "震荡"
    else:
        trend = "数据不足"

    # 支撑链：跌破第一档看第二档，依次给出
    support_chain = []
    for i, z in enumerate(supports[:4]):
        nxt = supports[i + 1] if i + 1 < len(supports) else None
        item = z.to_dict(price)
        item["if_break_next"] = nxt.to_dict(price) if nxt else None
        support_chain.append(item)

    res_chain = [z.to_dict(price) for z in resistances[:4]]

    # 趋势模式(trend)用的跟踪字段：移动止损 = 60日最高点回落2.75ATR；连续低于50日线天数
    trail_stop = round(float(df["High"].tail(60).max()) - 2.75 * a, 2)
    below_streak = 0
    if len(df) >= 50:
        cond = (close < sma(close, 50)).values
        for v in cond[::-1]:
            if v:
                below_streak += 1
            else:
                break

    out = {
        "ticker": ticker,
        "price": round(price, 2),
        "trail_stop": trail_stop,
        "below_ma50_streak": int(below_streak),
        "chg_pct": round((price / prev - 1) * 100, 2),
        "atr": round(a, 2),
        "rsi": round(r, 1),
        "trend": trend,
        "ma": ma,
        "vol_ratio": round(float(df["Volume"].iloc[-1] / df["Volume"].tail(20).mean()), 2),
        "high_52w": round(float(df.tail(252)["High"].max()), 2),
        "low_52w": round(float(df.tail(252)["Low"].min()), 2),
        "inside_zone": inside.to_dict(price) if inside else None,
        "supports": support_chain,
        "resistances": res_chain,
        "events": events,
        "dip": dip_score(df, supports, tol),
    }
    if walls:
        out["option_walls"] = walls
    if minprof:
        out["minute_profile"] = [{"price": round(p, 2), "weight": round(w, 2)} for p, w, _ in minprof]
    out["plan"] = trade_plan(out)
    if with_short:
        short = data.get_short_stats(ticker)
        if short:
            out["short"] = short
    if with_news:
        out["news"] = data.get_news(ticker)
    return out
