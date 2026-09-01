"""模式化买卖建议引擎（大盘自适应调度）。

每只票按 watchlist 里的模式标签给建议：
  swing = 抄底波段（默认）：进场靠触发(≥55分)，止盈靠TP，认错靠无效位
  trend = 趋势长拿：不设固定止盈，只在趋势破坏时离场（连续2日收低于50日线 或 破移动止损）
  event = 财报事件票：同 swing 触发，但要求人工确认暴跌是过度反应而非基本面恶化

大盘模式决定当下哪些动作可开火（来自回测）：
  panic 恐慌期 → 抄底火力全开；bull 多头市 → 压制个股独跌抄底(45-54段回测胜率仅32%)；
  range 震荡市 → 允许持仓的轮动减仓(E模式,仅1/3,卖出时同时定接回位和追回位)；
  bear 空头市 → 只做70+极端恐慌。

watchlist 里填了 cost 的视为持仓，给卖出侧建议。输出是分析参考，不是指令。
"""
from __future__ import annotations

from datetime import date, datetime

# 数字越小越紧急，报告里排越前
ACTION_URGENCY = {
    "清仓认错": 0, "趋势破坏-离场": 0,
    "止盈减仓": 1, "财报降风险": 1,
    "轮动减仓": 2, "时间止损": 2, "警戒持有": 3,
    "进场分批": 4, "临近挂单": 4, "持有": 5,
    "等待": 6, "回避": 7,
}
URGENT_ACTIONS = {"清仓认错", "趋势破坏-离场", "止盈减仓", "财报降风险", "轮动减仓", "时间止损"}


def market_mode(mkt: dict) -> tuple[str, str]:
    if mkt["dip_gauge"]["score"] >= 65 or mkt["vix"].get("backwardation"):
        return "panic", "恐慌期——抄底模式火力全开，这是你的策略主场"
    label = mkt["regime"]["label"]
    if label in ("空头",):
        return "bear", "空头市——现金为主，只做抄底分70+的极端恐慌"
    if label == "震荡":
        return "range", "震荡市——区间思路，持仓允许在强压力处轮动减1/3"
    if label == "恐慌":
        return "panic", "恐慌期——抄底模式火力全开"
    return "bull", "多头市——持仓让利润跑；警惕大盘岁月静好时的个股独跌(回测该情形胜率仅32%)"


def _days_to(datestr: str | None) -> int | None:
    if not datestr:
        return None
    try:
        return (datetime.strptime(datestr, "%Y-%m-%d").date() - date.today()).days
    except ValueError:
        return None


def _tranches(lo: float, hi: float) -> dict:
    return {"t1": round(hi, 2), "t2": round((lo + hi) / 2, 2), "t3": round(lo, 2)}


def _o(side: str, otype: str, price: float, portion: str, note: str = "") -> dict:
    return {"side": side, "type": otype, "price": round(price, 2), "portion": portion, "note": note}


def _build_orders(res: dict, a: dict, cost: float | None, mmode: str) -> None:
    """把建议翻译成可直接执行的挂单清单（价位+份额）。份额指该票计划仓位的比例。"""
    act = res["action"]
    price = a["price"]
    plan = a.get("plan") or {}
    targets = plan.get("targets") or []
    inv = plan.get("invalid_below")
    orders: list[dict] = []

    if cost is not None:
        if act in ("清仓认错", "趋势破坏-离场"):
            orders.append(_o("卖", "限价", price * 0.995, "全部", "尽快离场，挂现价下方一点保证成交"))
        elif act == "止盈减仓":
            orders.append(_o("卖", "限价", price, "1/3", "已在止盈区，即市附近卖出"))
            # 锁盈位取"保本价"与"TP1下沿-0.75ATR"的更低者：cost 可能被 wash sale 抬高到
            # 止盈区之上，直接挂保本价会挂在现价上方立刻触发
            lock = cost
            if targets:
                lock = min(cost, targets[0]["low"] - 0.75 * a["atr"])
            orders.append(_o("卖", "止损", lock, "剩余", "锁盈位（保本价与TP1下方0.75ATR取低者），让剩余仓位低风险奔跑"))
        elif act == "轮动减仓" and res.get("_rotation"):
            r = res["_rotation"]
            orders.append(_o("卖", "限价", r["sell"], "1/3", "压力区+过热，先落袋"))
            orders.append(_o("买", "限价", r["rebuy"], "1/3", "接回单：回落到支撑接回"))
            orders.append(_o("买", "止损买入", r["chase"], "1/3", "追回单：放量站上压力区就认错追回"))
        elif act == "财报降风险":
            if res.get("pnl_pct", 0) > 0:
                orders.append(_o("卖", "限价", price, "1/3", "财报前先兑现一部分浮盈"))
            if inv:
                orders.append(_o("卖", "止损", inv, "全部", "无效位止损单保持(GTC)"))
        else:  # 持有 / 警戒持有
            if res.get("mode") == "trend":
                ts = a.get("trail_stop")
                if ts:
                    orders.append(_o("卖", "止损", ts, "全部", "移动止损(60日高-2.75ATR)，每周只上移不下移"))
            elif act == "时间止损":
                orders.append(_o("卖", "限价", price, "全部", "时间止损：15个交易日未达标，离场换弹"))
            else:
                if inv:
                    orders.append(_o("卖", "止损", inv, "全部", "无效位止损单(GTC)，跌破自动离场"))
                if targets:
                    tp_price = targets[0].get("mid", targets[0]["low"])
                    orders.append(_o("卖", "限价", tp_price, "1/3", "TP1 止盈单(GTC)，挂压力区中部(回测:卖下沿过于保守)"))
    else:
        if act == "进场分批" and res.get("tranches"):
            tr = res["tranches"]
            orders.append(_o("买", "限价", tr["t1"], "1/3", "进场区上沿"))
            orders.append(_o("买", "限价", tr["t2"], "1/3", "进场区中部"))
            orders.append(_o("买", "限价", tr["t3"], "1/3", "进场区下沿/恐慌针刺"))
            if inv:
                orders.append(_o("卖", "止损", inv, "成交部分", "成交后立刻设无效位止损"))
        elif act == "等待" and not res.get("_no_orders"):
            # 临近预挂：分数已过线(≥55)、价格逼近进场区上沿3%以内——提前埋伏下两档
            ez = plan.get("entry_zone")
            dip = a["dip"]["score"]
            if ez and dip >= 55 and price <= ez["high"] * 1.03:
                res["action"] = "临近挂单"
                res["reasons"].append(f"抄底分 {dip:.0f} 已过线且现价距进场区上沿不足3%——可提前挂低接单")
                tr = _tranches(ez["low"], ez["high"])
                orders.append(_o("买", "限价", tr["t2"], "1/3", "进场区中部埋伏"))
                orders.append(_o("买", "限价", tr["t3"], "1/3", "进场区下沿/恐慌针刺"))
                if inv:
                    orders.append(_o("卖", "止损", inv, "成交部分", "成交后立刻设无效位止损"))

    res["orders"] = orders


def advise(a: dict, item: dict, mmode: str) -> dict:
    mode = item.get("mode", "swing")
    cost = item.get("cost")
    price = a["price"]
    atr_v = a["atr"]
    rsi = a["rsi"]
    dip = a["dip"]["score"]
    plan = a.get("plan") or {}
    ma50 = a.get("ma", {}).get(50) or {}
    de = _days_to(a.get("earnings_date"))

    res: dict = {"mode": mode, "action": "等待", "reasons": [], "tranches": None}

    # ================= 持仓侧 =================
    if cost:
        pnl = round((price / cost - 1) * 100, 1)
        res["pnl_pct"] = pnl

        if mode == "trend":
            ts = a.get("trail_stop")
            streak = a.get("below_ma50_streak", 0)
            if streak >= 2 or (ts and price < ts):
                res["action"] = "趋势破坏-离场"
                res["reasons"].append(
                    f"连续{streak}日收低于50日线" if streak >= 2 else f"跌破移动止损 {ts}(60日高点-2.75ATR)"
                )
                res["reasons"].append("趋势票的唯一卖点就是趋势破坏——别把趋势票拿成套牢票")
            elif streak == 1:
                res["action"] = "警戒持有"
                res["reasons"].append(f"首日收低于50日线({ma50.get('value')})，再收一日低于此则离场")
            else:
                res["action"] = "持有"
                dist = (price / a["trail_stop"] - 1) * 100 if a.get("trail_stop") else None
                res["reasons"].append(
                    "趋势完好——趋势票的回撤不看浮盈只看结构"
                    + (f"，距移动止损还有 {dist:.1f}%" if dist is not None else "")
                )
        else:  # swing / event 持仓
            invalid = plan.get("invalid_below")
            targets = plan.get("targets") or []
            if invalid and price < invalid:
                res["action"] = "清仓认错"
                nxt = plan.get("if_invalid_next_support")
                res["reasons"].append(
                    f"价格低于无效位 {invalid}——抄底逻辑已失效"
                    + (f"，下一档支撑 {nxt['low']}–{nxt['high']}" if nxt else "")
                )
            elif targets and price >= targets[-1]["low"]:
                res["action"] = "止盈减仓"
                res["reasons"].append(
                    f"已达最后一档止盈区 {targets[-1]['low']}–{targets[-1]['high']}（浮盈 {pnl:+.1f}%）——按数据兑现大部，剩余把止损提到保本"
                )
            elif targets and price >= targets[0]["low"]:
                res["action"] = "止盈减仓"
                nxt_t = targets[1] if len(targets) > 1 else None
                res["reasons"].append(
                    f"进入 TP1 区 {targets[0]['low']}–{targets[0]['high']}：减 1/3"
                    + (f"，剩余看 TP2 {nxt_t['low']}–{nxt_t['high']}" if nxt_t else "")
                )
            else:
                res["action"] = "持有"
                if targets:
                    res["reasons"].append(
                        f"位于无效位 {invalid} 与 TP1 {targets[0]['low']} 之间，计划未走完（浮盈 {pnl:+.1f}%）"
                    )

        # 时间止损(2026-09-01锦标赛校准)：+10%目标叠加"15个交易日未达标离场"后，
        # 资金效率从0.272%/天升至0.298%/天且胜率略升；超时滞留仓位是负期望(-0.5%/笔)
        since = item.get("since")
        if since and mode in ("swing", "event") and res["action"] == "持有":
            held_days = _days_to(str(since))
            if held_days is not None and -held_days >= 21:  # ≈15个交易日
                res["action"] = "时间止损"
                res["reasons"] = [
                    f"持仓已 {-held_days} 天(≈15+交易日)仍未到 TP1（浮盈 {pnl:+.1f}%）——回测：拖过15个交易日的仓位期望衰减为负，资金效率优先，参考离场换弹",
                ]

        # E 轮动叠加：仅震荡市、仅盈利单、仅强压力+过热、且当前无更紧急动作
        if mmode == "range" and res["action"] == "持有" and pnl >= 5:
            resis = a.get("resistances") or []
            near = next((z for z in resis if z["low"] - price <= 0.5 * atr_v), None)
            sup = (a.get("supports") or [None])[0]
            if near and rsi >= 68:
                res["action"] = "轮动减仓"
                res["reasons"] = [
                    f"震荡市+贴近强压力 {near['low']}–{near['high']}+RSI {rsi} 过热：可减 1/3 落袋",
                    (f"接回条件：回落至 {sup['low']}–{sup['high']}" if sup else "接回条件：回落至下方强支撑"),
                    f"追回条件：放量收盘站上 {near['high']}——突破了就认错追回，别让踏空演变成更高位追高",
                ]
                res["_rotation"] = {"sell": price, "rebuy": (sup["high"] if sup else price * 0.95), "chase": near["high"]}

        # 财报降风险叠加（所有模式）
        if de is not None and 0 <= de <= 3:
            if res["action"] in ("持有", "警戒持有"):
                res["action"] = "财报降风险"
            res["reasons"].append(
                f"⚠ {a['earnings_date']} 财报（{de}天内）：跳空双向、支撑挡不住跳空——杠杆清零，浮盈单考虑先兑现一部分"
            )
        _build_orders(res, a, cost, mmode)
        res.pop("_rotation", None)
        return res

    # ================= 候选侧（无持仓） =================
    if mmode == "bear" and dip < 70:
        res["action"] = "等待"
        res["_no_orders"] = True
        res["reasons"].append(f"空头市只做极端恐慌（抄底分70+），当前 {dip:.0f} 不够")
        _build_orders(res, a, None, mmode)
        return res
    if mmode == "bull" and 45 <= dip < 55:
        res["action"] = "回避"
        res["_no_orders"] = True
        res["reasons"].append("多头市里的个股独跌往往是有原因的跌：回测该情形胜率仅32%、负期望")
        _build_orders(res, a, None, mmode)
        return res

    if mode == "trend":
        v50 = ma50.get("value")
        if a.get("trend") == "多头排列" and v50 and v50 - 0.5 * atr_v <= price <= v50 + 1.0 * atr_v:
            res["action"] = "进场分批"
            res["reasons"].append(f"上升趋势回调至50日线带（{v50}）——趋势票的标准上车区")
            res["tranches"] = _tranches(v50 - 0.5 * atr_v, v50 + 0.5 * atr_v)
        else:
            res["action"] = "等待"
            res["_no_orders"] = True
            res["reasons"].append("趋势票只在'趋势完好+回调到50日线带或大级别支撑'时上车，不追高")
        _build_orders(res, a, None, mmode)
        return res

    status = plan.get("status", "")
    ez = plan.get("entry_zone")
    if status == "触发中" and ez:
        res["action"] = "进场分批"
        res["reasons"].append(plan.get("status_note", ""))
        res["tranches"] = _tranches(ez["low"], ez["high"])
        if mode == "event":
            res["reasons"].append("事件票：进场前先看简报的新闻判读，确认暴跌是过度反应而非基本面恶化（营收造假/需求崩塌类的跌不接）")
        if de is not None and 0 <= de <= 5:
            res["action"] = "等待"
            res["reasons"].append(f"⚠ 但 {a['earnings_date']} 财报在{de}天内——财报前不开新仓，等落地")
    else:
        res["action"] = "等待"
        res["reasons"].append(plan.get("status_note") or plan.get("reason") or "未触发")
    _build_orders(res, a, None, mmode)
    return res
