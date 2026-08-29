"""仓位管理引擎：R体系单笔风险预算 + 组合层约束 + 连败熔断。

目标不是"期望利润最大化"（那条路通向满Kelly和破产分支），而是最大化长期复利
增速且任何连败都活得下来。所有计算用账户百分比完成（云端可用）；
本地存在 account.yaml (gitignored) 时额外换算股数和金额。

规则：
- 单笔风险 = 账户的 RISK_PER_TRADE%（55-69分触发×0.75，70+分×1.0，quarter-Kelly封顶思想）
- 仓位% = 单笔风险% ÷ 到止损的距离%，上限 MAX_SINGLE_POS%
- 同相关性簇合计 ≤ CLUSTER_CAP%（clusters.yaml 定义），总开放风险 ≤ TOTAL_OPEN_RISK_CAP%
- 回撤熔断：账户 < 高水位×0.92 时单笔风险减半
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]

RISK_PER_TRADE_PCT = 1.0
MAX_SINGLE_POS_PCT = 10.0
CLUSTER_CAP_PCT = 40.0
TOTAL_OPEN_RISK_CAP_PCT = 8.0
DD_THROTTLE_RATIO = 0.92


def load_account() -> dict | None:
    p = ROOT / "account.yaml"
    if not p.exists():
        return None
    try:
        acc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if acc.get("total_value") and not acc.get("high_water"):
            acc["high_water"] = acc["total_value"]
        return acc
    except Exception:
        return None


def _load_clusters() -> dict[str, str]:
    p = ROOT / "clusters.yaml"
    out: dict[str, str] = {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        for cname, tickers in data.items():
            for t in tickers or []:
                out[str(t).upper()] = cname
    except Exception:
        pass
    return out


def apply_sizing(ok_stocks: list[dict], wl_by_symbol: dict[str, dict]) -> dict:
    """给操作单里的挂单标定仓位%（及本地可用时的股数/金额），返回风险仪表盘。

    直接修改各 stock["advice"]["orders"] 元素：加 pct（占账户%）、shares、usd 字段。
    """
    acc = load_account()
    total = (acc or {}).get("total_value")
    throttled = bool(acc and acc.get("high_water") and total and total < acc["high_water"] * DD_THROTTLE_RATIO)
    risk_base = RISK_PER_TRADE_PCT * (0.5 if throttled else 1.0)

    clusters = _load_clusters()
    pos_pct: dict[str, float] = {}
    for t, item in wl_by_symbol.items():
        if item.get("cost") is not None and item.get("pct"):
            pos_pct[t] = float(item["pct"])

    cluster_sums: dict[str, float] = {}
    for t, pct in pos_pct.items():
        c = clusters.get(t, "其他")
        cluster_sums[c] = round(cluster_sums.get(c, 0) + pct, 1)

    # 总开放风险：每只持仓到其止损单价位的潜在亏损合计
    open_risk = 0.0
    for s in ok_stocks:
        t = s["ticker"]
        if t not in pos_pct:
            continue
        stop = next((o["price"] for o in s["advice"].get("orders", []) if o["type"] in ("止损",)), None)
        if stop and s["price"] > 0:
            open_risk += pos_pct[t] * max(0.0, (s["price"] - stop) / s["price"])
    open_risk = round(open_risk, 2)

    warnings: list[str] = []
    if throttled:
        warnings.append(f"⛔ 连败熔断生效：账户较高水位回撤超8%，单笔风险已减半至 {risk_base:.1f}%")
    for c, v in cluster_sums.items():
        if v > CLUSTER_CAP_PCT:
            warnings.append(f"⚠ 「{c}」簇持仓合计 {v}% 已超上限 {CLUSTER_CAP_PCT:.0f}%——该簇新单建议跳过，反弹时优先减该簇")
    if open_risk > TOTAL_OPEN_RISK_CAP_PCT:
        warnings.append(f"⚠ 总开放风险 {open_risk}% 超上限 {TOTAL_OPEN_RISK_CAP_PCT:.0f}%——收紧部分止损或减仓，别再开新仓")

    # 给挂单标定仓位
    for s in ok_stocks:
        adv = s["advice"]
        orders = adv.get("orders") or []
        if not orders:
            continue
        t = s["ticker"]
        price = s["price"]
        dip = s["dip"]["score"]
        plan = s.get("plan") or {}
        inv = plan.get("invalid_below")
        c = clusters.get(t, "其他")

        buys = [o for o in orders if o["side"] == "买" and o["type"] == "限价"]
        if buys and t not in pos_pct:  # 新进场：算总仓位再分摊到各档
            risk = risk_base * (1.0 if dip >= 70 else 0.75)
            entry_ref = sum(o["price"] for o in buys) / len(buys)
            stop_ref = inv if inv else entry_ref * 0.93
            dist = max((entry_ref - stop_ref) / entry_ref, 0.02)
            total_pct = min(risk / dist, MAX_SINGLE_POS_PCT)
            # 簇约束：超限则减半并警告
            room = CLUSTER_CAP_PCT - cluster_sums.get(c, 0)
            if room <= 0:
                total_pct = 0
                adv["reasons"].append(f"⚠ 「{c}」簇已满（{cluster_sums.get(c, 0)}%），本单被组合约束否决——要么跳过，要么先减该簇旧仓")
            elif total_pct > room:
                total_pct = room
                adv["reasons"].append(f"⚠ 「{c}」簇仅剩 {room:.1f}% 额度，本单已按余额缩减")
            # 开放风险约束
            if open_risk >= TOTAL_OPEN_RISK_CAP_PCT and total_pct > 0:
                total_pct = 0
                adv["reasons"].append(f"⚠ 总开放风险已达 {open_risk}%，本单暂停——先等旧仓了结释放预算")
            each = round(total_pct / len(buys), 1)
            for o in buys:
                o["pct"] = each
                if total and each:
                    o["usd"] = int(total * each / 100)
                    o["shares"] = max(1, int(total * each / 100 / o["price"])) if o["price"] else None
        else:  # 持仓的卖单：份额换算成账户%
            held = pos_pct.get(t)
            for o in orders:
                if o["side"] != "卖" or held is None:
                    continue
                frac = 1.0 if o["portion"] in ("全部", "剩余") else (1 / 3 if "1/3" in o["portion"] else None)
                if frac:
                    o["pct"] = round(held * frac, 1)
                    if total:
                        o["usd"] = int(total * o["pct"] / 100)

    dashboard = {
        "open_risk_pct": open_risk,
        "open_risk_cap": TOTAL_OPEN_RISK_CAP_PCT,
        "cash_pct": round(max(0.0, 100 - sum(pos_pct.values())), 1) if pos_pct else None,
        "positions_pct": dict(sorted(pos_pct.items(), key=lambda kv: -kv[1])),
        "cluster_sums": dict(sorted(cluster_sums.items(), key=lambda kv: -kv[1])),
        "cluster_cap": CLUSTER_CAP_PCT,
        "risk_per_trade": risk_base,
        "throttled": throttled,
        "has_account_file": acc is not None,
        "warnings": warnings,
    }
    return dashboard
