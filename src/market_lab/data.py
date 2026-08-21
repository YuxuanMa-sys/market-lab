"""数据层。当前实现基于 yfinance（免费）；配置 POLYGON_API_KEY 后可替换为 Polygon。

所有取数走这里，上层分析代码不感知数据源。日线数据按天缓存在 data/cache/。
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from datetime import date, datetime, timedelta
from urllib.parse import quote as _urlquote
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

# 云端数据中心 IP 会被 Yahoo 对 yfinance 的 cookie/crumb 流程严格限流(429)，
# 但带浏览器 UA 直调 chart 接口可用——作为 yfinance 库的备用通道
_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_PERIOD_DAYS = {"6mo": 183, "1y": 365, "2y": 730, "5y": 1825}


def _yahoo_chart(ticker: str, days: int) -> dict:
    end = int(time.time())
    r = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{_urlquote(ticker)}",
        params={"period1": end - days * 86400, "period2": end, "interval": "1d"},
        headers={"User-Agent": _BROWSER_UA},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["chart"]["result"][0]


def _yahoo_direct_daily(ticker: str, period: str = "2y") -> pd.DataFrame:
    j = _yahoo_chart(ticker, _PERIOD_DAYS.get(period, 730))
    q = j["indicators"]["quote"][0]
    df = pd.DataFrame(
        {"Open": q["open"], "High": q["high"], "Low": q["low"], "Close": q["close"], "Volume": q["volume"]},
        index=pd.to_datetime(j["timestamp"], unit="s").normalize(),
    ).dropna(subset=["Close"])
    if df.empty:
        raise ValueError(f"Yahoo 直连也没有取到 {ticker} 的数据")
    return df


def _yahoo_direct_quote(ticker: str) -> dict:
    meta = _yahoo_chart(ticker, 7)["meta"]
    last = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if last is None or not prev:
        return {}
    return {
        "last": float(last),
        "prev_close": float(prev),
        "chg_pct": (float(last) / float(prev) - 1) * 100,
    }

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"
MARKET_TZ = ZoneInfo("America/Chicago")


def _now_ct() -> datetime:
    # 缓存和日历一律用市场时区，机器换时区时数据语义不变
    return datetime.now(MARKET_TZ)


def _session_tag() -> str:
    # 收盘(15:00 CT)后再留15分钟给 Polygon 延迟数据归集，15:20 才算 pm，
    # 否则 15:00-15:15 之间跑报告会把半根日K当完整K线缓存一整个下午
    now = _now_ct()
    return "pm" if (now.hour, now.minute) >= (15, 20) else "am"


def _cache_path(key: str, by_session: bool = False) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = key.replace("^", "_").replace("=", "_").replace("/", "_")
    tag = f"_{_session_tag()}" if by_session else ""
    return CACHE_DIR / f"{safe}_{_now_ct().date().isoformat()}{tag}.pkl"


def _is_stock(ticker: str) -> bool:
    """Polygon 只有股票/ETF；指数(^)和期货(=F)走 yfinance。"""
    return not ticker.startswith("^") and "=" not in ticker


def get_daily(ticker: str, period: str = "2y") -> pd.DataFrame:
    """日线 OHLCV，拆分/分红已复权。列: Open High Low Close Volume。"""
    use_polygon = has_polygon() and _is_stock(ticker)
    provider = "pg" if use_polygon else "yf"
    p = _cache_path(f"{provider}_{ticker}_{period}", by_session=True)
    if p.exists():
        return pd.read_pickle(p)
    if use_polygon:
        try:
            from .providers import polygon
            df = polygon.get_daily(ticker, period)
            df.to_pickle(p)
            return df
        except Exception:
            pass  # Polygon 失败时降级到 yfinance
    # auto_adjust=False = 仅拆分复权，与 Polygon adjusted=true 口径一致；
    # 否则降级切换时高分红票的历史位置会整体漂移
    try:
        df = yf.Ticker(ticker).history(period=period, auto_adjust=False)
        if df.empty:
            raise ValueError("empty")
        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.index = df.index.tz_localize(None)
    except Exception:
        df = _yahoo_direct_daily(ticker, period)  # yfinance 被限流(云端常见)时的直连备用
    df.to_pickle(p)
    return df


def get_daily_batch(tickers: list[str], period: str = "1y") -> dict[str, pd.DataFrame]:
    """批量日线，用于市场宽度计算。取数失败的票直接跳过。"""
    digest = hashlib.md5(",".join(sorted(tickers)).encode()).hexdigest()[:10]
    p = _cache_path(f"batch_{digest}_{period}", by_session=True)
    if p.exists():
        return pd.read_pickle(p)
    out: dict[str, pd.DataFrame] = {}
    if has_polygon():
        # Polygon 不限请求数且对云端 IP 友好；yf.download 批量接口在云端会被 Yahoo 限流
        from .providers import polygon
        for t in tickers:
            try:
                df = polygon.get_daily(t, period)
                if len(df) > 50:
                    out[t] = df
            except Exception:
                continue
    if not out:
        raw = yf.download(
            tickers, period=period, auto_adjust=False, progress=False, group_by="ticker", threads=True
        )
        for t in tickers:
            try:
                df = raw[t][["Open", "High", "Low", "Close", "Volume"]].dropna(how="all")
                if len(df) > 50:
                    out[t] = df
            except KeyError:
                continue
    pd.to_pickle(out, p)
    return out


def get_quote(ticker: str) -> dict:
    """最新报价（含盘前盘后/期货的最近成交）。失败时返回空 dict。"""
    if has_polygon() and _is_stock(ticker):
        try:
            from .providers import polygon
            q = polygon.get_quote(ticker)
            if q:
                return q
        except Exception:
            pass
    try:
        fi = yf.Ticker(ticker).fast_info
        last = fi["last_price"]
        prev = fi["previous_close"]
        return {
            "last": float(last),
            "prev_close": float(prev),
            "chg_pct": (float(last) / float(prev) - 1) * 100 if prev else None,
        }
    except Exception:
        pass
    try:
        return _yahoo_direct_quote(ticker)
    except Exception:
        return {}


def get_news(ticker: str, limit: int = 6) -> list[dict]:
    """个股新闻标题。Polygon 可用时优先（带 AI 情绪标注）。"""
    if has_polygon() and _is_stock(ticker):
        try:
            from .providers import polygon
            items = polygon.get_news(ticker, limit)
            if items:
                return items
        except Exception:
            pass
    try:
        items = yf.Ticker(ticker).news or []
    except Exception:
        return []
    out = []
    for it in items[:limit]:
        c = it.get("content", it)
        title = c.get("title", "")
        url = ""
        cu = c.get("canonicalUrl")
        if isinstance(cu, dict):
            url = cu.get("url", "")
        else:
            url = it.get("link", "")
        provider = ""
        pv = c.get("provider")
        if isinstance(pv, dict):
            provider = pv.get("displayName", "")
        elif "publisher" in it:
            provider = it.get("publisher", "")
        ts = c.get("pubDate") or it.get("providerPublishTime", "")
        if title:
            out.append({"title": title, "url": url, "source": provider, "time": str(ts)})
    return out


def has_polygon() -> bool:
    return bool(os.environ.get("POLYGON_API_KEY"))


def get_short_stats(ticker: str) -> dict | None:
    """空头状态（Ortex）。EOD 数据按最近交易日缓存（周末不重取），省 credits；失败返回 None 自动降级。"""
    if not os.environ.get("ORTEX_API_KEY"):
        return None
    d = _now_ct().date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    p = CACHE_DIR / f"short_{ticker}_{d.isoformat()}.pkl"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if p.exists():
        return pd.read_pickle(p)
    try:
        from .providers import ortex
        s = ortex.short_stats(ticker)
    except Exception:
        return None  # 网络/限流等瞬时失败不缓存，下次再试
    pd.to_pickle(s, p)  # 成功结果都缓存，包括"无数据"(None)，避免同日反复打 API
    return s


def get_option_walls(ticker: str, price: float) -> dict | None:
    """期权持仓墙（Polygon Options）。无权限/链太薄/失败时返回 None，自动降级。"""
    if not has_polygon() or not _is_stock(ticker):
        return None
    p = _cache_path(f"walls_{ticker}", by_session=True)
    if p.exists():
        return pd.read_pickle(p)
    try:
        from .providers import polygon
        w = polygon.get_option_walls(ticker, price)
    except Exception:
        return None
    pd.to_pickle(w, p)
    return w


def get_earnings_calendar(days: int = 14) -> list[dict]:
    """未来 N 天财报日历（Finnhub）。没配 key 时返回空列表。"""
    if not os.environ.get("FINNHUB_API_KEY"):
        return []
    try:
        from .providers import finnhub
        return finnhub.earnings_calendar(days)
    except Exception:
        return []
