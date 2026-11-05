"""Finnhub：新闻 + 财报日历 + 实时报价。免费档限速 60次/分钟。"""
from __future__ import annotations

import os
import sys as _sys
import threading
import time
from datetime import date, datetime, timedelta, timezone

import requests

BASE = "https://finnhub.io/api/v1"
MIN_INTERVAL = 1.05     # 免费档 60 次/分钟。观察池上百只票时必须自己限速，否则后面的请求全被 429

_lock = threading.Lock()
_last_call = 0.0
_wait_left = 240.0      # 整个进程最多为 429 等这么久。一直 429 时也要在 Bash 上限 590 秒内跑完，把没扫成的票写出来


def _reset_wait(r: requests.Response) -> float:
    """60 秒窗口用完了，等 5 秒没用，要等到窗口重置（X-Ratelimit-Reset，2026-11-05 实测 429 响应里也有）。"""
    try:
        return min(61.0, max(1.0, float(r.headers["X-Ratelimit-Reset"]) - time.time() + 1))
    except (KeyError, ValueError):
        return 5.0


def _get(path: str, **params) -> dict | list:
    global _last_call, _wait_left
    params["token"] = os.environ["FINNHUB_API_KEY"]
    for attempt in range(2):
        with _lock:
            wait = _last_call + MIN_INTERVAL - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last_call = time.monotonic()
        r = requests.get(f"{BASE}/{path}", params=params, timeout=15)
        if r.status_code == 429 and attempt == 0 and _wait_left > 0:
            wait = _reset_wait(r)
            _wait_left -= wait
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()


CAL_ROW_CAP = 1500      # Finnhub 单次响应的硬上限（实测）
CAL_CHUNK_DAYS = 5      # 分片宽度。财报季高峰一片还会封顶（2026-11-02~05 实测 1519 条），封顶的片对半重查
NEWS_PAGE_CAP = 230     # company-news 单次最多约 250 条，新的在前（实测 TSLA 243、SPCX 242 也是被截断的）
NEWS_MAX_PAGES = 12


def earnings_calendar(days: int = 14, problems: list[str] | None = None) -> list[dict]:
    """未来 N 天财报日历，按 CAL_CHUNK_DAYS 分片抓取后合并。

    为什么必须分片：Finnhub 的响应在 1500 条封顶，而且**不按日期排序**——
    直接要一个宽区间会被静默截断，而且截掉的往往是近端日期。
    2026-10-10 实测：days=14 返回 557 条（完整，含 WDC/NBIS 的 10/22）；
    days=45 返回 1500 条（封顶），WDC 和 NBIS 的 10/22 双双消失，只剩 CRWV 11/11。
    把窗口"拉宽"反而让最该看见的近端财报不见了，而且没有任何报错。
    """
    today = date.today()
    seen: set[tuple] = set()
    out: list[dict] = []
    problems = [] if problems is None else problems

    def fetch(d0: date, d1: date) -> None:
        try:
            data = _get("calendar/earnings", **{"from": d0.isoformat(), "to": d1.isoformat()})
        except Exception:
            problems.append(f"{d0}~{d1} 取数失败")
            return
        rows = data.get("earningsCalendar", []) or []
        if len(rows) >= CAL_ROW_CAP:
            if d1 > d0:
                mid = d0 + (d1 - d0) // 2
                fetch(d0, mid)
                fetch(mid + timedelta(days=1), d1)
                return
            problems.append(f"{d0} 单日触到 {CAL_ROW_CAP} 条上限")
        for e in rows:
            key = (e.get("symbol"), e.get("date"))
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "symbol": (e.get("symbol") or "").replace(".", "-"),
                "date": e.get("date"),
                "hour": {"bmo": "盘前", "amc": "盘后"}.get(e.get("hour"), e.get("hour") or ""),
                "eps_estimate": e.get("epsEstimate"),
            })

    start = 0
    while start < days:
        end = min(start + CAL_CHUNK_DAYS, days)
        fetch(today + timedelta(days=start), today + timedelta(days=end))
        start = end
    if problems:
        # 不静默：这些日期的财报在报告里看不到 📅
        print(f"⚠ 财报日历不全: {', '.join(problems)}", file=_sys.stderr)
    out.sort(key=lambda x: x["date"] or "")
    return out


def next_earnings(symbol: str, days: int = 45) -> str | None:
    """按代码查这只票的下一次财报日。批量日历只认主上市代码（Alphabet 是 GOOGL，台积电是 2330.TW），
    按代码查时返回的 symbol 也是主上市代码，所以这里只取日期，由调用方记到自己的代码名下。"""
    today = date.today()
    data = _get("calendar/earnings", symbol=symbol.replace("-", "."),
                **{"from": today.isoformat(), "to": (today + timedelta(days=days)).isoformat()})
    dates = sorted(e["date"] for e in data.get("earningsCalendar") or [] if e.get("date"))
    return dates[0] if dates else None


def _news_item(it: dict) -> dict:
    return {
        "title": it.get("headline", ""),
        "summary": it.get("summary", ""),
        "url": it.get("url", ""),
        "source": it.get("source", ""),
        "time": str(it.get("datetime", "")),
    }


def company_news(symbol: str, days: int = 7, limit: int = 8) -> list[dict]:
    today = date.today()
    items = _get(
        "company-news",
        symbol=symbol.replace("-", "."),     # Finnhub 用 BRK.B，我们用 BRK-B
        **{"from": (today - timedelta(days=days)).isoformat(), "to": today.isoformat()},
    )
    return [_news_item(it) for it in items[:limit]]


def company_news_all(symbol: str, days: int = 10) -> tuple[list[dict], dict | None]:
    """这段时间的全部标题。返回 (新闻, 没覆盖全时 {"days": 最早取到距今几天, "cut_days": 一天就超过上限、当天没取全的日子}；覆盖全了是 None)。

    单次最多约 250 条：财报季 NVDA 只够 1.4 天、MRVL 6.5 天，所以条数接近上限时往回翻页。
    """
    now = datetime.now(timezone.utc)
    start = now.date() - timedelta(days=days)
    to = now.date()
    seen: set[str] = set()
    out: list[dict] = []
    earliest = now.timestamp()
    gap = False
    cut_days: list[str] = []
    for _ in range(NEWS_MAX_PAGES):
        page = _get("company-news", symbol=symbol.replace("-", "."), **{"from": start.isoformat(), "to": to.isoformat()})
        for it in page:
            key = str(it.get("id") or it.get("url") or it.get("headline"))
            if key not in seen:
                seen.add(key)
                out.append(_news_item(it))
        stamps = [it["datetime"] for it in page if it.get("datetime")]
        earliest = min([earliest, *stamps])
        if len(page) < NEWS_PAGE_CAP or not stamps:
            break
        first_day = datetime.fromtimestamp(min(stamps), timezone.utc).date()
        if first_day < start:
            break
        if first_day >= to:      # 一天就超过上限，这一天取不全，只能跳到前一天
            gap = True
            cut_days.append(to.isoformat())
            if to == start:
                break
            first_day = to - timedelta(days=1)
        to = first_day           # 这一页最早的那天可能也没取全，所以从这一天接着翻，靠去重
    else:
        gap = True
    return out, ({"days": round((now.timestamp() - earliest) / 86400, 1), "cut_days": cut_days} if gap else None)


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
