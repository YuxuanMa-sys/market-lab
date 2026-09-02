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

            # 持仓票：盯防止损/止盈两侧(单笔挂单模式下,没挂的那一侧全靠这里提醒换单)
            cost = item.get("cost")
            if cost is not None:
                from .indicators import atr as _atr
                a_v = float(_atr(df).iloc[-1])
                tp_full = float(cost) * 1.10
                zones_p, _ = build_zones(df)
                sup_p, _, ins_p = split_zones(zones_p, ref_close)
                cand_p = ([ins_p] if ins_p else []) + sup_p
                strong_p = [z for z in cand_p if z.score >= 4.0]
                inv = (strong_p[0].low - 0.75 * a_v) if strong_p else None
                chg = q.get("chg_pct")
                if inv and last <= inv + 0.75 * a_v:
                    out.append({"ticker": t, "last": round(last, 2),
                                "chg_pct": round(chg, 2) if chg is not None else None,
                                "entry_zone": {"low": round(inv, 2), "high": round(inv, 2), "score": 0},
                                "kind": "持仓接近止损位", "note": f"止损参考位 {inv:.2f}——若该侧没挂单，现在换单"})
                elif last >= tp_full - 0.75 * a_v:
                    out.append({"ticker": t, "last": round(last, 2),
                                "chg_pct": round(chg, 2) if chg is not None else None,
                                "entry_zone": {"low": round(tp_full, 2), "high": round(tp_full, 2), "score": 0},
                                "kind": "持仓接近止盈位", "note": f"止盈位 {tp_full:.2f}(成本×1.10)——若该侧没挂单，现在换单"})
                continue  # 持仓票不再做进场区检查
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
