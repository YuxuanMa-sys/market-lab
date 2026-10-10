"""Finnhub：新闻 + 财报日历 + 实时报价。免费档限速 60次/分钟。"""
from __future__ import annotations

import os
import sys as _sys
from datetime import date, timedelta

import requests

BASE = "https://finnhub.io/api/v1"


def _get(path: str, **params) -> dict | list:
    params["token"] = os.environ["FINNHUB_API_KEY"]
    r = requests.get(f"{BASE}/{path}", params=params, timeout=15)
    r.raise_for_status()
    return r.json()


CAL_ROW_CAP = 1500      # Finnhub 单次响应的硬上限（实测）
CAL_CHUNK_DAYS = 5      # 分片宽度。财报季高峰(10月下旬-11月初)14天分片仍会触到1500上限，
                        # 实测需要收到 5 天；上面的告警会在再次封顶时喊出来


def earnings_calendar(days: int = 14) -> list[dict]:
    """未来 N 天财报日历，按 14 天分片抓取后合并。

    为什么必须分片：Finnhub 的响应在 1500 条封顶，而且**不按日期排序**——
    直接要一个宽区间会被静默截断，而且截掉的往往是近端日期。
    2026-10-10 实测：days=14 返回 557 条（完整，含 WDC/NBIS 的 10/22）；
    days=45 返回 1500 条（封顶），WDC 和 NBIS 的 10/22 双双消失，只剩 CRWV 11/11。
    把窗口"拉宽"反而让最该看见的近端财报不见了，而且没有任何报错。
    """
    today = date.today()
    seen: set[tuple] = set()
    out: list[dict] = []
    truncated: list[str] = []
    start = 0
    while start < days:
        end = min(start + CAL_CHUNK_DAYS, days)
        d0 = today + timedelta(days=start)
        d1 = today + timedelta(days=end)
        try:
            data = _get("calendar/earnings", **{"from": d0.isoformat(), "to": d1.isoformat()})
        except Exception:
            start = end
            continue
        rows = data.get("earningsCalendar", []) or []
        if len(rows) >= CAL_ROW_CAP:
            truncated.append(f"{d0}~{d1}")
        for e in rows:
            key = (e.get("symbol"), e.get("date"))
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "symbol": e.get("symbol"),
                "date": e.get("date"),
                "hour": {"bmo": "盘前", "amc": "盘后"}.get(e.get("hour"), e.get("hour") or ""),
                "eps_estimate": e.get("epsEstimate"),
            })
        start = end
    if truncated:
        # 不静默：分片还封顶说明该缩小 CAL_CHUNK_DAYS，否则又在丢数据
        print(f"⚠ 财报日历分片仍触到 {CAL_ROW_CAP} 条上限: {', '.join(truncated)}"
              f"——请调小 CAL_CHUNK_DAYS", file=_sys.stderr)
    out.sort(key=lambda x: x["date"] or "")
    return out


def company_news(symbol: str, days: int = 7, limit: int = 8) -> list[dict]:
    today = date.today()
    items = _get(
        "company-news",
        symbol=symbol,
        **{"from": (today - timedelta(days=days)).isoformat(), "to": today.isoformat()},
    )
    out = []
    for it in items[:limit]:
        out.append({
            "title": it.get("headline", ""),
            "url": it.get("url", ""),
            "source": it.get("source", ""),
            "time": str(it.get("datetime", "")),
        })
    return out


def general_news(limit: int = 10) -> list[dict]:
    items = _get("news", category="general")
    return [
        {
            "title": it.get("headline", ""),
            "url": it.get("url", ""),
            "source": it.get("source", ""),
            "time": str(it.get("datetime", "")),
        }
        for it in items[:limit]
    ]


def quote(symbol: str) -> dict:
    q = _get("quote", symbol=symbol)
    return {
        "last": q.get("c"),
        "prev_close": q.get("pc"),
        "chg_pct": q.get("dp"),
    }
