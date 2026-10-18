"""盘中检查，供每小时定时任务调用。

2026-10-17 起 check() 只做一件事：报告"今天盘中新进入低位"的自选股。用户自己看价格买、自己止损、自己卖，
所以不再给进场区、止损、止盈。旧的进场区/持仓止损提醒保留为 check_trading()，需要时可切回。

（以下为旧说明）盘中触发检查（轻量）：现价是否进入进场参考区。

只做位置判断不做完整分析——抄底分依赖日线收盘，盘中给的是"到位提醒"，
最终确认要等收盘后的报告。行情为15分钟延迟。
"""
from __future__ import annotations

import pandas as pd

from . import data
from .levels import build_zones, split_zones
from .sizing import CLUSTER_CAP_PCT, SUB_CLUSTER_CAP_PCT, _load_clusters
from .stock import dip_score


def _live_dip_estimate(df, last: float, sup, tol) -> float:
    """盘中抄底分估计：把实时价拼成一根合成K线再算分(成交量项不可靠,仅作排序用)。"""
    try:
        row = pd.DataFrame(
            {"Open": [last], "High": [max(last, float(df['High'].iloc[-1]))],
             "Low": [min(last, float(df['Low'].iloc[-1]))], "Close": [last],
             "Volume": [float(df['Volume'].tail(20).mean())]},
            index=[df.index[-1] + pd.Timedelta(days=1)],
        )
        return dip_score(pd.concat([df, row]), sup, tol)["score"]
    except Exception:
        return 0.0


def check_trading(watchlist: list[dict]) -> list[dict]:
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
                # 持仓止损必须与早报【同一条代码路径】算出来：用户照早报挂 GTC，盘中提醒若用另一套
                # 简化算法，两者就会分叉。2026-10-15 实测：NBIS 早报止损 174.39、盘中旧算法 144.87
                # （"接近止损"要到 ~161 才响，此时 GTC 早已触发）；SPCX 早报 138.87、盘中算不出止损。
                # 根因是盘中路径的 build_zones 不带期权墙/分钟成交分布等 extra，强支撑区判定不同。
                # 持仓通常只有几只，走一遍完整 analyze 的成本可以接受(不取新闻)。
                from .stock import analyze
                from .plan import ratchet_invalid
                a_full = analyze(t, with_news=False, with_short=False)
                plan_p = a_full.get("plan") or {}
                if item.get("since"):
                    ratchet_invalid(plan_p, t, str(item["since"]))   # 与 advise() 持仓分支同一棘轮
                inv = plan_p.get("invalid_below")
                a_v = float(a_full["atr"])
                tp_full = float(cost) * 1.10
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
                    "_dip_est": _live_dip_estimate(df, last, sup, tol),
                    "_zscore": float(ez.score),
                })
        except Exception:
            continue

    # ---- 候选触发的优先级排序：簇余量 > 抄底分段位 > 进场区强度 ----
    # (大跌日十几只同时到位时,资金有限该先接谁——排序逻辑与仓位引擎同源)
    parent_map, sub_map = _load_clusters()
    pos_pct: dict[str, float] = {}
    for it in watchlist:
        if it.get("cost") is not None and it.get("pct"):
            pos_pct[it["symbol"]] = float(it["pct"])
    csums: dict[str, float] = {}
    ssums: dict[str, float] = {}
    for tk, p in pos_pct.items():
        csums[parent_map.get(tk, "其他")] = csums.get(parent_map.get(tk, "其他"), 0) + p
        sc = sub_map.get(tk)
        if sc:
            ssums[sc] = ssums.get(sc, 0) + p

    entries = [x for x in out if x["kind"] in ("盘中进入进场区", "大跌逼近进场区")]
    others = [x for x in out if x not in entries]
    for x in entries:
        c = parent_map.get(x["ticker"], "其他")
        room = CLUSTER_CAP_PCT - csums.get(c, 0)
        sc = sub_map.get(x["ticker"])
        if sc:
            room = min(room, SUB_CLUSTER_CAP_PCT - ssums.get(sc, 0))
        x["cluster"] = c
        x["cluster_room_pct"] = round(room, 1)
        x["dip_est"] = round(x.pop("_dip_est"), 0)
        band = 2 if x["dip_est"] >= 70 else (1 if x["dip_est"] >= 55 else 0)
        x["_rank"] = (room > 0, band, x.pop("_zscore"))
    entries.sort(key=lambda x: x["_rank"], reverse=True)
    # 只有"盘中估分≥55 且簇有余量"的才编 P 号。2026-10-15 发现：30 分的 NU 被编成 P3，
    # 而推送原文是"P1 最优先接"——一个未达进场线的票被包装成了买入推荐，用户自己也把盘中
    # 推送称为"推荐进场点位"。本周 17 笔买入里 16 笔低于 55 分，这个呈现方式难辞其咎。
    # 45-54 单列：盘中估分看不到"恐慌放量 +10"(合成K线用20日均量)，收盘放量后可能达标。
    n = 0
    for x in entries:
        x.pop("_rank")
        why = []
        if x["dip_est"] < 55:
            why.append(f"盘中估分 {x['dip_est']:.0f} 未达 55 进场线"
                       + ("（收盘若放量>1.8倍可能+10分达标）" if x["dip_est"] >= 45 else ""))
        if x["cluster_room_pct"] <= 0:
            why.append(f"「{x['cluster']}」簇已满")
        if why:
            x["priority"] = None
            x["status"] = "观察"
            x["note"] = ("观察，不是进场信号：" + "；".join(why) + (f" — {x['note']}" if x["note"] else ""))
        else:
            n += 1
            x["priority"] = n
            x["status"] = "候选"
    return others + entries


_RANK = {None: 0, "接近低位": 1, "低位": 2, "深度低位": 3}


def _price_hour_ago(ticker: str, minutes: int = 60) -> float | None:
    """约一小时前的盘中价格（Polygon 分钟线）。用于判断"这一小时才刚跌进低位"，
    避免同一只票整天每小时重复推送——云端每次运行都是全新副本，记不住上次推过什么。
    盘中第一次检查（一小时前还没开盘）返回 None，此时与昨收比较。"""
    try:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        from .providers import polygon
        now = datetime.now(ZoneInfo("America/New_York"))
        j = polygon._get(f"/v2/aggs/ticker/{polygon._sym(ticker)}/range/1/minute/{now.date()}/{now.date()}",
                         adjusted="true", sort="asc", limit=50000)
        bars = j.get("results") or []
        cut = (now - timedelta(minutes=minutes)).timestamp() * 1000
        open_ms = now.replace(hour=9, minute=30, second=0, microsecond=0).timestamp() * 1000
        if cut < open_ms:
            return None
        prior = [b for b in bars if open_ms <= b["t"] <= cut]
        return float(prior[-1]["c"]) if prior else None
    except Exception:
        return None


def check(watchlist: list[dict]) -> list[dict]:
    """盘中新进入低位（低位 = 抄底分≥55，深度低位 ≥70；与早晚报的低位清单同一口径）。

    条件：盘中估分所在档位 ≥ 低位，且高于"一小时前"的档位（盘中第一次检查时与昨收比）。
    盘中估分把实时价当收盘价算（成交量用 20 日均量，看不到恐慌放量那 +10 分），所以要收盘确认。
    """
    from .report import _tier, load_holdings
    held = set(load_holdings())
    have = {i["symbol"] for i in watchlist}
    out = []
    for item in watchlist + [{"symbol": t} for t in held if t not in have]:
        t = item["symbol"]
        try:
            df = data.get_daily(t, "2y")
            q = data.get_quote(t)
            if not q or not q.get("last"):
                continue
            last = float(q["last"])
            zones, tol = build_zones(df)
            sup_now, _, _ = split_zones(zones, last)
            est = _live_dip_estimate(df, last, sup_now, tol)
            tier_now = _tier(est)
            if _RANK[tier_now] < _RANK["低位"]:
                continue
            p1h = _price_hour_ago(t)
            if p1h is None:            # 盘中第一次检查：与昨收比
                ref_close = float(df["Close"].iloc[-1])
                sup_ref, _, _ = split_zones(zones, ref_close)
                ref_est = dip_score(df, sup_ref, tol)["score"]
                ref_label = "昨收"
            else:
                sup_ref, _, _ = split_zones(zones, p1h)
                ref_est = _live_dip_estimate(df, p1h, sup_ref, tol)
                ref_label = "一小时前"
            if _RANK[tier_now] <= _RANK[_tier(ref_est)]:
                continue
            chg = q.get("chg_pct")
            out.append({
                "ticker": t, "kind": f"盘中进入{tier_now}", "tier": tier_now,
                "last": round(last, 2), "chg_pct": round(chg, 2) if chg is not None else None,
                "dip_est": round(est, 0), "ref_est": round(ref_est, 0), "ref": ref_label,
                "held": t in held, "note": item.get("note", ""),
            })
        except Exception:
            continue
    out.sort(key=lambda x: -x["dip_est"])
    return out
