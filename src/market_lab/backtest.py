"""抄底分回测 v2：不只回答"多少分进场"，还回答这条线可不可信。

v2 相对 v1 的方法学升级：
- 信号生成与出场模拟解耦：昂贵的区间计算每票只做一次，参数扫描(止损宽度/目标)复用信号
- 大盘状态切片：同样的分数，在大盘多头 vs 走弱时胜率差多少
- 样本前后切片：前半段和后半段结论是否一致(过拟合检验)
- 财报临近切片：财报前5天进场的样本单独看(跳空风险)
- 波动率分层：低/中/高波动票分别统计(WMT 和 IREN 不该用同一套参数)

口径不变：逐日向前走、无未来函数；先+目标算赢、先破无效位算输、超时按收盘结算；
跳空穿越按次日开盘成交。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

from . import data
from .indicators import atr, rsi, sma
from .levels import build_zones, split_zones
from .stock import dip_score

MIN_BARS = 320
TIMEOUT_DAYS = 60


# ---------- 信号生成（昂贵，每票一次） ----------

def generate_signals(ticker: str, period: str = "5y") -> tuple[pd.DataFrame | None, list[dict]]:
    df = data.get_daily(ticker, period)
    if len(df) < MIN_BARS:
        return None, []
    close = df["Close"].values
    rsi_s = rsi(df["Close"]).values
    ma20_s = sma(df["Close"], 20).values
    atr_s = atr(df).values
    high20 = df["High"].rolling(20).max().values

    signals = []
    for i in range(300, len(df) - 1):
        price = float(close[i])
        a = float(atr_s[i]) or 1e-9
        dev = (price - float(ma20_s[i])) / a
        dd = price / float(high20[i]) - 1
        if not (rsi_s[i] < 40 or dev < -1.5 or dd < -0.08):
            continue
        window = df.iloc[: i + 1]
        zones, tol = build_zones(window)
        sup, res, inside = split_zones(zones, price)
        d = dip_score(window, sup, tol)
        if d["score"] < 45:
            continue
        cand = ([inside] if inside else []) + sup
        strong = [z for z in cand if z.score >= 4.0]
        entry_zone = strong[0] if strong else None
        if entry_zone is None or not (entry_zone is inside or price <= entry_zone.high * 1.005):
            continue
        tp1 = tp1_high = None
        for z in sorted(zones, key=lambda zz: zz.mid):
            if z.low > entry_zone.high and z.score >= 3.5 and (z.low / price - 1) >= 0.03:
                tp1 = float(z.low)
                tp1_high = float(z.high)
                break
        signals.append({
            "ticker": ticker, "i": i, "date": str(df.index[i].date()),
            "score": d["score"], "price": price, "atr": a,
            "zone_low": float(entry_zone.low), "zone_high": float(entry_zone.high),
            "atr_pct": a / price, "tp1": tp1, "tp1_high": tp1_high,
        })
    return df, signals


# ---------- 出场模拟（便宜，可参数扫描） ----------

def simulate(df: pd.DataFrame, signals: list[dict], stop_atr: float = 0.5,
             target_pct: float = 10.0, timeout: int = TIMEOUT_DAYS) -> list[dict]:
    close = df["Close"].values
    high = df["High"].values
    low = df["Low"].values
    opn = df["Open"].values
    entries = []
    next_ok = 0
    for s in signals:
        i = s["i"]
        if i < next_ok:
            continue
        e_price = s["price"]
        stop = s["zone_low"] - stop_atr * s["atr"]
        tgt = e_price * (1 + target_pct / 100)
        outcome, ret, days, mae = None, 0.0, 0, 0.0
        for j in range(i + 1, min(i + 1 + timeout, len(df))):
            mae = min(mae, float(low[j]) / e_price - 1)
            if float(low[j]) <= stop:
                fill = min(stop, float(opn[j]))
                outcome, ret, days = "stop", fill / e_price - 1, j - i
                break
            if float(high[j]) >= tgt:
                fill = max(tgt, float(opn[j]))
                outcome, ret, days = "win", fill / e_price - 1, j - i
                break
        if outcome is None:
            j = min(i + timeout, len(df) - 1)
            outcome, ret, days = "timeout", float(close[j]) / e_price - 1, j - i
        entries.append({**s, "outcome": outcome, "ret_pct": round(ret * 100, 2),
                        "days": days, "mae_pct": round(mae * 100, 2)})
        next_ok = i + days + 1
    return entries


# ---------- 统计 ----------

def stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    wins = [e for e in rows if e["outcome"] == "win"]
    return {
        "n": len(rows),
        "win_rate": round(len(wins) / len(rows) * 100, 1),
        "avg_ret_pct": round(sum(e["ret_pct"] for e in rows) / len(rows), 2),
        "avg_days": round(sum(e["days"] for e in rows) / len(rows), 1),
        "avg_mae_pct": round(sum(e["mae_pct"] for e in rows) / len(rows), 2),
    }


def _band(e: dict) -> str:
    if e["score"] >= 70:
        return "70+"
    if e["score"] >= 55:
        return "55-69"
    return "45-54"


# ---------- 上下文：大盘状态 / 财报日期 ----------

def _market_context() -> tuple[pd.Series, pd.Series]:
    spy = data.get_daily("SPY", "5y")["Close"]
    above50 = spy > spy.rolling(50).mean()
    vix = data.get_daily("^VIX", "5y")["Close"]
    return above50, vix


def _earnings_dates(ticker: str) -> list[date]:
    p = data.CACHE_DIR / f"earn_hist_{ticker}.pkl"
    if p.exists():
        return pd.read_pickle(p)
    try:
        from .providers import finnhub
        today = date.today()
        j = finnhub._get(
            "calendar/earnings", symbol=ticker,
            **{"from": (today - timedelta(days=5 * 365)).isoformat(), "to": today.isoformat()},
        )
        dates = sorted({datetime.strptime(e["date"], "%Y-%m-%d").date()
                        for e in j.get("earningsCalendar", []) if e.get("date")})
    except Exception:
        dates = []
    pd.to_pickle(dates, p)
    return dates


def _near_earnings(entry_date: str, edates: list[date], days_before: int = 5) -> bool:
    d = datetime.strptime(entry_date, "%Y-%m-%d").date()
    return any(0 <= (e - d).days <= days_before for e in edates)


# ---------- 全量分析 ----------

def full_analysis(tickers: list[str], period: str = "5y") -> dict:
    frames: dict[str, pd.DataFrame] = {}
    all_signals: dict[str, list[dict]] = {}
    failed = []
    for t in tickers:
        try:
            df, sigs = generate_signals(t, period)
            if df is not None:
                frames[t], all_signals[t] = df, sigs
        except Exception:
            failed.append(t)

    def run_config(stop_atr: float, target_pct: float) -> list[dict]:
        out = []
        for t, sigs in all_signals.items():
            out.extend(simulate(frames[t], sigs, stop_atr, target_pct))
        return out

    base = run_config(0.5, 10.0)

    # 上下文标注
    above50, vix = _market_context()
    edates = {t: _earnings_dates(t) for t in all_signals}
    for e in base:
        d = pd.Timestamp(e["date"])
        try:
            e["mkt_bull"] = bool(above50.asof(d)) and float(vix.asof(d)) < 25
        except Exception:
            e["mkt_bull"] = None
        e["near_earnings"] = _near_earnings(e["date"], edates.get(e["ticker"], []))

    def by_band(rows):
        return {b: stats([e for e in rows if _band(e) == b]) for b in ("45-54", "55-69", "70+")}

    tradable = [e for e in base if e["score"] >= 55]
    dates_sorted = sorted(e["date"] for e in base)
    mid_date = dates_sorted[len(dates_sorted) // 2] if dates_sorted else ""

    result = {
        "n_tickers": len(all_signals), "failed": failed, "n_entries_base": len(base),
        "base_bands": by_band(base),
        "sweep_stop_atr": {
            str(sa): by_band(run_config(sa, 10.0)) for sa in (0.25, 0.5, 0.75, 1.0)
        },
        "sweep_target": {
            str(tp): by_band(run_config(0.5, tp)) for tp in (8.0, 10.0, 15.0)
        },
        "slice_market": {
            "bull": stats([e for e in tradable if e["mkt_bull"] is True]),
            "not_bull": stats([e for e in tradable if e["mkt_bull"] is False]),
            "45-54_bull": stats([e for e in base if _band(e) == "45-54" and e["mkt_bull"] is True]),
            "45-54_not_bull": stats([e for e in base if _band(e) == "45-54" and e["mkt_bull"] is False]),
        },
        "slice_oos": {
            "mid_date": mid_date,
            "first_half": by_band([e for e in base if e["date"] < mid_date]),
            "second_half": by_band([e for e in base if e["date"] >= mid_date]),
        },
        "slice_earnings": {
            "near": stats([e for e in tradable if e["near_earnings"]]),
            "clear": stats([e for e in tradable if not e["near_earnings"]]),
        },
        "slice_volatility": {
            "low(<2.5%)": stats([e for e in tradable if e["atr_pct"] < 0.025]),
            "mid(2.5-5%)": stats([e for e in tradable if 0.025 <= e["atr_pct"] < 0.05]),
            "high(>5%)": stats([e for e in tradable if e["atr_pct"] >= 0.05]),
        },
        "entries": base,
    }
    return result


# ---------- 出场策略锦标赛：同一信号集打擂台 ----------

def _eval_exit(sig: dict, arrs: tuple, *, target: float | None = None,
               chand_k: float | None = None, half_at: float | None = None,
               time_n: int | None = None, timeout: int = TIMEOUT_DAYS) -> tuple[float, int]:
    """通用出场模拟：支持固定目标/吊灯移动止损/减半后追踪/时间止损的任意组合。
    返回 (总收益%, 持有天数)。跳空按开盘成交；同日先查止损(保守口径)。"""
    close, high, low, opn = arrs
    i, e, a = sig["i"], sig["price"], sig["atr"]
    stop0 = sig["zone_low"] - (1.0 if sig["score"] >= 70 else 0.75) * a
    pos, realized, maxc = 1.0, 0.0, e
    end = min(i + timeout, len(close) - 1)
    for j in range(i + 1, end + 1):
        cur_stop = stop0
        if chand_k is not None:
            cur_stop = max(cur_stop, maxc - chand_k * a)
        if float(low[j]) <= cur_stop:
            fill = min(cur_stop, float(opn[j]))
            realized += pos * (fill / e - 1)
            return realized * 100, j - i
        if half_at is not None and pos == 1.0 and float(high[j]) >= half_at:
            fill = max(half_at, float(opn[j]))
            realized += 0.5 * (fill / e - 1)
            pos = 0.5
        if target is not None and pos > 0 and float(high[j]) >= target:
            fill = max(target, float(opn[j]))
            realized += pos * (fill / e - 1)
            return realized * 100, j - i
        maxc = max(maxc, float(close[j]))
        if time_n is not None and j - i >= time_n and pos > 0:
            realized += pos * (float(close[j]) / e - 1)
            return realized * 100, j - i
    realized += pos * (float(close[end]) / e - 1)
    return realized * 100, end - i


def _policy_params(name: str, sig: dict) -> dict:
    e = sig["price"]
    tp1 = sig.get("tp1") or e * 1.10
    tp1_high = sig.get("tp1_high") or e * 1.10
    tp1_mid = (tp1 + tp1_high) / 2 if sig.get("tp1") else e * 1.10
    return {
        "fixed10": {"target": e * 1.10},
        "tp1zone": {"target": tp1},
        "tp1mid": {"target": tp1_mid},
        "tp1top": {"target": tp1_high},
        "tp1mid15": {"target": tp1_mid, "time_n": 15},
        "tp1top15": {"target": tp1_high, "time_n": 15},
        "half_trail": {"half_at": tp1, "chand_k": 2.5},
        "chand20": {"chand_k": 2.0},
        "chand25": {"chand_k": 2.5},
        "chand30": {"chand_k": 3.0},
        "time10": {"target": e * 1.10, "time_n": 10},
        "time15": {"target": e * 1.10, "time_n": 15},
        "time20": {"target": e * 1.10, "time_n": 20},
    }[name]


EXIT_POLICIES = ["fixed10", "tp1zone", "tp1mid", "tp1top", "tp1mid15", "tp1top15",
                 "half_trail", "chand20", "chand25", "chand30",
                 "time10", "time15", "time20"]


def exit_tournament(tickers: list[str], period: str = "5y", min_score: float = 55.0) -> dict:
    results: dict[str, list] = {p: [] for p in EXIT_POLICIES}
    for t in tickers:
        try:
            df, sigs = generate_signals(t, period)
        except Exception:
            continue
        if df is None:
            continue
        arrs = (df["Close"].values, df["High"].values, df["Low"].values, df["Open"].values)
        sigs = [s for s in sigs if s["score"] >= min_score]
        for p in EXIT_POLICIES:
            next_ok = 0
            for s in sigs:
                if s["i"] < next_ok:
                    continue
                ret, days = _eval_exit(s, arrs, **_policy_params(p, s))
                results[p].append({"date": s["date"], "ret": ret, "days": days})
                next_ok = s["i"] + days + 1

    def stats(rows):
        if not rows:
            return {"n": 0}
        n = len(rows)
        avg = sum(r["ret"] for r in rows) / n
        days = sum(r["days"] for r in rows) / n
        return {
            "n": n,
            "win_rate": round(sum(r["ret"] > 0 for r in rows) / n * 100, 1),
            "avg_ret_pct": round(avg, 2),
            "avg_days": round(days, 1),
            "ret_per_day": round(avg / max(days, 0.1), 3),
        }

    out = {}
    for p, rows in results.items():
        dates = sorted(r["date"] for r in rows)
        mid = dates[len(dates) // 2] if dates else ""
        out[p] = {
            "overall": stats(rows),
            "first_half": stats([r for r in rows if r["date"] < mid]),
            "second_half": stats([r for r in rows if r["date"] >= mid]),
        }
    return out


# v1 兼容入口
def run(tickers: list[str], period: str = "5y") -> dict:
    return full_analysis(tickers, period)
