"""盘中检查，供每小时定时任务调用。

2026-10-17 起 check() 只做一件事：报告"今天盘中新进入低位"的自选股。用户自己看价格买、自己止损、自己卖，
所以不再给进场区、止损、止盈。旧的进场区/持仓止损提醒保留为 check_trading()，需要时可切回。

"""
from __future__ import annotations

import time

import pandas as pd

from . import data
from .levels import build_zones, split_zones
from .report import _tier
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
SUPPORT_MAX = 20            # dip_score 里"贴近强支撑"最多加这么多分


def _price_minutes_ago(bars: list[tuple[int, float]], minutes: int = 60, now_ts: float | None = None) -> float | None:
    """约 minutes 分钟前的价格。bars 是当天 5 分钟线 [(开始时间epoch秒, 收盘价), ...]。

    那时还没开盘（盘中第一次检查）返回 None。
    """
    cut = (time.time() if now_ts is None else now_ts) - minutes * 60
    prior = [c for t, c in bars if t <= cut]
    return prior[-1] if prior else None


def _earlier_check_lows(bars: list[tuple[int, float]], now_ts: float | None = None) -> list[float]:
    """今天更早的每个小时检查点附近的最低价（由近到远）。云端任务实际触发有几分钟抖动，所以取检查点前后一段时间，
    不然上一次刚提醒过的票，这次复算时价格差一点就落回档位线上方，会被当成"新进入"再推一遍。"""
    if not bars:
        return []
    cut = (time.time() if now_ts is None else now_ts) - 3600
    out = []
    while cut + 300 >= bars[0][0]:
        near = [c for t, c in bars if cut - 600 <= t <= cut + 300]
        if near:
            out.append(min(near))
        cut -= 3600
    return out


def _check_one(item: dict, held: set[str]) -> dict | None:
    t = item["symbol"]
    df = data.get_daily(t, "2y")
    q = data.get_intraday(t)
    if not q or not q.get("last"):
        raise ValueError("取不到现价")
    last = float(q["last"])
    # 不算支撑项就已经够不着低位的票，不用算支撑区（这是最耗 CPU 的一步，平常日子能省掉九成）
    if _live_dip_estimate(df, last, [], 0) + SUPPORT_MAX < 55:
        return None
    zones, tol = build_zones(df)
    sup_now, _, _ = split_zones(zones, last)
    est = _live_dip_estimate(df, last, sup_now, tol)
    tier_now = _tier(est)
    if _RANK[tier_now] < _RANK["低位"]:
        return None
    # 今天早些时候（每小时一个检查点）和昨收的最高档位。云端每次都是全新副本、记不住推过什么，所以用当天分钟线复算：
    # 价格在档位线上下来回穿，同一只票也只在第一次跌进去时提醒（2026-11-04 回放：只比一小时前的话，ALAB、ETN、FIS 各推 3 次）。
    bars = q.get("bars") or []
    ref_close = float(df["Close"].iloc[-1])    # 日线只含已收盘的K线，最后一根就是昨天
    best = dip_score(df, split_zones(zones, ref_close)[0], tol)["score"]
    for p in _earlier_check_lows(bars):
        best = max(best, _live_dip_estimate(df, p, split_zones(zones, p)[0], tol))
    if _RANK[tier_now] <= _RANK[_tier(best)]:
        return None
    # 必须是跌进来的。"贴近强支撑"那 20 分会让价格涨上去时分数反而变高（实测 GOOG 当天 +4% 被判成新进入深度低位）
    hour_ago = _price_minutes_ago(bars)
    if last >= (ref_close if hour_ago is None else hour_ago):
        return None
    return {
        "ticker": t, "kind": f"盘中进入{tier_now}", "tier": tier_now, "sector": item.get("sector"),
        "last": round(last, 2), "chg_pct": round(q["chg_pct"], 2),
        "dip_est": round(est, 0), "earlier_best": round(best, 0),
        "held": t in held, "note": item.get("note", ""),
    }


def scan(watchlist: list[dict]) -> dict:
    """盘中新进入低位（低位 = 抄底分≥55，深度低位 ≥70；与早晚报的低位清单同一口径）。

    条件：盘中估分所在档位 ≥ 低位，高于昨收和今天早些时候的档位，且价格比一小时前（第一次检查时比昨收）低。
    盘中估分把实时价当收盘价算（成交量用 20 日均量，看不到恐慌放量那 +10 分），所以要收盘确认。
    返回 {"alerts": [...], "n_ok": 成功检查的只数, "failed": [取数失败的票]}。
    """
    from .report import load_holdings
    held = set(load_holdings())
    have = {i["symbol"] for i in watchlist}
    items = watchlist + [{"symbol": t} for t in load_holdings() if t not in have]

    def one(item):
        try:
            return item["symbol"], _check_one(item, held), None
        except Exception as e:
            return item["symbol"], None, str(e)

    res = data.pmap(one, items)
    alerts = [a for _, a, _ in res if a]
    alerts.sort(key=lambda x: (not x["held"], -x["dip_est"]))
    failed = [t for t, _, e in res if e]
    return {"alerts": alerts, "n_ok": len(res) - len(failed), "failed": failed}


def check(watchlist: list[dict]) -> list[dict]:
    return scan(watchlist)["alerts"]


PUSH_DETAIL = 4         # 前几只写全（档位、涨跌、现价），其余只写代码、板块和分数
PUSH_MAX = 300


def push_text(alerts: list[dict]) -> str:
    """盘中推送原文。没有新进入低位的票就返回空字符串（不推送）。"""
    if not alerts:
        return ""
    lines = ["📉盘中新进入低位（盘中估分，收盘才确认）"]
    for a in alerts[:PUSH_DETAIL]:
        lines.append(f"{a['ticker']}{'★' if a['held'] else ''}[{a.get('sector') or '未分类'}] {a['tier']}{int(a['dip_est'])}"
                     f" {a['chg_pct']:+.1f}% 现价{a['last']:g}")
    rest = alerts[PUSH_DETAIL:]
    if rest:
        text = "\n".join(lines) + "\n另有 "
        bits = []
        for i, a in enumerate(rest):
            bit = f"{a['ticker']}{'★' if a['held'] else ''}[{a.get('sector') or '未分类'}]{int(a['dip_est'])}"
            if len(text) + sum(len(b) + 1 for b in bits) + len(bit) > PUSH_MAX - 6:
                bits.append(f"+{len(rest) - i}只")
                break
            bits.append(bit)
        lines.append("另有 " + " ".join(bits))
    return "\n".join(lines)
