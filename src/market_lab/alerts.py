"""盘中触发检查（轻量）：现价是否进入进场参考区，供每小时定时任务调用。

只做位置判断不做完整分析——抄底分依赖日线收盘，盘中给的是"到位提醒"，
最终确认要等收盘后的报告。行情为15分钟延迟。
"""
from __future__ import annotations

from . import data
from .levels import build_zones, split_zones


def check(watchlist: list[dict]) -> list[dict]:
    out = []
    for item in watchlist:
        t = item["symbol"]
        try:
            df = data.get_daily(t, "2y")  # 当日已缓存，便宜
            q = data.get_quote(t)
            if not q or not q.get("last"):
                continue
            last = float(q["last"])
            ref_close = float(df["Close"].iloc[-1])
            zones, tol = build_zones(df)
            sup, res, inside = split_zones(zones, ref_close)
            cand = ([inside] if inside else []) + sup
            strong = [z for z in cand if z.score >= 4.0]
            if not strong:
                continue
            ez = strong[0]
            prev = float(q.get("prev_close") or ref_close)
            chg = q.get("chg_pct")
            newly_in = last <= ez.high and prev > ez.high
            crash_at = last <= ez.high * 1.02 and chg is not None and chg <= -4
            if newly_in or crash_at:
                out.append({
                    "ticker": t,
                    "last": round(last, 2),
                    "chg_pct": round(chg, 2) if chg is not None else None,
                    "entry_zone": {"low": round(ez.low, 2), "high": round(ez.high, 2), "score": round(ez.score, 1)},
                    "kind": "盘中进入进场区" if newly_in else "大跌逼近进场区",
                    "note": item.get("note", ""),
                })
        except Exception:
            continue
    return out
