"""交易计划：把"在哪进、错了怎么办、拿到哪"三件事量化成参考区间。

围绕用户的抄底策略：大跌进场。止盈以数据为主——TP 取自进场位上方的强区间
（卖在区间下沿，不赌穿越），用户偏好"至少+10%"作为参照线标注，不是硬目标。
无效位是"跌破说明抄错了"的位置。输出是计划参考，不是指令。
"""
from __future__ import annotations

PREF_MIN_PCT = 10.0    # 用户偏好：盈利至少10%（参照线）
TRIGGER_DIP = 55       # 进场分数线。5年回测(655样本)：45-54段胜率37.7%期望≈0；55-69段胜率52.5%均+2.85%/笔；70+段49%并不更优
MIN_ENTRY_SCORE = 4.0  # 只把足够强的区间当进场依托
MIN_TP_SCORE = 3.5     # 止盈参考的区间最低强度
MIN_TP_PCT = 3.0       # 距进场不足3%的区间算进场区的延伸，不算止盈位（否则盈亏比被紧邻区间系统性压低）


def trade_plan(a: dict) -> dict:
    """输入 stock.analyze() 的结果 dict，输出交易计划。"""
    price = a["price"]
    atr_v = a["atr"]
    supports = a["supports"]
    inside = a.get("inside_zone")
    dip = a["dip"]["score"]

    # 进场候选：现价所在区间优先（价格已经在里面就是"到位"，
    # 否则价格跌入等待中的进场区时，该区变成 inside_zone 从支撑列表消失，计划会倒退到更低一档）
    candidates = ([inside] if inside else []) + supports
    strong = [z for z in candidates if z["score"] >= MIN_ENTRY_SCORE]
    entry = strong[0] if strong else (candidates[0] if candidates else None)
    entry_is_strong = bool(strong)
    if entry is None:
        return {"status": "无计划", "reason": "现价附近和下方都没有可依托的区间，跌起来没有参照，不适合抄底"}

    entry_mid = entry["mid"]
    # 止损宽度来自回测扫描：0.5ATR会被噪音打掉(55-69段胜率52.5%)；0.75ATR升到57.1%；
    # 70+深恐慌段用1.0ATR最优(67.8%)——恐慌越深，噪音越大，止损要越宽
    stop_atr = 1.0 if dip >= 70 else 0.75
    invalid = round(entry["low"] - stop_atr * atr_v, 2)
    below = [z for z in supports if z["mid"] < entry_mid - 1e-9]
    next_sup = below[0] if below else None

    # 止盈候选：进场区上方的强区间，去掉互相重叠的（重叠时保留更低的那个）
    ladder = list(supports) + list(a["resistances"])
    if inside is not None and inside is not entry:
        ladder.append(inside)
    raw = sorted(
        (
            z for z in ladder
            if z["low"] > entry["high"]
            and z["score"] >= MIN_TP_SCORE
            and (z["low"] / entry_mid - 1) * 100 >= MIN_TP_PCT
        ),
        key=lambda z: z["mid"],
    )
    tp_ladder: list[dict] = []
    for z in raw:
        if tp_ladder and z["low"] <= tp_ladder[-1]["high"]:
            continue
        tp_ladder.append(z)

    def _tp(z: dict) -> dict:
        mid = round((z["low"] + z["high"]) / 2, 2)
        return {
            "low": z["low"], "high": z["high"], "mid": mid, "score": z["score"],
            # 卖在区间中部(2026-09-01回测校准)：反转前扎入压力区的中位深度0.83，
            # 卖下沿被证实过于保守(每笔期望1.28% vs 固定+10%的2.00%)；中部是兼顾成交率的半步
            "pct_from_entry": round((mid / entry_mid - 1) * 100, 1),
        }

    # TP1 = 路上第一道强压力；TP2 = +10% 之外最近的强区（有的话），而不是盲取第二近的
    beyond = next((t for t in tp_ladder if (t["low"] / entry_mid - 1) * 100 >= PREF_MIN_PCT), None)
    targets: list[dict] = []
    if tp_ladder:
        targets.append(_tp(tp_ladder[0]))
        if beyond is not None and beyond is not tp_ladder[0]:
            targets.append(_tp(beyond))
        elif len(tp_ladder) > 1:
            targets.append(_tp(tp_ladder[1]))

    fallback_target = round(entry_mid * (1 + PREF_MIN_PCT / 100), 2)
    if not targets:
        target_note = f"进场位上方近处没有强区间（如接近新高），用 +{PREF_MIN_PCT:.0f}% 作基准目标，涨起来按新形成的位置再评估"
        reward_ref = fallback_target
    else:
        if targets[0]["pct_from_entry"] >= PREF_MIN_PCT:
            reward_ref = targets[0]["mid"]
            target_note = f"TP1 就在 +{PREF_MIN_PCT:.0f}% 之外，数据目标与你的偏好一致"
        elif beyond is not None:
            # 简报建议"TP1减一部分、剩余看TP2"，盈亏比就按实际建议的持有目标(TP2)算
            reward_ref = round((beyond["low"] + beyond["high"]) / 2, 2)
            target_note = f"TP1 不足 +{PREF_MIN_PCT:.0f}%：参考在 TP1 先减一部分，剩余看 TP2（+{PREF_MIN_PCT:.0f}% 之外最近的强区）"
        else:
            reward_ref = targets[0]["mid"]
            target_note = f"已识别的上方强区间都在 +{PREF_MIN_PCT:.0f}% 以内，想拿满需要突破配合——TP1 的抛压是第一道坎"

    in_entry_zone = (entry is inside) or price <= entry["high"] * 1.005
    if in_entry_zone and dip >= TRIGGER_DIP:
        status = "触发中"
        status_note = f"价格已到进场参考区，抄底分 {dip:.0f} 过进场线({TRIGGER_DIP})——5年回测该分段胜率52.5%、平均+2.85%/笔"
    elif in_entry_zone and dip >= 45:
        status = "到位但不够狠"
        status_note = f"价格到位但抄底分 {dip:.0f} 落在45-54段——回测该段胜率仅37.7%、期望≈0，等55再动手"
    elif in_entry_zone:
        status = "到位但不够狠"
        status_note = "价格在进场区附近，但抄底分不高——阴跌到支撑和恐慌砸到支撑是两回事，等情绪信号"
    else:
        dist_pct = (1 - entry["high"] / price) * 100
        status = "等待"
        status_note = f"现价距进场区上沿还有 {dist_pct:.1f}%，跌下来才有机会"
    if not entry_is_strong:
        status_note += "；注意：这是弱区间降级充当的进场参考，依托有限，仓位要更保守"
    if atr_v / price < 0.025:
        status_note += "；⚠ 低波动票：回测中该类票+10%目标胜率仅30%、平均要拿16天——目标预期要打折"

    risk = entry_mid - invalid
    rr = round((reward_ref - entry_mid) / risk, 1) if risk > 0 else None

    return {
        "status": status,
        "status_note": status_note,
        "entry_zone": {"low": entry["low"], "high": entry["high"], "score": entry["score"]},
        "entry_is_strong": entry_is_strong,
        "invalid_below": invalid,
        "if_invalid_next_support": (
            {"low": next_sup["low"], "high": next_sup["high"]} if next_sup else None
        ),
        "targets": targets,
        "target_note": target_note,
        "fallback_target": fallback_target if not targets else None,
        "pref_min_pct": PREF_MIN_PCT,
        "risk_reward": rr,
    }
