"""Finnhub：新闻 + 财报日历 + 实时报价。免费档限速 60次/分钟。"""
from __future__ import annotations

import os
from datetime import date, timedelta

import requests

BASE = "https://finnhub.io/api/v1"


def _get(path: str, **params) -> dict | list:
    params["token"] = os.environ["FINNHUB_API_KEY"]
    r = requests.get(f"{BASE}/{path}", params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def earnings_calendar(days: int = 14) -> list[dict]:
    today = date.today()
    data = _get(
        "calendar/earnings",
        **{"from": today.isoformat(), "to": (today + timedelta(days=days)).isoformat()},
    )
    out = []
    for e in data.get("earningsCalendar", []):
        out.append({
            "symbol": e.get("symbol"),
            "date": e.get("date"),
            "hour": {"bmo": "盘前", "amc": "盘后"}.get(e.get("hour"), e.get("hour") or ""),
            "eps_estimate": e.get("epsEstimate"),
        })
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
