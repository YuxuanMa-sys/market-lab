"""硬事件扫描：观察池里有没有票被收购、破产、退市、停牌、改代码。周度维护用。

只用关键词筛新闻标题和摘要，命中的还要人（或模型）核实：标题里的 "acquire" 经常是这家公司收购别人。
最要紧的是别把"没扫到"说成"没有事"：之前 Polygon 掉档后新闻全部取不到，周度任务连续两周报告"零命中"，
而 LITE 这种已签约被收购的票就留在池子里。所以这里单独报告取不到新闻的票。
"""
from __future__ import annotations

import os

KEYWORDS = ("acquire", "acquired", "acquisition", "merger", "definitive agreement", "take private", "taken private",
            "go private", "tender offer", "buyout", "bankruptcy", "chapter 11", "delist", "halted", "trading halt",
            "ticker change", "name change", "reverse split", "spin-off", "spinoff")


def _hits(items: list[dict]) -> list[str]:
    out = []
    for n in items:
        text = f"{n.get('title', '')} {n.get('summary', '')}".lower()
        if any(k in text for k in KEYWORDS):
            out.append(n.get("title", ""))
    return out


def scan(tickers: list[str], days: int = 10, per_ticker: int = 50) -> dict:
    """返回 {"scanned": 成功扫到的只数, "no_news": 这段时间确实没有新闻的票, "failed": 请求失败的票, "hits": {票: [标题...]}}。

    Finnhub 免费档每秒一次，两百多只票要 4 分钟左右。
    """
    if not os.environ.get("FINNHUB_API_KEY"):
        return {"scanned": 0, "no_news": [], "failed": list(tickers), "hits": {}, "error": "没有 FINNHUB_API_KEY"}
    from .providers import finnhub
    hits: dict[str, list[str]] = {}
    no_news, failed = [], []
    scanned = 0
    for t in tickers:
        try:
            items = finnhub.company_news(t, days=days, limit=per_ticker)
        except Exception:
            failed.append(t)
            continue
        scanned += 1
        if not items:
            no_news.append(t)
        found = _hits(items)
        if found:
            hits[t] = found[:5]
    return {"scanned": scanned, "no_news": no_news, "failed": failed, "hits": hits}
