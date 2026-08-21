"""Ortex：空头数据（EOD档）。

约束：限速约1次/秒(429则等1秒重试)；按credits计费(~1000/周期，每次~1.2)。
省credits策略：SI占流通盘必取；仅 SI >= DEEP_THRESHOLD 的高做空票追加 CTB/DTC。
公开 API v1 没有 utilization 端点，用 CTB(新借券费率) + DTC(回补天数) 替代。
"""
from __future__ import annotations

import os
import time
from datetime import date, timedelta

import requests

BASE = "https://api.ortex.com/api/v1"
DEEP_THRESHOLD = 8.0  # SI%流通盘超过此值才多花credits取CTB/DTC（2.5太松，17/22只票都触发，credits烧不起）

_last_call = 0.0


def _get(path: str, **params) -> dict:
    global _last_call
    wait = 1.1 - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    headers = {"Ortex-Api-Key": os.environ["ORTEX_API_KEY"]}
    url = f"{BASE}{path}"
    r = requests.get(url, params={"format": "json", **params}, headers=headers, timeout=20)
    _last_call = time.time()
    if r.status_code == 429:
        time.sleep(1.5)
        r = requests.get(url, params={"format": "json", **params}, headers=headers, timeout=20)
        _last_call = time.time()
    r.raise_for_status()
    return r.json()


def _latest_rows(path: str, days: int = 21) -> tuple[list[dict], dict]:
    frm = (date.today() - timedelta(days=days)).isoformat()
    j = _get(path, from_date=frm)
    rows = sorted(j.get("rows") or [], key=lambda r: r.get("date", ""))
    return rows, j


def short_stats(ticker: str) -> dict | None:
    rows, envelope = _latest_rows(f"/stock/us/{ticker}/short_interest")
    if not rows:
        return None
    last, first = rows[-1], rows[0]
    si = last.get("shortInterestPcFreeFloat")
    si_first = first.get("shortInterestPcFreeFloat")
    # 只有一行时 first 就是 last，趋势应为"数据不足"(None)而不是伪装成 0.00
    has_trend = len(rows) >= 2 and si is not None and si_first is not None
    out = {
        "si_pct_float": round(si, 2) if si is not None else None,
        "si_trend_3w": round(si - si_first, 2) if has_trend else None,
        "date": last.get("date"),
        "credits_left": envelope.get("creditsLeft"),
    }
    if si is not None and si >= DEEP_THRESHOLD:
        try:
            crows, _ = _latest_rows(f"/stock/us/{ticker}/ctb/new")
            if crows and crows[-1].get("costToBorrowNew") is not None:
                out["ctb_new"] = round(crows[-1]["costToBorrowNew"], 2)
        except Exception:
            pass
        try:
            drows, _ = _latest_rows(f"/stock/us/{ticker}/dtc")
            if drows and drows[-1].get("daysToCover") is not None:
                out["dtc"] = round(drows[-1]["daysToCover"], 2)
        except Exception:
            pass
    return out
