"""盘中检查，供每小时定时任务调用。

2026-10-17 起 check() 只做一件事：报告"今天盘中新进入低位"的自选股。用户自己看价格买、自己止损、自己卖，
所以不再给进场区、止损、止盈。旧的进场区/持仓止损提醒保留为 check_trading()，需要时可切回。

"""
from __future__ import annotations

import time
from datetime import datetime

import pandas as pd

from . import alertstate, data
from .alertstate import RANK as _RANK
from .brief import PUSH_MAX
from .levels import build_zones, split_zones
from .report import _tier
from .sizing import CLUSTER_CAP_PCT, SUB_CLUSTER_CAP_PCT, _load_clusters
from .stock import dip_score


def _live_dip_estimate(df, last: float, sup, tol, day_high: float | None = None, day_low: float | None = None) -> float:
    """盘中抄底分估计：把实时价拼成今天的一根合成K线再算分(成交量项不可靠,仅作排序用)。

    df 只含已收盘的K线，最后一根是昨天，所以今天的高低点要单独传进来，取不到就用现价。
    """
    try:
        row = pd.DataFrame(
            {"Open": [last], "High": [max(last, day_high or last)],
             "Low": [min(last, day_low or last)], "Close": [last],
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


SUPPORT_MAX = 20            # dip_score 里"贴近强支撑"最多加这么多分


def _price_minutes_ago(bars: list[tuple[int, float]], minutes: int = 60, now_ts: float | None = None) -> float | None:
    """约 minutes 分钟前的价格。bars 是当天 5 分钟线 [(开始时间epoch秒, 收盘价), ...]。

    那时还没开盘（盘中第一次检查）返回 None。
    """
    cut = (time.time() if now_ts is None else now_ts) - minutes * 60
    prior = [c for t, c in bars if t <= cut]
    return prior[-1] if prior else None


def _check_one(item: dict, held: set[str], sent: dict[str, str] | None = None, as_of: pd.Timestamp | None = None) -> dict | None:
    t = item["symbol"]
    # 比的是昨收：美东 16:20 之后日线已经带上今天，这里切掉，不然今天会算两遍
    df = data.get_daily(t, "2y")
    df = df[df.index < pd.Timestamp(data._now_ct().date())]
    # 日线缺最近一根：昨收和分数都是旧的，和盘后报告同一口径，算取数失败
    if as_of is not None and df.index[-1] < as_of:
        raise ValueError(f"日线只到 {df.index[-1].date()}")
    q = data.get_intraday(t)
    if not q or not q.get("last"):
        raise ValueError("取不到现价")
    last = float(q["last"])
    # 这只票今天还没有成交（休市日 SPY 又没取到，或者停牌）：现价就是上一个交易日的收盘，涨跌幅也是那天的，没有新信息
    bars = q.get("bars") or []
    if bars and datetime.fromtimestamp(bars[-1][0], data.MARKET_TZ).date() != datetime.fromtimestamp(time.time(), data.MARKET_TZ).date():
        return None
    # 拆股当天日线和报价可能还没调到同一口径。容差不能小：除息日 chartPreviousClose 会减掉分红（2026-11-04 EQT、BRO、MUSA 实测，观察池一年有 208 次分红超过 0.5%）
    ref_close = float(df["Close"].iloc[-1])
    if not 0.85 < float(q.get("prev_close") or ref_close) / ref_close < 1.15 or last < ref_close * 0.5:
        raise ValueError("日线和报价对不上（拆股？）")
    hi, lo = q.get("day_high"), q.get("day_low")
    # 不算支撑项就已经够不着低位的票，不用算支撑区（这是最耗 CPU 的一步，平常日子能省掉九成）
    if _live_dip_estimate(df, last, [], 0, hi, lo) + SUPPORT_MAX < 55:
        return None
    zones, tol = build_zones(df)
    sup_now, _, _ = split_zones(zones, last)
    est = _live_dip_estimate(df, last, sup_now, tol, hi, lo)
    tier_now = _tier(est)
    if _RANK[tier_now] < _RANK["低位"]:
        return None
    # 昨收已经在这个档位，或者今天已经推过这个档位，都不算新进入（2026-11-04：ALAB、ETN、FIS 在档位线上下来回穿，只比一小时前的话各推 3 次）
    prev_score = dip_score(df, split_zones(zones, ref_close)[0], tol)["score"]
    floor = max(_RANK[_tier(prev_score)], _RANK.get((sent or {}).get(t), 0))
    if _RANK[tier_now] <= floor:
        return None
    # 必须是跌进来的："贴近强支撑"那 20 分会让价格涨上去时分数反而变高（实测 GOOG 当天 +4% 被判成新进入深度低位）。
    # 当天还在涨的不算；比一小时前高、而且这一档是涨上来才有的也不算。一小时前已经在这一档只是还没推过（上次放不下、
    # 没跑成或取数失败）的要推，不然反弹一点就再也提醒不到（2026-10-30 QBTS 跌 10.9%，全天都没写出名字）。
    if last >= ref_close:
        return None
    hour_ago = _price_minutes_ago(q.get("bars") or [])
    if hour_ago is not None and last >= hour_ago:
        then = _RANK[_tier(_live_dip_estimate(df, hour_ago, split_zones(zones, hour_ago)[0], tol, hi, lo))]
        if then < _RANK[tier_now] and then <= max(floor, _RANK["接近低位"]):      # 一小时前已经够得上一个没推过的档位：不能因为现在分数更高反而不推
            return None
    return {
        "ticker": t, "kind": f"盘中进入{tier_now}", "tier": tier_now, "sector": item.get("sector"),
        "last": round(last, 2), "chg_pct": round(q["chg_pct"], 2),
        "dip_est": round(est, 0), "prev_score": round(prev_score, 0),
        "held": t in held, "note": item.get("note", ""),
    }


def _market_open_today() -> bool:
    """休市日 Yahoo 返回上一个交易日的分钟线。取不到就照常检查，真有问题会算进取数失败。"""
    bars = data.get_intraday("SPY").get("bars")
    if not bars:
        return True
    return datetime.fromtimestamp(bars[-1][0], data.MARKET_TZ).date() == data._now_ct().date()


def _last_close_day() -> pd.Timestamp | None:
    """观察池的日线应该到哪一天（SPY 最近一根已收盘的日线，和盘后报告同一口径）。取不到就不查。"""
    try:
        spy = data.get_daily("SPY", "2y")
        return spy[spy.index < pd.Timestamp(data._now_ct().date())].index[-1]
    except Exception:
        return None


def scan(watchlist: list[dict]) -> dict:
    """盘中新进入低位（低位 = 抄底分≥55，深度低位 ≥70；与早晚报的低位清单同一口径）。

    条件：盘中估分所在档位 ≥ 低位，高于昨收档位和今天已经推过的档位，价格比昨收低，而且这一档不是最近一小时涨上来才有的。
    盘中估分把实时价当收盘价算（成交量用 20 日均量，看不到恐慌放量那 +10 分），所以要收盘确认。
    返回 {"alerts": [...], "n_ok": 成功检查的只数, "failed": [取数失败的票], "date": 今天,
         "sent_ok": 今天推过什么读没读到, "market_closed": 今天是否休市}。
    """
    from .report import load_holdings
    day = data._now_ct().date().isoformat()
    if not _market_open_today():
        return {"alerts": [], "n_ok": 0, "failed": [], "date": day, "sent_ok": True, "market_closed": True}
    sent = alertstate.load_sent(day)      # 读不到时当作没推过：宁可重复也不能漏，推送里会说明
    held = set(load_holdings())
    have = {i["symbol"] for i in watchlist}
    items = watchlist + [{"symbol": t} for t in load_holdings() if t not in have]
    as_of = _last_close_day()

    def one(item):
        try:
            return item["symbol"], _check_one(item, held, sent, as_of), None
        except Exception as e:
            return item["symbol"], None, str(e)

    res = data.pmap(one, items)
    alerts = [a for _, a, _ in res if a]
    alerts.sort(key=lambda x: (not x["held"], -x["dip_est"]))
    failed = [t for t, _, e in res if e]
    return {"alerts": alerts, "n_ok": len(res) - len(failed), "failed": failed, "date": day,
            "sent_ok": sent is not None, "market_closed": False}


def check(watchlist: list[dict]) -> list[dict]:
    return scan(watchlist)["alerts"]


PUSH_DETAIL = 4         # 前几只写全（档位、涨跌、现价），其余只写代码、板块和分数
WARN_FAILED = 20        # 取数失败超过这么多只，就算没有新低位也要推一条，不然和"今天没票跌进低位"分不开


def push_text(alerts: list[dict], n_failed: int = 0, n_total: int = 0, sent_ok: bool = True) -> tuple[str, list[str]]:
    """盘中推送原文，和其中写出名字的票。没什么可说时返回空字符串（不推送）。

    放不下的票只算在 +N只 里，不记成推过，下一小时接着推。
    """
    warns = []
    if n_failed > WARN_FAILED:
        warns.append(f"⚠盘中检查{n_failed}/{n_total}只取数失败，这小时没查全")
    if alerts and not sent_ok:
        warns.append("⚠读不到今天推过什么，可能重复")
    if not alerts:
        return "\n".join(warns), []
    lines = warns + ["📉今天新进入低位（估分，以盘后报告为准）"]
    used = len("\n".join(lines))
    tail = len("\n另有 +999只")
    shown: list[str] = []

    def name(a):
        return f"{a['ticker']}{'★' if a['held'] else ''}[{a.get('sector') or '未分类'}]"

    for a in alerts[:PUSH_DETAIL]:
        line = f"{name(a)} {a['tier']}{int(a['dip_est'])} {a['chg_pct']:+.1f}% 现价{a['last']:g}"
        left = len(alerts) - len(shown) - 1
        if used + 1 + len(line) + (tail if left else 0) > PUSH_MAX:
            break
        lines.append(line)
        used += 1 + len(line)
        shown.append(a["ticker"])
    rest = alerts[len(shown):]
    if rest:
        used += len("\n另有")
        bits = []
        for i, a in enumerate(rest):
            bit = f"{name(a)}{int(a['dip_est'])}"
            left = len(rest) - i - 1
            if used + 1 + len(bit) + (len(" +999只") if left else 0) > PUSH_MAX:
                bits.append(f"+{len(rest) - i}只")
                break
            bits.append(bit)
            used += 1 + len(bit)
            shown.append(a["ticker"])
        lines.append("另有 " + " ".join(bits))
    return "\n".join(lines), shown
