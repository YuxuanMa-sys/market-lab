"""Polygon.io：日线/快照/新闻(含AI情绪标注)。当前 key 为 Developer 档：15分钟延迟、不限请求数。

Polygon 只有股票/ETF；指数(^VIX)和期货(ES=F)不在这里，路由层会把它们留给 yfinance。
"""
from __future__ import annotations

import os
from datetime import date, timedelta

import pandas as pd
import requests

BASE = "https://api.polygon.io"

_PERIOD_DAYS = {"6mo": 183, "1y": 365, "2y": 730, "5y": 1825}


def _get(path: str, **params) -> dict:
    params["apiKey"] = os.environ["POLYGON_API_KEY"]
    r = requests.get(f"{BASE}{path}", params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def _sym(ticker: str) -> str:
    # yfinance 用 BRK-B，Polygon 用 BRK.B
    return ticker.replace("-", ".")


def get_daily(ticker: str, period: str = "2y") -> pd.DataFrame:
    days = _PERIOD_DAYS.get(period, 730)
    start = (date.today() - timedelta(days=days)).isoformat()
    end = date.today().isoformat()
    j = _get(
        f"/v2/aggs/ticker/{_sym(ticker)}/range/1/day/{start}/{end}",
        adjusted="true", sort="asc", limit=50000,
    )
    results = j.get("results")
    if not results:
        raise ValueError(f"Polygon 没有返回 {ticker} 的数据")
    df = pd.DataFrame(results)
    df.index = pd.to_datetime(df["t"], unit="ms").dt.normalize()
    df = df.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
    return df[["Open", "High", "Low", "Close", "Volume"]]


def get_quote(ticker: str) -> dict:
    j = _get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{_sym(ticker)}")
    t = j.get("ticker", {})
    last = (t.get("lastTrade") or {}).get("p")
    prev = (t.get("prevDay") or {}).get("c")
    if last is None or not prev:
        return {}
    return {
        "last": float(last),
        "prev_close": float(prev),
        "chg_pct": (float(last) / float(prev) - 1) * 100,
    }


def get_minute_profile(ticker: str, days: int = 60, bins: int = 120, top_n: int = 6) -> list | None:
    """分钟级 volume-at-price：比日线典型价近似准得多的成交密集区。

    近60个交易日的1分钟K线，按半衰期30天加权聚合到价格轴，取局部峰值。
    返回 [(price, weight, label), ...] 供 levels 引擎作为候选位（替代日线版粗算）。
    """
    import numpy as np

    sym = _sym(ticker)
    end = date.today()
    start = (end - timedelta(days=int(days * 1.6))).isoformat()
    prices, vols, ages = [], [], []
    j = _get(
        f"/v2/aggs/ticker/{sym}/range/1/minute/{start}/{end.isoformat()}",
        adjusted="true", sort="asc", limit=50000,
    )
    now_ms = None
    for _ in range(4):  # 最多4页(~20万根)防失控
        for r in j.get("results", []):
            p = r.get("vw") or r.get("c")
            v = r.get("v") or 0
            if p and v:
                prices.append(float(p))
                vols.append(float(v))
                ages.append(r["t"])
        nxt = j.get("next_url")
        if not nxt:
            break
        rr = requests.get(nxt, params={"apiKey": os.environ["POLYGON_API_KEY"]}, timeout=25)
        rr.raise_for_status()
        j = rr.json()
    if len(prices) < 2000:
        return None
    prices = np.array(prices)
    vols = np.array(vols)
    now_ms = max(ages)
    age_days = (now_ms - np.array(ages)) / 86400000.0
    w = vols * 0.5 ** (age_days / 30.0)  # 半衰期30天：越新的成交越有定价权
    lo, hi = np.percentile(prices, 1), np.percentile(prices, 99)
    if hi <= lo:
        return None
    hist, edges = np.histogram(prices, bins=bins, range=(lo, hi), weights=w)
    centers = (edges[:-1] + edges[1:]) / 2
    mean_v = hist.mean()
    peaks = [
        (float(centers[i]), float(hist[i]))
        for i in range(1, bins - 1)
        if hist[i] > hist[i - 1] and hist[i] >= hist[i + 1] and hist[i] > 1.3 * mean_v
    ]
    peaks.sort(key=lambda x: -x[1])
    top = peaks[:top_n]
    if not top:
        return None
    vmax = top[0][1]
    return [(p, 0.9 + 1.3 * v / vmax, "成交密集区·分钟级") for p, v in top]


def get_short_stats(ticker: str) -> dict | None:
    """空头数据（含在股票套餐里，不限调用）：官方双周 SI + 回补天数 + 每日做空量占比。

    与 Ortex 的差别：SI 是交易所官方数据(滞后~2周)而非日频估计；口径是占总股本%而非流通盘%；
    没有借券费率(CTB)——那是 Ortex 仅存的独家价值，由 data 层按需增强。
    """
    sym = _sym(ticker)
    today = date.today()
    j = _get(
        "/stocks/v1/short-interest", ticker=sym, limit=10,
        **{"settlement_date.gte": (today - timedelta(days=75)).isoformat()},
    )
    rows = sorted(j.get("results", []), key=lambda r: r.get("settlement_date", ""))
    if not rows:
        return None
    last = rows[-1]
    prev = rows[-2] if len(rows) > 1 else None
    si = last.get("short_interest")

    si_pct = None
    try:
        ov = _get(f"/v3/reference/tickers/{sym}").get("results", {})
        shares = ov.get("share_class_shares_outstanding") or ov.get("weighted_shares_outstanding")
        if si and shares:
            si_pct = round(si / shares * 100, 2)
    except Exception:
        pass

    si_change = None
    if prev and prev.get("short_interest"):
        si_change = round((si - prev["short_interest"]) / prev["short_interest"] * 100, 1)

    svr_last = svr_5d = None
    try:
        sv = _get(
            "/stocks/v1/short-volume", ticker=sym, limit=10,
            **{"date.gte": (today - timedelta(days=12)).isoformat()},
        )
        svr = sorted(sv.get("results", []), key=lambda r: r.get("date", ""))
        if svr:
            svr_last = round(svr[-1]["short_volume_ratio"], 1)
            tail = svr[-5:]
            svr_5d = round(sum(r["short_volume_ratio"] for r in tail) / len(tail), 1)
    except Exception:
        pass

    return {
        "si_pct_out": si_pct,                 # SI 占总股本%（官方口径，注意不是流通盘%）
        "si_shares": si,
        "si_change_pct": si_change,           # 相对上一期(两周前)的变化%
        "dtc": last.get("days_to_cover"),
        "settlement_date": last.get("settlement_date"),
        "svr_last": svr_last,                 # 最新交易日做空量占总成交量%
        "svr_5d": svr_5d,                     # 5日均做空量占比
        "source": "polygon",
    }


def get_option_walls(ticker: str, price: float, days: int = 45, top_n: int = 3) -> dict | None:
    """期权持仓墙：近45天到期、现价±25%内，按行权价聚合持仓量(OI)。

    现价上方的大 call OI = 压力磁吸位，下方的大 put OI = 支撑位。
    只对期权链活跃的票有意义，链太小时返回 None。
    """
    today = date.today()
    params = {
        "strike_price.gte": round(price * 0.78, 2),
        "strike_price.lte": round(price * 1.28, 2),
        "expiration_date.gte": today.isoformat(),
        "expiration_date.lte": (today + timedelta(days=days)).isoformat(),
        "limit": 250,
    }
    oi_call: dict[float, int] = {}
    oi_put: dict[float, int] = {}
    j = _get(f"/v3/snapshot/options/{_sym(ticker)}", **params)
    for _ in range(8):  # 分页上限，防失控
        for c in j.get("results", []):
            det = c.get("details", {})
            k, oi = det.get("strike_price"), c.get("open_interest") or 0
            if k is None or not oi:
                continue
            side = oi_call if det.get("contract_type") == "call" else oi_put
            side[float(k)] = side.get(float(k), 0) + int(oi)
        nxt = j.get("next_url")
        if not nxt:
            break
        r = requests.get(nxt, params={"apiKey": os.environ["POLYGON_API_KEY"]}, timeout=20)
        r.raise_for_status()
        j = r.json()
    total_oi = sum(oi_call.values()) + sum(oi_put.values())
    if total_oi < 2000:  # 链太薄，墙没有参考意义
        return None
    calls = sorted(((k, v) for k, v in oi_call.items() if k >= price), key=lambda x: -x[1])[:top_n]
    puts = sorted(((k, v) for k, v in oi_put.items() if k <= price), key=lambda x: -x[1])[:top_n]
    return {
        "call_walls": [{"strike": k, "oi": v} for k, v in sorted(calls)],
        "put_walls": [{"strike": k, "oi": v} for k, v in sorted(puts)],
        "total_oi": total_oi,
        "window_days": days,
    }


_SENTIMENT_ZH = {"positive": "利好", "negative": "利空", "neutral": "中性"}


def get_news(ticker: str, limit: int = 6) -> list[dict]:
    j = _get("/v2/reference/news", ticker=_sym(ticker), limit=limit)
    out = []
    for it in j.get("results", []):
        sentiment = ""
        reasoning = ""
        for ins in it.get("insights", []):
            if ins.get("ticker") == _sym(ticker):
                sentiment = _SENTIMENT_ZH.get(ins.get("sentiment", ""), "")
                reasoning = ins.get("sentiment_reasoning", "")
                break
        out.append({
            "title": it.get("title", ""),
            "url": it.get("article_url", ""),
            "source": (it.get("publisher") or {}).get("name", ""),
            "time": it.get("published_utc", ""),
            "sentiment": sentiment,
            "sentiment_reasoning": reasoning,
        })
    return out
