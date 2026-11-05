"""硬事件扫描：观察池里有没有票被收购、破产、退市、停牌、改代码。周度维护用。

只是筛新闻标题，命中的还要人（或模型）核实。两件事最要紧：
- 别把"没扫到"说成"没有事"：之前 Polygon 掉档后新闻全部取不到，周度任务连续两周报告"零命中"，
  而已签约被收购的 LITE 就留在池子里。所以这里单独报告没扫成的票。
- 别淹没真正的事件：Finnhub 的公司新闻把提到这只票的文章都算进来，只按关键词筛的话，
  231 只里 125 只都有命中（比如 AAPL 命中"Skyworks ... Qorvo's Merger"）。所以标题必须提到这家公司本身。
"""
from __future__ import annotations

import os
import re

from . import data

# 标题提到这家公司，又出现这些词，才算可能相关。"收购别人"不在这里，见 _BUY_RE。
EVENT_WORDS = ("to be acquired", "acquired by", "to be bought", "being acquired", "sold to", "takeover", "take-private",
               "take private", "taken private", "go private", "going private", "buyout", "bought out", "merger",
               "to merge", "to combine", "bankrupt", "chapter 11", "delist", "deficiency notice", "trading halt", "halted",
               "ticker change", "new ticker", "change ticker", "ticker symbol",
               "to split into", "breakup", "break up", "spin-off", "spinoff", "to spin off")
_EVENT_RE = r"\btake \S+ private\b|\btrading\b.*\bsuspended\b|\bsuspended\b.*\btrading\b"
# 不收单独的 buy：评级和荐股标题里到处都是（"Maintains Buy on AMD"、"Should You Buy Micron"）
_BUY_RE = r"\b(?:to acquire|to buy|will acquire|will buy|agrees? to buy|acquires?|acquiring|buys|acquisition of|bid for|offer for|deal for|purchase of)\b"
_NOT_COMPANY = r"(?!\W+(?:stock|shares|ai\b|chips|gpus|products))"     # "to buy Nvidia AI chips" 买的是货不是公司
_NOT_DEAL = ("reason to buy", "reasons to buy", "late to buy", "stocks to buy", "time to buy", "stock options")   # 荐股和内部人交易
_GENERIC = {"american", "united", "general", "first", "national", "international", "digital", "applied", "advanced",
            "global", "western", "royal", "texas", "southern", "bank", "new", "the", "eli"}
# Yahoo 的公司名和标题里的叫法对不上的票。用词边界：citi 不能匹配 Citizens，southern co 不能匹配 Southern Copper
ALIASES = {"GOOG": r"\bgoogle\b", "DIS": r"\bdisney\b", "XOM": r"\bexxon\b", "LLY": r"\blilly\b", "C": r"\bciti\b",
           "TSM": r"\btsmc\b", "SO": r"\bsouthern co\b", "ON": r"\bonsemi\b"}


def _squash(text: str) -> str:
    """去掉缩写里的句点，U.S. Bancorp → US Bancorp，D.R. Horton → DR Horton。Amazon.com 不受影响。"""
    return re.sub(r"\b(?:[A-Za-z]\.){2,}", lambda m: m.group().replace(".", ""), text)


def company_key(name: str | None) -> str:
    """公司名里用来匹配标题的部分（小写）。Lumentum Holdings Inc. → lumentum；Royal Caribbean Cruises → royal caribbean。"""
    words = re.sub(r"[^A-Za-z0-9&\- ]", " ", _squash(name or "")).split()
    if words and words[0].lower() == "the":
        words = words[1:]
    if not words:
        return ""
    first = words[0].lower()
    if len(first) >= 3 and first not in _GENERIC:
        return first
    n = 3 if len(words) > 2 and words[1].lower() in ("of", "and", "&") else 2
    return " ".join(words[:n]).lower()


def _name_pattern(ticker: str, key: str) -> str:
    return "|".join(x for x in (re.escape(key) if key else "", ALIASES.get(ticker, "")) if x)


def mentions(title: str, ticker: str, key: str) -> bool:
    name = _name_pattern(ticker, key)
    if name and re.search(name, _squash(title).lower()):
        return True
    # 代码区分大小写。一两个字母的代码（T、ON、V）只认括号里的写法，不然到处都是
    t = re.escape(ticker)
    pat = rf"\((?:[A-Z]+:)?{t}\)" if len(ticker) <= 2 else rf"(?<![A-Za-z]){t}(?![A-Za-z])"
    return re.search(pat, title) is not None


def is_hit(title: str, ticker: str, key: str) -> bool:
    if not mentions(title, ticker, key):
        return False
    low = _squash(title).lower()
    if any(w in low for w in EVENT_WORDS) or re.search(_EVENT_RE, low):
        return True
    # "XX to acquire 这家公司"：公司名要出现在收购动词后面。"这家公司 acquires XX"是它收购别人，不算。
    if any(w in low for w in _NOT_DEAL):
        return False
    name = _name_pattern(ticker, key)
    return bool(name and re.search(_BUY_RE + rf".*?(?:{name})" + _NOT_COMPANY, low))


def scan(tickers: list[str], days: int = 10) -> dict:
    """返回 {"scanned": 成功扫到的只数, "no_news": 这段时间没有新闻的票, "failed": 请求失败的票,
    "name_unknown": 取不到公司名的票（只能靠标题里的代码匹配，会漏）, "partial": {新闻太多没翻完的票: 实际覆盖天数},
    "hits": {票: [标题...]}}。

    Finnhub 免费档每秒一次，两百多只票要 5 分钟左右（新闻多的票要翻页）。
    """
    if not os.environ.get("FINNHUB_API_KEY"):
        return {"scanned": 0, "no_news": [], "failed": list(tickers), "name_unknown": [], "partial": {}, "hits": {},
                "error": "没有 FINNHUB_API_KEY"}
    from .providers import finnhub
    names = dict(data.pmap(lambda t: (t, data.get_company_name(t)), tickers))
    hits: dict[str, list[str]] = {}
    partial: dict[str, float] = {}
    no_news, failed = [], []
    scanned = 0
    for t in tickers:
        try:
            items, covered = finnhub.company_news_all(t, days=days)
        except Exception:
            failed.append(t)
            continue
        scanned += 1
        if not items:
            no_news.append(t)
        if covered is not None:
            partial[t] = covered
        key = company_key(names.get(t))
        found = list(dict.fromkeys(n["title"] for n in items if is_hit(n.get("title", ""), t, key)))
        if found:
            hits[t] = found[:6]
    return {"scanned": scanned, "no_news": no_news, "failed": failed,
            "name_unknown": [t for t in tickers if not names.get(t)], "partial": partial, "hits": hits}
