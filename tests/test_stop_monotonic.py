"""持仓止损单调性回归测试。

守的是 2026-09-15 事故：止损随价格破位逐档下移，变成"越跌越扛"
(LITE 864→759 / CRWV 82→72 / NBIS 211→184)。2026-10-10 我在修 WDC 的
"止损远达 4.19 ATR"时用【当天的】supports 当锚点，把同一个机制又引了回来——
价格跌穿一档支撑后该档从 split_zones 的 supports 里消失，锚点自动换下一档，
止损跟着下移。对抗性复核复现出 350.09 → 325.84（下移 0.90 ATR）。

这个文件就是那次复核要求补上的断言：**跌一天之后发出的止损不能比前一天低。**
"""
from __future__ import annotations

import copy

import pytest

from market_lab.advice import advise


def _stop_of(res: dict) -> float | None:
    return next((o["price"] for o in res.get("orders") or [] if o["type"] == "止损"), None)


def _drop_price(a: dict, new_price: float) -> dict:
    """把分析结果搬到一个更低的价格上：重新按 split_zones 的口径切 supports/resistances。

    这正是第二天真实发生的事——不是模拟，是复刻 levels.split_zones 的过滤规则
    (supports = z.high < price)，所以原本当锚的那档会掉出 supports。
    """
    b = copy.deepcopy(a)
    b["price"] = new_price
    zones = (a.get("supports") or []) + ([a["inside_zone"]] if a.get("inside_zone") else []) + (a.get("resistances") or [])
    b["supports"] = sorted([z for z in zones if z["high"] < new_price], key=lambda z: -z["mid"])
    b["resistances"] = sorted([z for z in zones if z["low"] > new_price], key=lambda z: z["mid"])
    b["inside_zone"] = next((z for z in zones if z["low"] <= new_price <= z["high"]), None)
    return b


@pytest.mark.parametrize("style", ["hybrid", "single", "tranche"])
def test_holding_stop_never_drops_when_price_falls(style):
    """核心不变量：价格下跌且止损未被触发时，新发出的止损 >= 旧止损。"""
    a = {
        "ticker": "WDC", "price": 397.28, "atr": 26.58, "rsi": 50.0,
        "dip": {"score": 33.0, "band": "不在抄底区", "reasons": []},
        "trend": "震荡", "ma": {50: {"value": 390.0, "above": True}},
        "below_ma50_streak": 0, "trail_stop": None, "earnings_date": None,
        "supports": [
            {"low": 370.03, "high": 383.99, "mid": 377.01, "score": 3.7},
            {"low": 346.01, "high": 353.99, "mid": 350.00, "score": 2.9},
            {"low": 305.91, "high": 328.99, "mid": 317.45, "score": 4.8},
        ],
        "resistances": [{"low": 401.91, "high": 411.46, "mid": 406.69, "score": 3.5}],
        "inside_zone": None,
        "plan": {
            "status": "等待", "invalid_below": 285.98,
            "entry_zone": {"low": 305.91, "high": 328.99, "score": 4.8},
            "targets": [{"low": 401.91, "high": 411.46, "mid": 406.69, "score": 3.5, "pct_from_entry": 26.6}],
            "if_invalid_next_support": None,
        },
    }
    # 不填 since：holding_stop_asof 需要联网取数，单测里不该依赖网络。
    # 没有 since 时走的是原有的进场区锚定路径，照样能检出"随价下移"这个形态。
    item = {"symbol": "WDC", "cost": 388.0, "pct": 3.12}

    r1 = advise(copy.deepcopy(a), item, "bull", style)
    s1 = _stop_of(r1)

    # 第二天跌到 369：370.03-383.99 那档掉出 supports（它现在在现价上方）
    b = _drop_price(a, 369.0)
    assert all(z["high"] < 369.0 for z in b["supports"]), "测试前置条件：锚点那档必须已掉出 supports"
    r2 = advise(b, item, "bull", style)
    s2 = _stop_of(r2)

    if s1 is None or s2 is None:
        pytest.skip(f"style={style} 未产出止损单（动作 {r1['action']}→{r2['action']}），本测试不适用")
    assert s2 >= s1, (
        f"止损下移了：{s1} → {s2}（价格 397.28 → 369，止损从未触发）。"
        f"这是 2026-09-15 事故的形态，止损只能升不能降。"
    )


def test_take_profit_never_fires_below_cost():
    """止盈不能在亏损/微利仓位上触发——MU(-1.2%) 和 WDC(+2.4%) 曾被判"止盈减仓"并因此丢掉止损单。"""
    a = {
        "ticker": "MU", "price": 1029.0, "atr": 45.97, "rsi": 55.0,
        "dip": {"score": 0.0, "band": "不在抄底区", "reasons": []},
        "trend": "多头排列", "ma": {50: {"value": 980.0, "above": True}},
        "below_ma50_streak": 0, "trail_stop": None, "earnings_date": None,
        "supports": [{"low": 993.1, "high": 1018.67, "mid": 1005.88, "score": 3.7}],
        "resistances": [], "inside_zone": None,
        "plan": {
            "status": "等待", "invalid_below": 993.59,
            "entry_zone": {"low": 880.71, "high": 909.5, "score": 5.2},
            # 两档 TP 全在成本 1042 以下——这是 bug 的触发条件
            "targets": [
                {"low": 993.1, "high": 1018.67, "mid": 1005.88, "score": 3.7, "pct_from_entry": 12.4},
                {"low": 1025.2, "high": 1049.3, "mid": 1037.25, "score": 3.2, "pct_from_entry": 15.9},
            ],
            "if_invalid_next_support": None,
        },
    }
    item = {"symbol": "MU", "cost": 1042.0, "pct": 2.02}
    for style in ("hybrid", "single", "oco", "tranche"):
        r = advise(copy.deepcopy(a), item, "bull", style)
        assert r["action"] != "止盈减仓", f"style={style}: 在 -1.2% 的仓位上触发了止盈"
        assert _stop_of(r) is not None, f"style={style}: 持仓没有止损单"


def test_hybrid_take_profit_uses_cost_times_1_10():
    """hybrid/single/oco 的止盈线必须是成本×1.10，而不是某个侥幸高于成本的压力区下沿。"""
    a = {
        "ticker": "WDC", "price": 402.0, "atr": 26.58, "rsi": 60.0,
        "dip": {"score": 20.0, "band": "不在抄底区", "reasons": []},
        "trend": "震荡", "ma": {50: {"value": 390.0, "above": True}},
        "below_ma50_streak": 0, "trail_stop": None, "earnings_date": None,
        "supports": [{"low": 370.03, "high": 383.99, "mid": 377.01, "score": 3.7}],
        "resistances": [], "inside_zone": None,
        "plan": {
            "status": "等待", "invalid_below": 350.09,
            "entry_zone": {"low": 305.91, "high": 328.99, "score": 4.8},
            # 401.91 只比成本 388 高 3.6%，远低于 426.8(=388×1.10)
            "targets": [{"low": 401.91, "high": 411.46, "mid": 406.69, "score": 3.5, "pct_from_entry": 26.6}],
            "if_invalid_next_support": None,
        },
    }
    item = {"symbol": "WDC", "cost": 388.0, "pct": 3.12}
    r = advise(copy.deepcopy(a), item, "bull", "hybrid")
    assert r["action"] == "持有", f"在 +3.6% 就触发了止盈（应等成本×1.10=426.8），实际动作 {r['action']}"
    assert _stop_of(r) is not None, "止损单被丢掉了"

    # 价格真到 426.8 时才该兑现
    a2 = copy.deepcopy(a); a2["price"] = 427.0
    r2 = advise(a2, item, "bull", "hybrid")
    assert r2["action"] == "止盈减仓", f"到了成本×1.10 却没触发止盈，动作 {r2['action']}"


def test_ratchet_is_monotonic_across_days(monkeypatch):
    """止损棘轮的核心不变量：持仓期逐日取 max，所以随着天数推进只能升不能降。

    守的是 2026-10-10 对抗性复核发现的回归：旧实现只比较"买入日"那一天，
    止损先随价格上行抬高、再随价格下行跌回买入日常量（350.09 → 325.84），
    而止损单从未被触发。形态与 2026-09-15 的 LITE/CRWV/NBIS 事故一致。
    """
    import pandas as pd

    import market_lab.data as data_mod
    import market_lab.plan as plan_mod

    # 结构止损逐日的真实走势：先升后降（价格冲高回落时就是这个形状）
    curve = {
        "2026-10-05": 300.0,
        "2026-10-06": 320.0,
        "2026-10-07": 350.0,   # 高点
        "2026-10-08": 325.0,   # 开始回落
        "2026-10-09": 318.0,
        "2026-10-12": 305.0,
    }
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in curve])
    monkeypatch.setattr(data_mod, "get_daily", lambda t, period="2y": pd.DataFrame(index=idx))
    monkeypatch.setattr(plan_mod, "stop_asof", lambda t, d: curve.get(str(d)[:10]))

    seen = []
    for today in list(curve):
        # 截到"今天"，模拟那一天运行报告时能看到的窗口
        cut = pd.DatetimeIndex([d for d in idx if d <= pd.Timestamp(today)])
        monkeypatch.setattr(data_mod, "get_daily", lambda t, period="2y", _c=cut: pd.DataFrame(index=_c))
        hwm = plan_mod._stop_hwm("WDC", "2026-10-05")
        seen.append((today, hwm))

    vals = [v for _, v in seen]
    assert vals == sorted(vals), f"棘轮下移了：{seen}"
    assert vals[-1] == 350.0, f"回落后没有守住历史最高止损 350.0，实际 {vals[-1]}（{seen}）"


def test_ratchet_raises_plan_invalid(monkeypatch):
    """ratchet_invalid 必须把 plan.invalid_below 抬到历史最高位，并标记 ratcheted。"""
    import market_lab.plan as plan_mod

    monkeypatch.setattr(plan_mod, "_stop_hwm", lambda t, s: 350.0)
    plan = {"invalid_below": 285.98}
    rk = plan_mod.ratchet_invalid(plan, "WDC", "2026-10-09")
    assert rk["ratcheted"] is True
    assert rk["since_stop"] == 350.0
    assert plan["invalid_below"] == 350.0, plan

    # 今日位已经更高时不该"降"回去
    monkeypatch.setattr(plan_mod, "_stop_hwm", lambda t, s: 300.0)
    plan2 = {"invalid_below": 360.0}
    rk2 = plan_mod.ratchet_invalid(plan2, "WDC", "2026-10-09")
    assert plan2["invalid_below"] == 360.0, plan2
    assert rk2["ratcheted"] is False
