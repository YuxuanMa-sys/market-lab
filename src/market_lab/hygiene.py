"""周度维护的数据卫生检查（确定性部分）。

2026-10-17 基本面研究（SEC EDGAR 时点数据，H1 拟合 / H2 样本外，几百个检验 + 多重校正）的结论：
盈利、增长、现金流、稀释、红旗、估值，没有一条池子规则能让跌进低位(dip≥55)的票在 10/20/60 日
明显好于被筛掉的低位票，也跑不赢同日随机股。所以周度维护不再按"值不值得"挑票，只保证池子里
的票能交易、数据可信：
  1 是个股（普通股/ADR，不是 ETF、权证、优先股等）
  2 实际收盘价 ≥ $5
  3 近 20 个交易日 收盘价×成交量 均值 ≥ $50M，且 20 天都有成交
  5 数据完整：近 20 日缺失 ≤3 天、ATR14>0、两年日线里没有"换了一家公司"的拼接
硬事件（被收购、破产、退市通知、停牌、改代码）要看新闻，由周度任务自己核实，不在这里。

剔除要"连续两次周检不满足 2 或 3"。云端每次都是全新副本、记不住上周，所以这里直接用
"5 个交易日前"的窗口复算一遍上周的结果。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import data
from .indicators import atr

STOCK_TYPES = {"CS", "ADRC", "OS", "NYRS", "GDR"}   # Polygon reference 的 type 字段
MIN_PRICE = 5.0
MIN_DVOL = 50e6
MAX_MISSING_20 = 3


def find_splices(df: pd.DataFrame) -> list[dict]:
    """日历断档 >10 天，且前后 20 日成交量中位数相差 >100 倍或价格跳 >5 倍：同一代码换了一家公司
    （例：SPCX 以前是 SPAC ETF，约 $25；同代码现在交易的票约 $140）。与无偏回测面板的拼接检测同一口径。"""
    out = []
    idx, c, v = df.index, df["Close"].to_numpy(float), df["Volume"].to_numpy(float)
    for g in np.where(np.diff(idx).astype("timedelta64[D]").astype(int) > 10)[0]:
        pre, post = v[max(0, g - 19):g + 1], v[g + 1:g + 21]
        r = (np.nanmedian(post) + 1) / (np.nanmedian(pre) + 1)
        pr = c[g + 1] / c[g] if c[g] else np.nan
        if r > 100 or r < 0.01 or (np.isfinite(pr) and (pr > 5 or pr < 0.2)):
            out.append({"before": str(idx[g].date()), "after": str(idx[g + 1].date()),
                        "close_before": round(float(c[g]), 2), "close_after": round(float(c[g + 1]), 2)})
    return out


def window_stats(df: pd.DataFrame, cal: pd.DatetimeIndex) -> dict:
    """在交易日历 cal（取最后 20 天）上算价格和流动性。缺失的交易日按成交额 0 计。"""
    win = cal[-20:]
    sub = df.reindex(win)
    traded = int(sub["Close"].notna().sum())
    dvol = (sub["Close"] * sub["Volume"]).fillna(0.0).mean()
    last = df.loc[:win[-1], "Close"].dropna()
    price = float(last.iloc[-1]) if len(last) else float("nan")
    fails = []
    if not price >= MIN_PRICE:
        fails.append(f"2 价格 {price:.2f} < ${MIN_PRICE:.0f}")
    if dvol < MIN_DVOL:
        fails.append(f"3 20日均成交额 ${dvol / 1e6:.0f}M < ${MIN_DVOL / 1e6:.0f}M")
    if traded < 20:
        fails.append(f"3 近20个交易日只有 {traded} 天有成交")
    return {"price": round(price, 2), "dvol20_m": round(float(dvol) / 1e6, 1),
            "traded20": traded, "fails": fails}


def _ticker_type(ticker: str) -> tuple[str | None, bool | None]:
    try:
        from .providers import polygon
        r = polygon._get(f"/v3/reference/tickers/{polygon._sym(ticker)}").get("results") or {}
        return r.get("type"), r.get("active")
    except Exception:
        return None, None


def check(watchlist: list[dict], only: list[str] | None = None) -> dict:
    cal = data.get_daily("SPY", "2y").index
    rows = []
    for item in watchlist:
        t = item["symbol"]
        if only and t not in only:
            continue
        held = item.get("cost") is not None or item.get("pct") is not None
        row = {"ticker": t, "held": held}
        try:
            df = data.get_daily(t, "2y")
        except Exception as e:
            row.update({"error": f"取不到日线: {e}", "fails_new": ["5 取不到日线"],
                        "remove_eligible": False})
            rows.append(row)
            continue
        typ, active = _ticker_type(t)
        now = window_stats(df, cal)
        prev = window_stats(df, cal[:-5])
        spl = find_splices(df)
        a = float(atr(df).iloc[-1]) if len(df) > 15 else 0.0
        valid_from = pd.Timestamp(spl[-1]["after"]) if spl else df.index[0]
        valid_days = int((df.index >= valid_from).sum())

        data_fails = []
        if 20 - now["traded20"] > MAX_MISSING_20:
            data_fails.append(f"5 近20日缺 {20 - now['traded20']} 天")
        if not a > 0:
            data_fails.append("5 ATR14 不可用")
        if spl:
            data_fails.append(f"5 日线拼接：{spl[-1]['before']}→{spl[-1]['after']} 价格 "
                              f"{spl[-1]['close_before']}→{spl[-1]['close_after']}，"
                              f"之前的历史是另一只证券（真实历史只有 {valid_days} 天）")
        type_fails = []
        if typ is not None and typ not in STOCK_TYPES:
            type_fails.append(f"1 类型 {typ} 不是个股")
        if active is False:
            type_fails.append("1 Polygon 标记为已停止交易")

        row.update({
            "type": typ, "price": now["price"], "dvol20_m": now["dvol20_m"],
            "atr_pct": round(a / now["price"] * 100, 1) if now["price"] > 0 else None,
            "splices": spl, "valid_days": valid_days,
            "fails_now": now["fails"], "fails_prev_week": prev["fails"],
            "fails_new": type_fails + now["fails"] + data_fails,     # 新票入池用：必须为空
            "data_issues": data_fails,
            # 候选票剔除：本周和上周都不满足 2 或 3，或者类型/停牌问题
            "remove_eligible": (not held) and (bool(now["fails"] and prev["fails"]) or bool(type_fails)),
        })
        rows.append(row)
    return {
        "criteria": {"min_price": MIN_PRICE, "min_dvol20_m": MIN_DVOL / 1e6,
                     "max_missing_20": MAX_MISSING_20, "stock_types": sorted(STOCK_TYPES)},
        "as_of": str(cal[-1].date()),
        "remove_eligible": [r["ticker"] for r in rows if r.get("remove_eligible")],
        "data_issues": {r["ticker"]: r["data_issues"] for r in rows if r.get("data_issues")},
        "rows": rows,
    }
