"""支撑/压力位引擎。

多个独立方法各自产出候选位置（摆动高低点、成交量密集区、均线、缺口、
前高前低、整数关口），再把价格相近的候选聚成"区间"，重叠越多评分越高。
输出的是带强度评分的价格区间，不是单一价格。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .indicators import atr, sma

LOOKBACK = 250


@dataclass
class Zone:
    low: float
    high: float
    score: float
    sources: list[str] = field(default_factory=list)
    touches: int = 0

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2

    def to_dict(self, price: float) -> dict:
        return {
            "low": round(self.low, 2),
            "high": round(self.high, 2),
            "mid": round(self.mid, 2),
            "score": round(self.score, 1),
            "touches": self.touches,
            "sources": self.sources,
            "dist_pct": round((self.mid / price - 1) * 100, 2),
        }


def _swing_candidates(df: pd.DataFrame, k: int = 4) -> list[tuple[float, float, str]]:
    h, l = df["High"].values, df["Low"].values
    n = len(df)
    out = []
    for i in range(k, n - k):
        recency = 0.5 + 0.8 * (i / n)  # 越新的摆动点权重越高
        if h[i] == max(h[i - k : i + k + 1]):
            out.append((float(h[i]), 1.0 * recency, "摆动高点"))
        if l[i] == min(l[i - k : i + k + 1]):
            out.append((float(l[i]), 1.0 * recency, "摆动低点"))
    return out


def _volume_profile_candidates(df: pd.DataFrame, bins: int = 40) -> list[tuple[float, float, str]]:
    lo, hi = df["Low"].min(), df["High"].max()
    if hi <= lo:
        return []
    typical = (df["High"] + df["Low"] + df["Close"]) / 3
    vol, edges = np.histogram(typical, bins=bins, range=(lo, hi), weights=df["Volume"])
    centers = (edges[:-1] + edges[1:]) / 2
    mean_v = vol.mean()
    out = []
    for i in range(1, bins - 1):
        if vol[i] > vol[i - 1] and vol[i] >= vol[i + 1] and vol[i] > 1.3 * mean_v:
            w = 0.8 + 1.2 * (vol[i] / vol.max())
            out.append((float(centers[i]), float(w), "成交密集区"))
    return out


def _ma_candidates(df: pd.DataFrame) -> list[tuple[float, float, str]]:
    out = []
    for n, w in [(20, 1.5), (50, 2.5), (200, 3.5)]:
        if len(df) >= n:
            v = float(sma(df["Close"], n).iloc[-1])
            if not math.isnan(v):
                out.append((v, w, f"{n}日均线"))
    year = df.tail(252)
    out.append((float(year["High"].max()), 2.5, "52周高点"))
    out.append((float(year["Low"].min()), 2.5, "52周低点"))
    if len(df) >= 2:
        out.append((float(df["High"].iloc[-2]), 0.8, "昨日高点"))
        out.append((float(df["Low"].iloc[-2]), 0.8, "昨日低点"))
    return out


def _gap_candidates(df: pd.DataFrame, lookback: int = 120) -> list[tuple[float, float, str]]:
    d = df.tail(lookback)
    h, l = d["High"].values, d["Low"].values
    out = []
    for i in range(1, len(d)):
        if l[i] > h[i - 1] * 1.002:  # 向上跳空
            if i == len(d) - 1 or l[i + 1 :].min() > h[i - 1]:  # 未回补
                out.append((float((h[i - 1] + l[i]) / 2), 1.5, "未回补缺口"))
        if h[i] < l[i - 1] * 0.998:  # 向下跳空
            if i == len(d) - 1 or h[i + 1 :].max() < l[i - 1]:
                out.append((float((l[i - 1] + h[i]) / 2), 1.5, "未回补缺口"))
    return out


def _round_number_candidates(price: float) -> list[tuple[float, float, str]]:
    if price < 20:
        grid = 1
    elif price < 100:
        grid = 5
    elif price < 250:
        grid = 10
    elif price < 500:
        grid = 25
    elif price < 1000:
        grid = 50
    else:
        grid = 100
    lo, hi = price * 0.85, price * 1.15
    out = []
    x = math.floor(lo / grid) * grid
    while x <= hi:
        if x > 0:
            out.append((float(x), 0.4, "整数关口"))
        x += grid
    return out


def _count_touches(df: pd.DataFrame, z_lo: float, z_hi: float) -> int:
    d = df.tail(LOOKBACK)
    inter = (d["Low"] <= z_hi) & (d["High"] >= z_lo)
    return int(inter.sum())


def build_zones(df: pd.DataFrame, extra: list[tuple[float, float, str]] | None = None) -> tuple[list[Zone], float]:
    """返回 (按价格排序的 zones, 聚类容差 tol)。extra 为外部候选位，如期权持仓墙。"""
    d = df.tail(LOOKBACK)
    price = float(d["Close"].iloc[-1])
    a = float(atr(d).iloc[-1])
    tol = max(0.4 * a, 0.0035 * price)

    cands: list[tuple[float, float, str]] = []
    cands += _swing_candidates(d)
    cands += _volume_profile_candidates(d)
    cands += _ma_candidates(df)
    cands += _gap_candidates(df)
    cands += _round_number_candidates(price)
    if extra:
        cands += extra
    cands = [c for c in cands if 0.6 * price <= c[0] <= 1.5 * price]
    cands.sort(key=lambda c: c[0])

    zones: list[Zone] = []
    cluster: list[tuple[float, float, str]] = []

    def flush():
        if not cluster:
            return
        total_w = sum(w for _, w, _ in cluster)
        wmean = sum(p * w for p, w, _ in cluster) / total_w
        lo = min(p for p, _, _ in cluster)
        hi = max(p for p, _, _ in cluster)
        pad = 0.15 * a
        z = Zone(low=lo - pad, high=hi + pad, score=total_w)
        # 来源去重并按类型汇总，如 "摆动低点×3"
        srcs: dict[str, int] = {}
        for _, _, s in cluster:
            srcs[s] = srcs.get(s, 0) + 1
        z.sources = [f"{s}×{n}" if n > 1 else s for s, n in sorted(srcs.items(), key=lambda kv: -kv[1])]
        z.touches = _count_touches(df, z.low, z.high)
        z.score += 0.25 * math.sqrt(z.touches)
        zones.append(z)

    for c in cands:
        if not cluster:
            cluster = [c]
            continue
        wmean = sum(p * w for p, w, _ in cluster) / sum(w for _, w, _ in cluster)
        if c[0] - wmean <= tol:
            cluster.append(c)
        else:
            flush()
            cluster = [c]
    flush()

    zones = [z for z in zones if z.score >= 2.0]
    zones.sort(key=lambda z: z.mid)
    return zones, tol


def split_zones(zones: list[Zone], price: float) -> tuple[list[Zone], list[Zone], Zone | None]:
    """按当前价拆成 (支撑列表-由近到远, 压力列表-由近到远, 当前所在区间)。"""
    supports = [z for z in zones if z.high < price]
    resistances = [z for z in zones if z.low > price]
    inside = next((z for z in zones if z.low <= price <= z.high), None)
    supports.sort(key=lambda z: -z.mid)
    resistances.sort(key=lambda z: z.mid)
    return supports, resistances, inside


def detect_events(df: pd.DataFrame, zones: list[Zone], tol: float) -> list[dict]:
    """检测最近的破位/站上事件，并给出下一档位置。

    有效跌破：最近3根K线内收盘从区间上方跌到区间下方，且(放量 或 次日继续收在下方)。
    站稳：突破区间上沿后，最近连续2根收盘都在上沿之上。
    """
    closes = df["Close"].values
    vols = df["Volume"].values
    avg_vol = float(df["Volume"].tail(20).mean())
    price = float(closes[-1])
    events = []
    n = len(closes)
    for z in zones:
        if abs(z.mid / price - 1) > 0.12:
            continue  # 只看当前价附近的位置
        for j in range(max(1, n - 3), n):
            prev_above = closes[j - 1] >= z.low
            now_below = closes[j] < z.low
            if prev_above and now_below:
                vol_confirm = vols[j] > 1.2 * avg_vol
                still_below = closes[-1] < z.low
                confirmed = still_below and (vol_confirm or (n - 1 > j))
                events.append({
                    "type": "跌破" if confirmed else "跌破(待确认)",
                    "zone": z.to_dict(price),
                    "bars_ago": n - 1 - j,
                    "vol_confirm": bool(vol_confirm),
                })
                break
            prev_below = closes[j - 1] <= z.high
            now_above = closes[j] > z.high
            if prev_below and now_above:
                held = n - 1 - j >= 1 and all(c > z.high for c in closes[j:])
                events.append({
                    "type": "站稳" if held else "站上(待确认)",
                    "zone": z.to_dict(price),
                    "bars_ago": n - 1 - j,
                    "vol_confirm": bool(vols[j] > 1.2 * avg_vol),
                })
                break
    return events
