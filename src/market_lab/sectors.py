"""板块：分类表 + 收盘板块总结。分类和验证方法写在仓库根目录的 sectors.yaml 开头。"""
from __future__ import annotations

from pathlib import Path
from statistics import median

import yaml

from . import data

ROOT = Path(__file__).resolve().parents[2]
SECTORS_FILE = ROOT / "sectors.yaml"
MIN_NAMES = 3                 # 细分少于这么多只，板块数字就不太有代表性了
THIN_OK = {"EDA"}             # 例外：EDA 全美就两家


def load_taxonomy() -> dict[str, dict]:
    """{大板块: {"etf": 参照ETF或None, "subs": [细分, ...]}}，顺序同文件。"""
    return yaml.safe_load(SECTORS_FILE.read_text(encoding="utf-8"))


def sub_to_group(tax: dict | None = None) -> dict[str, str]:
    tax = load_taxonomy() if tax is None else tax
    return {s: g for g, v in tax.items() for s in v["subs"]}


def coverage(watchlist: list[dict], tax: dict | None = None) -> dict:
    """观察池的板块覆盖：每个细分几只、哪些太薄、哪些票没分类或分类名写错。"""
    tax = load_taxonomy() if tax is None else tax
    groups = sub_to_group(tax)
    counts = {s: 0 for s in groups}
    unclassified = []
    for i in watchlist:
        s = i.get("sector")
        if s in counts:
            counts[s] += 1
        else:
            unclassified.append(i["symbol"])
    return {"counts": counts, "total": len(watchlist), "unclassified": unclassified,
            "thin": [s for s, n in counts.items() if n < MIN_NAMES and s not in THIN_OK]}


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def _stats(rows: list[dict], spy_chg) -> dict:
    chg = [r["chg_pct"] for r in rows]
    best = max(rows, key=lambda r: r["chg_pct"])
    worst = min(rows, key=lambda r: r["chg_pct"])
    avg = _mean(chg)
    return {
        "n": len(rows), "avg": avg, "median": round(median(chg), 2),
        "vs_spy": round(avg - spy_chg, 2) if spy_chg is not None else None,
        "up": sum(c > 0 for c in chg), "down": sum(c < 0 for c in chg),
        "ret5": _mean([r.get("ret5") for r in rows]), "ret20": _mean([r.get("ret20") for r in rows]),
        "n_low": sum(r["dip"] >= 55 for r in rows), "n_deep": sum(r["dip"] >= 70 for r in rows),
        "n_high": sum((r["high"] or 0) >= 55 for r in rows),
        "best": {"ticker": best["ticker"], "chg": best["chg_pct"]},
        "worst": {"ticker": worst["ticker"], "chg": worst["chg_pct"]},
    }


def etf_changes(tax: dict | None = None, as_of: str | None = None) -> dict[str, float]:
    """参照 ETF 最近一个交易日的涨跌幅。取不到的就不写。"""
    tax = load_taxonomy() if tax is None else tax

    def one(etf):
        try:
            c = data.get_daily(etf, "2y")["Close"]
            if as_of and c.index[-1].date().isoformat() < as_of:     # 缺最近一根：涨跌幅是旧的，不写
                return etf, None
            return etf, round((float(c.iloc[-1]) / float(c.iloc[-2]) - 1) * 100, 2)
        except Exception:
            return etf, None
    return {e: v for e, v in data.pmap(one, [v["etf"] for v in tax.values() if v.get("etf")]) if v is not None}


def build_sector_summary(stocks: list[dict], wl_by_symbol: dict, spy_chg: float | None = None,
                         etf_chg: dict | None = None, tax: dict | None = None) -> dict:
    """观察池各板块今天的情况（等权，不是行业指数）。stocks 是 analyze() 的结果。

    只统计 watchlist 里有 sector 的票。持仓里不在池子的票不算进来，免得一只票拉偏一个小细分。
    """
    tax = load_taxonomy() if tax is None else tax
    groups = sub_to_group(tax)
    by_sub: dict[str, list[dict]] = {}
    for s in stocks:
        sub = (wl_by_symbol.get(s["ticker"]) or {}).get("sector")
        if sub not in groups or s.get("chg_pct") is None:
            continue
        by_sub.setdefault(sub, []).append({
            "ticker": s["ticker"], "chg_pct": s["chg_pct"], "ret5": s.get("ret5"), "ret20": s.get("ret20"),
            "dip": float(s["dip"]["score"]), "high": (s.get("high") or {}).get("score"),
        })
    out = []
    every = []
    for g, v in tax.items():
        rows = [r for sub in v["subs"] for r in by_sub.get(sub, [])]
        if not rows:
            continue
        every.extend(rows)
        subs = [{"sub": sub, **_stats(by_sub[sub], spy_chg)} for sub in v["subs"] if by_sub.get(sub)]
        subs.sort(key=lambda x: -x["avg"])
        etf = v.get("etf")
        out.append({"group": g, "etf": etf, "etf_chg": (etf_chg or {}).get(etf), **_stats(rows, spy_chg), "subs": subs})
    out.sort(key=lambda x: -x["avg"])
    return {"spy_chg": spy_chg, "pool": _stats(every, spy_chg) if every else None, "groups": out}
