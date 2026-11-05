"""季度复测：用没参与调参的新数据检验抄底分和高位分。只报告，不改公式、不改推送里的历史统计。

公式和那些统计都来自 2026-10 的无偏研究，数据截至 2026-10-15，所以这之后的数据是真正的样本外。

股票池是 research/retest_universe.txt：2026-10-15 当天全部流动股，此后固定不变，不会因为"后来谁涨得好"产生选择偏差。
数据走 Yahoo（免费，云端可用）。Yahoo 会删掉已退市的票，拿不到数据的只数会写进报告。还能拿到、但已停止交易的票，
按研究的口径回收：最后收盘 <$1 或不到前 20 日最高价的一半，按破产算 0；否则按最后收盘（多半是被收购）。

口径同研究：t+1 开盘买、t+h 收盘卖；超额 = 个股收益 − 同日全部成员的等权平均；主口径按日等权（先每天内平均再跨天平均）；
区间是循环块 bootstrap（5/10 日用 21 日块，20/60 日用 63 日块）。不含分红和交易成本。
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import data
from .indicators import atr, rsi, sma
from .levels import build_zones, split_zones
from .stock import HIGH_52W_BARS, dip_score

ROOT = Path(__file__).resolve().parents[2]
UNIVERSE_FILE = ROOT / "research" / "retest_universe.txt"
OUT_DIR = ROOT / "research" / "retest"
START = "2026-10-16"             # 研究数据到 2026-10-15，这之后才算样本外
HISTORY_FROM = "2024-10-01"      # 早两年开始取，和生产一样每个时点都有约 2 年日线
HORIZONS = (5, 10, 20, 60)
MIN_DAYS = 40                    # 少于这么多个交易日，只报告数字，不下结论
MIN_DAYS_CHANGE = 120            # 少于这么多个交易日，再好也不提议改公式
SEED = 20261016

# 研究里的数字（样本外 2024-04-16 至 2026-10-15，按日等权）：拿来和新数据对照，不参与计算
RESEARCH = {
    "dip55_10": -0.13, "dip55_20": -0.48, "dip55_60": -1.57,
    "high55_10": -0.42, "high55_20": -0.52, "high55_60": -1.27,
    "dip_mdd10": -6.0,
}


def load_universe() -> list[str]:
    return [ln.strip() for ln in UNIVERSE_FILE.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")]


def fetch_daily(ticker: str, start: str = HISTORY_FROM) -> pd.DataFrame | None:
    """这段时间的已收盘日线（不走缓存）。取不到返回 None。"""
    try:
        j = data._yahoo_get(ticker, {"period1": int(datetime.fromisoformat(start).timestamp()),
                                     "period2": int(time.time()), "interval": "1d"})
        q = j["indicators"]["quote"][0]
        df = pd.DataFrame({"Open": q["open"], "High": q["high"], "Low": q["low"], "Close": q["close"],
                           "Volume": q["volume"]},
                          index=pd.to_datetime(j["timestamp"], unit="s").normalize()).dropna(subset=["Close"])
    except Exception:
        return None
    df = data._drop_partial_bar(df[~df.index.duplicated(keep="last")])
    return df if len(df) else None


def _pick(conds, values):
    return np.select(conds, values, default=0)


def dip_base(df: pd.DataFrame) -> pd.Series:
    """抄底分里除"贴近强支撑"以外的部分（向量化，和 stock.dip_score 同一口径，tests 里逐行对照）。"""
    c = df["Close"]
    a = atr(df)
    r = rsi(c)
    dev = ((c - sma(c, 20)) / a).where(a != 0, 0.0)
    dd = (c / df["High"].rolling(20).max() - 1) * 100
    vr = df["Volume"] / df["Volume"].rolling(20).mean()
    pts = (_pick([r < 25, r < 32, r < 40], [30, 22, 10]) + _pick([dev < -3, dev < -2, dev < -1], [25, 18, 8])
           + _pick([dd < -15, dd < -8], [15, 10]) + np.where((vr > 1.8) & (c < c.shift(1)), 10, 0))
    return pd.Series(pts, index=df.index, dtype=float)


def high_points(df: pd.DataFrame) -> pd.Series:
    """高位分（向量化，和 stock.high_score 同一口径，tests 里逐行对照）。"""
    c = df["Close"]
    a = atr(df)
    r = rsi(c)
    dev = ((c - sma(c, 20)) / a).where(a != 0, 0.0)
    run = c / df["Low"].rolling(20).min() - 1
    vr = df["Volume"] / df["Volume"].rolling(20).mean()
    d52 = c / df["High"].rolling(HIGH_52W_BARS).max() - 1
    pts = (_pick([r > 75, r > 68, r > 60], [30, 22, 10]) + _pick([dev > 3, dev > 2, dev > 1], [25, 18, 8])
           + _pick([run > 0.15, run > 0.08], [15, 10]) + np.where((vr > 1.8) & (c > c.shift(1)), 10, 0)
           + _pick([d52 >= -0.02, d52 >= -0.05], [20, 10]))
    return pd.Series(np.minimum(pts, 100), index=df.index, dtype=float)


def forward_returns(df: pd.DataFrame, last_date: pd.Timestamp) -> dict[str, np.ndarray]:
    """t+1 开盘买、t+h 收盘卖的收益（%），以及买入后 10 天内相对买入价的最大浮亏（%）。

    窗口超出数据末尾：还在交易的票记 NaN（结果还没出来）；已停止交易的票按回收价算，不能丢，否则正好漏掉暴跌的。
    """
    o, h, l, c = (df[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))
    n = len(c)
    stale = (last_date - df.index[-1]).days > 5
    recovery = np.nan
    if stale:
        recent_high = np.nanmax(h[-20:])
        recovery = 0.0 if (c[-1] < 1 or c[-1] < 0.5 * recent_high) else c[-1]
    out = {}
    for hz in HORIZONS:
        r = np.full(n, np.nan)
        for i in range(n - 1):
            entry = o[i + 1]
            if not entry > 0:
                continue
            if i + hz < n:
                r[i] = (c[i + hz] / entry - 1) * 100
            elif stale:
                r[i] = (recovery / entry - 1) * 100
        out[f"ret{hz}"] = r
    m = np.full(n, np.nan)
    for i in range(n - 1):
        entry = o[i + 1]
        if not entry > 0:
            continue
        if i + 10 < n:
            m[i] = (np.nanmin(l[i + 1:i + 11]) / entry - 1) * 100
        elif stale:
            m[i] = (min(np.nanmin(l[i + 1:]), recovery) / entry - 1) * 100
    out["mdd10"] = np.minimum(m, 0.0)
    return out


def score_ticker(args: tuple[str, pd.DataFrame, str]) -> pd.DataFrame | None:
    """一只票在样本外每天的分数和结果。分数低到支撑项也救不了的日子不算支撑区，其余和生产一样调 dip_score。"""
    ticker, df, last = args
    if len(df) < 60:
        return None
    start = pd.Timestamp(START)
    base = dip_base(df)
    high = high_points(df)
    fwd = forward_returns(df, pd.Timestamp(last))
    c = df["Close"]
    dvol = (c * df["Volume"]).rolling(20).mean()
    member = (c.shift(1) >= 5) & (dvol.shift(1) >= 20e6)     # t 日可知：用 t−1 及以前的价格和成交额
    dip = base.copy()
    for i in np.flatnonzero((df.index >= start) & (base.to_numpy() >= 25)):
        sub = df.iloc[max(0, i - 503):i + 1]
        zones, tol = build_zones(sub)
        sup, _, _ = split_zones(zones, float(sub["Close"].iloc[-1]))
        dip.iloc[i] = dip_score(sub, sup, tol)["score"]
    keep = df.index >= start
    if not keep.any():
        return None
    out = pd.DataFrame({"date": df.index[keep], "ticker": ticker, "member": member.to_numpy()[keep],
                        "dip": dip.to_numpy()[keep], "base": base.to_numpy()[keep], "high": high.to_numpy()[keep]})
    for k, v in fwd.items():
        out[k] = v[keep]
    return out


def _block_ci(series: np.ndarray, block: int) -> tuple[float, float] | None:
    n = len(series)
    if n < 20:
        return None
    block = min(block, max(5, n // 3))
    rng = np.random.default_rng(SEED)
    starts = rng.integers(0, n, size=(2000, n // block + 1))
    idx = (starts[..., None] + np.arange(block)) % n
    means = series[idx.reshape(2000, -1)[:, :n]].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(lo), float(hi)


def _block_for(col: str) -> int:
    hz = int("".join(ch for ch in col if ch.isdigit()) or 10)
    return 21 if hz <= 10 else 63


def summarize(rows: pd.DataFrame, mask, col: str) -> dict:
    """按日等权的均值和区间，附按事件平均。"""
    sub = rows.loc[mask & rows[col].notna(), ["date", col]]
    if sub.empty:
        return {"mean": None, "lo": None, "hi": None, "days": 0, "events": 0, "event_mean": None}
    daily = sub.groupby("date")[col].mean().to_numpy()
    ci = _block_ci(daily, _block_for(col))
    return {"mean": round(float(daily.mean()), 2), "lo": round(ci[0], 2) if ci else None,
            "hi": round(ci[1], 2) if ci else None, "days": len(daily), "events": len(sub),
            "event_mean": round(float(sub[col].mean()), 2)}


def paired(rows: pd.DataFrame, mask_a, mask_b, col: str) -> dict:
    """同一天 A 组减 B 组，只算两组都有票的日子。"""
    a = rows.loc[mask_a & rows[col].notna()].groupby("date")[col].mean()
    b = rows.loc[mask_b & rows[col].notna()].groupby("date")[col].mean()
    d = (a - b).dropna().to_numpy()
    if not len(d):
        return {"mean": None, "lo": None, "hi": None, "days": 0}
    ci = _block_ci(d, _block_for(col))
    return {"mean": round(float(d.mean()), 2), "lo": round(ci[0], 2) if ci else None,
            "hi": round(ci[1], 2) if ci else None, "days": len(d)}


def add_excess(rows: pd.DataFrame) -> pd.DataFrame:
    """只留当天的成员，算相对同日全部成员等权平均的超额。

    窗口还没走完的日子一律清空。已停止交易的票按回收价已经有"结果"，不清的话，
    超额就成了它们和自己比，报告里会出现一行没有意义的 +0.00。
    """
    rows = rows[rows["member"]].copy()
    cal = pd.Index(sorted(rows["date"].unique()))
    pos = cal.get_indexer(rows["date"])
    for hz in HORIZONS:
        rows.loc[pos + hz >= len(cal), f"ret{hz}"] = np.nan
        rows[f"exc{hz}"] = rows[f"ret{hz}"] - rows.groupby("date")[f"ret{hz}"].transform("mean")
    rows.loc[pos + 10 >= len(cal), "mdd10"] = np.nan
    return rows


def verdict(x: dict) -> str:
    if x["days"] < MIN_DAYS or x["lo"] is None:
        return "样本还太小"
    if x["lo"] > 0:
        return "新数据里明显跑赢"
    if x["hi"] < 0:
        return "新数据里明显跑输"
    return "和随机股没有可靠差别"


def analyze_rows(rows: pd.DataFrame) -> dict:
    d, h, b = rows["dip"], rows["high"], rows["base"]
    res: dict = {"low": {}, "high": {}, "compare": {}}
    groups_low = {"≥55": d >= 55, "≥70": d >= 70, "55-69": (d >= 55) & (d < 70), "45-54": (d >= 45) & (d < 55), "<45": d < 45}
    groups_high = {"≥55": h >= 55, "≥70": h >= 70, "55-69": (h >= 55) & (h < 70), "45-54": (h >= 45) & (h < 55), "<45": h < 45}
    for name, m in groups_low.items():
        res["low"][name] = {f"exc{hz}": summarize(rows, m, f"exc{hz}") for hz in HORIZONS}
    for name, m in groups_high.items():
        res["high"][name] = {f"exc{hz}": summarize(rows, m, f"exc{hz}") for hz in HORIZONS}
    res["low_raw10"] = summarize(rows, d >= 55, "ret10")
    res["low_mdd10"] = summarize(rows, d >= 55, "mdd10")
    res["all_mdd10"] = summarize(rows, pd.Series(True, index=rows.index), "mdd10")
    res["compare"]["低位 ≥70 减 55-69（10日）"] = paired(rows, d >= 70, (d >= 55) & (d < 70), "exc10")
    res["compare"]["高位 ≥70 减 55-69（10日）"] = paired(rows, h >= 70, (h >= 55) & (h < 70), "exc10")
    # 事先定好的唯一候选改法：去掉"贴近强支撑"那 20 分，低位线从 55 改到 45（研究里样本外等价）
    res["candidate"] = {f"exc{hz}": paired(rows, b >= 45, d >= 55, f"exc{hz}") for hz in (10, 20)}
    return res


def _fmt(x: dict) -> str:
    if x.get("mean") is None:
        return "还没有结果"
    ci = f" [{x['lo']:+.2f},{x['hi']:+.2f}]" if x.get("lo") is not None else ""
    return f"{x['mean']:+.2f}{ci}，{x['days']} 天"


def render(res: dict, meta: dict) -> tuple[str, str]:
    """返回 (报告 markdown, 推送原文)。"""
    low10, high10 = res["low"]["≥55"]["exc10"], res["high"]["≥55"]["exc10"]
    flags = []
    for label, x in (("低位分 ≥55", low10), ("高位分 ≥55", high10)):
        v = verdict(x)
        if v.startswith("新数据里明显"):
            flags.append(f"{label} {v}")
    cand = res["candidate"]["exc10"]
    if cand["days"] >= MIN_DAYS_CHANGE and cand["lo"] is not None and cand["lo"] > 0:
        flags.append("去掉支撑项的版本在新数据里更好")
    if meta["missing_share"] > 0.05:
        flags.append(f"有 {meta['missing']} 只票拿不到数据，这次结论要打折扣")
    if low10["days"] < MIN_DAYS:
        conclusion = f"新数据还只有 {low10['days']} 个交易日有 10 日结果，样本太小，不下结论，公式不动。"
    elif flags:
        conclusion = "有和研究不一致的地方：" + "；".join(flags) + "。这不会自动改公式，要不要改由你决定。"
    else:
        conclusion = "新数据和研究结论一致：分数只描述涨跌多猛，没有可靠的预测力。公式不动。"

    lines = [f"# 季度复测 {meta['run_date']}", "", f"**结论：{conclusion}**", "",
             f"样本：{START} 至 {meta['last_date']}，这段数据没参与过公式设计。固定股票池 {meta['universe']} 只，"
             f"拿到数据 {meta['fetched']} 只，拿不到 {meta['missing']} 只，其中已停止交易 {meta['stale']} 只。"
             f"下面都是相对同日全部成员的超额（百分点），按日等权，方括号是 95% 区间，不含分红和交易成本。", "",
             "## 低位分", "", "| 档位 | 5 日 | 10 日 | 20 日 | 60 日 |", "|---|---|---|---|---|"]
    for name in ("≥70", "55-69", "≥55", "45-54", "<45"):
        g = res["low"][name]
        lines.append(f"| {name} | " + " | ".join(_fmt(g[f"exc{hz}"]) for hz in HORIZONS) + " |")
    lines += ["", f"对照研究（≥55）：10 日 {RESEARCH['dip55_10']:+.2f}，20 日 {RESEARCH['dip55_20']:+.2f}，60 日 {RESEARCH['dip55_60']:+.2f}。"
              f"判断：{verdict(low10)}。",
              f"≥55 买入后 10 天内平均最大浮亏 {_fmt(res['low_mdd10'])}（研究约 {RESEARCH['dip_mdd10']:.0f}%），"
              f"全部成员 {_fmt(res['all_mdd10'])}。", "",
              "## 高位分", "", "| 档位 | 5 日 | 10 日 | 20 日 | 60 日 |", "|---|---|---|---|---|"]
    for name in ("≥70", "55-69", "≥55", "45-54", "<45"):
        g = res["high"][name]
        lines.append(f"| {name} | " + " | ".join(_fmt(g[f"exc{hz}"]) for hz in HORIZONS) + " |")
    lines += ["", f"对照研究（≥55）：10 日 {RESEARCH['high55_10']:+.2f}，20 日 {RESEARCH['high55_20']:+.2f}，60 日 {RESEARCH['high55_60']:+.2f}。"
              f"判断：{verdict(high10)}。", "", "## 对比", ""]
    for k, x in res["compare"].items():
        lines.append(f"- {k}：{_fmt(x)}")
    lines += [f"- 候选改法（去掉支撑项、低位线改 45）减现行公式，同日比较：10 日 {_fmt(res['candidate']['exc10'])}；"
              f"20 日 {_fmt(res['candidate']['exc20'])}。至少 {MIN_DAYS_CHANGE} 个交易日、区间整体大于 0 才会提议。", "",
              "## 局限", "",
              "- 这是信号层面的平均，不是交易策略回测。", "- 新数据的时间短时，区间很宽；市场状态也可能只覆盖一种。",
              "- 拿不到数据的票（多半已退市）不在统计里，会让结果略偏乐观。", ""]
    push = (f"📐季度复测（新数据到 {meta['last_date']}）：低位≥55 10天相对随机 {_fmt(low10)}；"
            f"高位≥55 {_fmt(high10)}。{conclusion}")
    return "\n".join(lines), push


def run() -> dict:
    universe = load_universe()
    spy = fetch_daily("SPY")
    if spy is None:
        raise RuntimeError("取不到 SPY，Yahoo 可能不可用")
    last = spy.index[-1]
    got = data.pmap(lambda t: (t, fetch_daily(t)), universe)
    frames = [(t, df, str(last.date())) for t, df in got if df is not None]
    missing = [t for t, df in got if df is None]
    stale = [t for t, df, _ in frames if (last - df.index[-1]).days > 5]
    with ProcessPoolExecutor(max_workers=min(os.cpu_count() or 2, 8)) as ex:
        parts = [p for p in ex.map(score_ticker, frames, chunksize=20) if p is not None]
    if not parts:
        raise RuntimeError("样本外还没有数据")
    rows = add_excess(pd.concat(parts, ignore_index=True))
    res = analyze_rows(rows)
    today = datetime.now(ZoneInfo("America/New_York")).date()
    meta = {"run_date": str(today), "last_date": str(last.date()), "universe": len(universe), "fetched": len(frames),
            "missing": len(missing), "missing_share": len(missing) / max(len(universe), 1), "stale": len(stale),
            "missing_tickers": missing[:50], "stale_tickers": stale[:50], "rows": len(rows)}
    md, push = render(res, meta)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report = OUT_DIR / f"{today}.md"
    report.write_text(md, encoding="utf-8")
    (OUT_DIR / f"{today}.json").write_text(json.dumps({"meta": meta, "results": res}, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"report": report, "push": push, "meta": meta}
