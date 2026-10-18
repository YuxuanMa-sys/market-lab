"""个股分析：位置图谱 + 站稳/破位状态 + 抄底评分 + 高位分。"""
from __future__ import annotations

import pandas as pd

from . import data
from .indicators import atr, rsi, sma
from .levels import build_zones, detect_events, split_zones
from .plan import trade_plan


def dip_score(df: pd.DataFrame, supports: list, tol: float) -> dict:
    """抄底评分 0-100：分数高 = 跌得更深（RSI 超卖、低于 20 日线、20 日回撤、恐慌放量；另含"贴近强支撑"项）。

    2026-10-17 无偏全市场检验（H1 拟合 / H2 样本外，21 日块 bootstrap）：
    - 8 个改版、5 个合成分、39–55 个候选因子都没有在 H2 校正后稳定胜过它，所以公式不改。
    - 但它本身没有挑票能力：按日等权，dip≥55 对同日随机股 H2 −0.13pp [−0.49,+0.22]；
      按事件加权的正超额完全来自崩盘日（那几天 ≥55 的票特别多）。
    - 旧注释里"55-69 胜率 52.5%、+2.85%/笔"来自有偏的 52 只自选池，无偏样本上复现不出来。
    - 只描述"跌得多深"，不代表更值得买。

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
        band = "深度低位(历史上不比55-69更好)"
    elif score >= 55:
        band = "低位"
    elif score >= 45:
        band = "接近低位"
    else:
        band = "不在低位"
    return {"score": round(score, 0), "band": band, "reasons": reasons}


HIGH_MIN_BARS = 20      # MA20 / 20 日最低 / 20 日均量需要 20 根
HIGH_52W_BARS = 252     # 52 周高项需要 252 根；不足 → 该项 0 分（研究主口径，= hs.pkl 的 m_pts_52h / mirror_v0）


def high_band(score: float) -> str:
    if score >= 70:
        return "深度高位(历史上不比55-69更容易回落)"
    if score >= 55:
        return "高位"
    if score >= 45:
        return "接近高位"
    return "不在高位"


def high_score(df: pd.DataFrame) -> dict:
    """高位分 0-100：分数高 = 最近涨得更猛（RSI 超买、高于 20 日线、20 日涨幅、放量上涨、贴近 52 周高）。
    dip_score 的镜像，用户用它对自己的持仓判断"涨得够不够猛"，卖不卖自己定。

    只描述"涨得多猛"，不预测会跌，也不是卖出信号。2026-10-18 无偏全市场检验（H1 设计 / H2 样本外，含退市；
    6281 只票 × 5 年；4 路对抗复核；完整规格见研究存档 hs_spec.md）：
    - ≥55 之后 10/20/60 日相对同日随机股：H2 按日 −0.4/−0.5/−1.3pp，区间全部含 0；按事件 +0.1/+0.2/+0.3pp。
    - ≥70 不比 55–69 更差；10 天内回撤 1 个 ATR 的概率两半样本方向相反（不能写"更容易回落"）。
    - 抄底买进后第一次到 ≥55，继续拿 10/20 天相对大盘 ≈0；"一到高位就卖"也不比同样持有时长的随机时点卖更好。
    - 公式是事先登记的镜像版（mirror_v0），没有按收益调参；必须喂已收盘的日线（盘中当日成交量不全，量比会失真）。
    """
    # 同一代码换过证券（例：SPCX 2026-06-12 之前是另一只 ~$22 的 ETF）：只用拼接点之后的真实历史，
    # 与研究面板的拼接截断同口径；否则 20 日涨幅会从旧证券的低点量起（实测 606%，误触发⚠附注）。
    from .hygiene import find_splices
    spl = find_splices(df) if len(df) > 1 else []
    if spl:
        df = df.loc[pd.Timestamp(spl[-1]["after"]):]
    n = len(df)
    if n < HIGH_MIN_BARS:
        return {"score": None, "band": "数据不足", "reasons": [f"只有 {n} 根日线，至少需要 {HIGH_MIN_BARS} 根"],
                "facts": {"n_bars": n}}
    close = df["Close"]
    price = float(close.iloc[-1])
    a = float(atr(df).iloc[-1])
    r = float(rsi(close).iloc[-1])
    ma20 = float(sma(close, 20).iloc[-1])
    low20 = float(df["Low"].tail(20).min())
    vol_ratio = float(df["Volume"].iloc[-1] / df["Volume"].tail(20).mean())

    score = 0
    reasons = []

    if r > 75:
        score += 30; reasons.append(f"RSI {r:.0f} 强超买")
    elif r > 68:
        score += 22; reasons.append(f"RSI {r:.0f} 超买")
    elif r > 60:
        score += 10; reasons.append(f"RSI {r:.0f} 偏强")

    dev = (price - ma20) / a if a else 0  # 高于 20 日均线多少个 ATR
    if dev > 3:
        score += 25; reasons.append(f"高于20日均线 {dev:.1f} 个ATR，大幅偏离")
    elif dev > 2:
        score += 18; reasons.append(f"高于20日均线 {dev:.1f} 个ATR")
    elif dev > 1:
        score += 8; reasons.append(f"高于20日均线 {dev:.1f} 个ATR")

    run = price / low20 - 1  # 从近 20 根（含今天）最低价算起的涨幅
    if run > 0.15:
        score += 15; reasons.append(f"近20日从低点涨了 {run * 100:.0f}%")
    elif run > 0.08:
        score += 10; reasons.append(f"近20日从低点涨了 {run * 100:.0f}%")

    if vol_ratio > 1.8 and price > float(close.iloc[-2]):
        score += 10; reasons.append(f"放量上涨(量比{vol_ratio:.1f})")

    d52 = None
    if n >= HIGH_52W_BARS:
        hi52 = float(df["High"].tail(HIGH_52W_BARS).max())
        d52 = price / hi52 - 1
        if d52 >= -0.02:
            score += 20; reasons.append(f"距52周高 {d52 * 100:+.1f}%，贴近一年高点")
        elif d52 >= -0.05:
            score += 10; reasons.append(f"距52周高 {d52 * 100:+.1f}%")
    else:
        reasons.append(f"上市不足一年（{n} 根日线），未计52周高项")

    score = min(100, score)
    atr_pct = a / price if price else float("nan")
    facts = {
        "n_bars": n, "rsi14": round(r, 1), "dev_atr": round(dev, 2), "runup20": round(run, 4),
        "vol_ratio": round(vol_ratio, 2), "dist52h": None if d52 is None else round(d52, 4),
        "atr_pct": atr_pct,          # 不取整：高波动句的门槛是 ≥4%，取整会把 3.997% 算进去
        # 实验性附注的触发条件（不计分）：近 20 日从低点翻倍且 ATR14/收盘 ≥10%
        "extreme_runup": bool(run >= 1.0 and atr_pct >= 0.10),
    }
    return {"score": score, "band": high_band(score), "reasons": reasons, "facts": facts}


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
        "high": high_score(df),
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
