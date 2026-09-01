"""位置质量回测：引擎改动的裁判。

逐步向前走(无未来函数)，每10个交易日用截至当日的数据重建区间，取现价下方10%以内的
支撑区，观察其后45日内的第一次测试：
  守住 = 价格触区后先反弹≥1ATR；击穿 = 收盘跌破区下沿0.5ATR。
按强度分数分桶统计守住率——好的引擎应该让"分数越高守住率越高"且整体守住率提升。
每次引擎改动都跑一遍，与基线对比，不提升则回滚。
"""
from __future__ import annotations

from . import data
from .indicators import atr
from .levels import build_zones, split_zones

STEP = 10
HORIZON = 45
NEAR_PCT = 0.10
MIN_BARS = 320

BUCKETS = [(2, 4), (4, 7), (7, 12), (12, 999)]


def run_ticker(ticker: str, period: str = "5y") -> list[dict]:
    df = data.get_daily(ticker, period)
    if len(df) < MIN_BARS:
        return []
    close = df["Close"].values
    high = df["High"].values
    low = df["Low"].values
    atr_s = atr(df).values
    samples = []
    for i in range(300, len(df) - 5, STEP):
        window = df.iloc[: i + 1]
        price = float(close[i])
        a = float(atr_s[i]) or 1e-9
        try:
            zones, _ = build_zones(window)
        except Exception:
            continue
        sup, _, _ = split_zones(zones, price)
        for z in sup:
            if (price - z.mid) / price > NEAR_PCT:
                continue
            end = min(i + 1 + HORIZON, len(df))
            for j in range(i + 1, end):
                if low[j] <= z.high:  # 第一次测试
                    entry = min(float(close[j]), z.high)
                    held = None
                    for k in range(j, min(j + HORIZON, len(df))):
                        if float(close[k]) < z.low - 0.5 * a:
                            held = False
                            break
                        if float(high[k]) >= entry + 1.0 * a:
                            held = True
                            break
                    if held is not None:
                        samples.append({"ticker": ticker, "score": round(z.score, 1), "held": held})
                    break
    return samples


def aggregate(samples: list[dict]) -> dict:
    def stats(rows):
        if not rows:
            return {"n": 0}
        return {"n": len(rows), "hold_rate": round(sum(r["held"] for r in rows) / len(rows) * 100, 1)}

    out = {"overall": stats(samples), "by_score": {}}
    for lo, hi in BUCKETS:
        out["by_score"][f"{lo}-{hi if hi < 999 else '+'}"] = stats(
            [s for s in samples if lo <= s["score"] < hi]
        )
    return out


def run(tickers: list[str], period: str = "5y") -> dict:
    samples = []
    failed = []
    for t in tickers:
        try:
            samples.extend(run_ticker(t, period))
        except Exception:
            failed.append(t)
    return {"n_tickers": len(tickers) - len(failed), "failed": failed, **aggregate(samples)}
