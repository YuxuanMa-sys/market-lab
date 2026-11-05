"""数据层。所有取数走这里，上层分析代码不感知数据源。日线数据按天缓存在 data/cache/。

数据源优先级：Polygon（只有付费档才用）→ Yahoo chart 接口直连 → yfinance 库。
2026-10 下旬 Polygon key 掉到免费档（每分钟 5 次、无快照/期权），之后：
- 前几只票走 Polygon、其余被 429 后降级，来源混杂；
- 盘中"一小时前价格"取不到，大部分票的新进低位判断失灵。
所以现在先探测档位（polygon_status），免费档就整个进程都不用 Polygon。
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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


class SymbolNotFound(LookupError):
    """Yahoo 明确说没有这个代码（已退市、改代码）。和限流、网络错分开：后者只是这次没查成。"""


def _yahoo_get(ticker: str, params: dict) -> dict:
    """Yahoo chart 接口，返回 result[0]。限流/5xx 换主机重试；代码不存在直接报错不重试。"""
    err: Exception | None = None
    for host, wait in (("query1", 0.0), ("query2", 1.0), ("query1", 3.0)):
        if wait:
            time.sleep(wait)
        try:
            r = requests.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{_urlquote(ticker)}",
                             params=params, headers={"User-Agent": _BROWSER_UA}, timeout=20)
            if r.status_code == 404:
                raise SymbolNotFound(f"Yahoo 没有 {ticker}")
            if r.status_code == 429 or r.status_code >= 500:
                err = RuntimeError(f"Yahoo {r.status_code}")
                continue
            r.raise_for_status()
            result = (r.json().get("chart") or {}).get("result")
            if not result:
                raise SymbolNotFound(f"Yahoo 没有 {ticker}")
            return result[0]
        except requests.RequestException as e:
            err = e
    raise err if err else RuntimeError("Yahoo 请求失败")


def _yahoo_chart(ticker: str, days: int) -> dict:
    end = int(time.time())
    return _yahoo_get(ticker, {"period1": end - days * 86400, "period2": end, "interval": "1d"})


def _yahoo_direct_daily(ticker: str, period: str = "2y") -> pd.DataFrame:
    j = _yahoo_chart(ticker, _PERIOD_DAYS.get(period, 730))
    if not j.get("timestamp"):      # 退市后 Yahoo 有时还回 200，只是没有K线（K、CMA）
        raise SymbolNotFound(f"Yahoo 没有 {ticker} 的K线")
    q = j["indicators"]["quote"][0]
    df = pd.DataFrame(
        {"Open": q["open"], "High": q["high"], "Low": q["low"], "Close": q["close"], "Volume": q["volume"]},
        index=pd.to_datetime(j["timestamp"], unit="s").normalize(),
    ).dropna(subset=["Close"])
    if df.empty:
        raise ValueError(f"Yahoo 直连也没有取到 {ticker} 的数据")
    return df


def get_intraday(ticker: str) -> dict:
    """现价 + 昨收 + 今天的高低点 + 当天 5 分钟线（Yahoo，一个请求拿全）。失败返回空 dict。

    昨收必须用 range=1d 的 chartPreviousClose：它指"图表起点之前的收盘"，
    旧代码用 7 天窗口取，算出来的"今日涨跌"其实是一周涨跌。
    """
    try:
        j = _yahoo_get(ticker, {"range": "1d", "interval": "5m"})
    except Exception:
        return {}
    m = j.get("meta") or {}
    last = m.get("regularMarketPrice")
    prev = m.get("chartPreviousClose") or m.get("previousClose")
    if last is None or not prev:
        return {}
    closes = (((j.get("indicators") or {}).get("quote") or [{}])[0].get("close")) or []
    bars = [(int(t), float(c)) for t, c in zip(j.get("timestamp") or [], closes) if c is not None]
    return {"last": float(last), "prev_close": float(prev), "chg_pct": (float(last) / float(prev) - 1) * 100,
            "day_high": m.get("regularMarketDayHigh"), "day_low": m.get("regularMarketDayLow"),
            "bars": bars, "type": m.get("instrumentType")}


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


def _atomic_pickle(obj, path: Path) -> None:
    # 多线程并发取同一只票时，避免别的线程读到写了一半的文件
    tmp = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
    pd.to_pickle(obj, tmp)
    os.replace(tmp, path)


def _drop_partial_bar(df: pd.DataFrame) -> pd.DataFrame:
    """日线只留已收盘的：收盘数据归集完之前（同 _session_tag 的 15:20 CT），去掉今天那根还在走的 K 线。

    Yahoo 盘中会带今天的半根。留着它的话，盘中提醒拿"昨收"做比较时比的其实是今天的价格，
    于是几乎不会判定"新进入低位"（2026-11-04 实测：VST 盘中 −13.8%、收盘抄底分 60，没有提醒）。
    """
    if len(df) and _session_tag() == "am" and df.index[-1].date() == _now_ct().date():
        return df.iloc[:-1]
    return df


def pmap(fn, items, workers: int = 8) -> list:
    """并发 map，保持顺序。观察池上百只票，顺序取数会超过云端任务能等的时长。"""
    items = list(items)
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as ex:
        return list(ex.map(fn, items))


def _is_stock(ticker: str) -> bool:
    """Polygon 只有股票/ETF；指数(^)和期货(=F)走 yfinance。"""
    return not ticker.startswith("^") and "=" not in ticker


def get_daily(ticker: str, period: str = "2y") -> pd.DataFrame:
    """已收盘的日线 OHLCV，只做拆分复权（不含分红）。列: Open High Low Close Volume。"""
    p = _cache_path(f"daily_{ticker}_{period}", by_session=True)
    if p.exists():
        return pd.read_pickle(p)
    df = None
    if has_polygon() and _is_stock(ticker):
        try:
            from .providers import polygon
            df = polygon.get_daily(ticker, period)
        except Exception:
            df = None  # Polygon 失败时降级
    if df is None:
        try:
            df = _yahoo_direct_daily(ticker, period)
        except Exception as e:
            # auto_adjust=False = 仅拆分复权，与上面两个来源口径一致；
            # 否则降级切换时高分红票的历史位置会整体漂移
            try:
                h = yf.Ticker(ticker).history(period=period, auto_adjust=False)
            except Exception:       # yfinance 自己被限流时不能盖掉"代码不存在"
                h = None
            if h is None or h.empty:
                raise (e if isinstance(e, SymbolNotFound) else ValueError(f"取不到 {ticker} 的日线"))
            df = h[["Open", "High", "Low", "Close", "Volume"]].copy()
            df.index = df.index.tz_localize(None)
    df = _drop_partial_bar(df)
    _atomic_pickle(df, p)
    return df


def get_daily_batch(tickers: list[str], period: str = "1y") -> dict[str, pd.DataFrame]:
    """批量日线，用于市场宽度计算。取数失败或历史太短的票直接跳过。"""
    def one(t):
        try:
            df = get_daily(t, period)
            return t, (df if len(df) > 50 else None)
        except Exception:
            return t, None
    return {t: df for t, df in pmap(one, tickers) if df is not None}


def get_quote(ticker: str) -> dict:
    """最新报价（期货含夜盘最近成交）。失败时返回空 dict。"""
    if has_polygon() and _is_stock(ticker):
        try:
            from .providers import polygon
            q = polygon.get_quote(ticker)
            if q:
                return q
        except Exception:
            pass
    q = get_intraday(ticker)
    if q:
        return {k: q[k] for k in ("last", "prev_close", "chg_pct")}
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
        return {}


def get_company_name(ticker: str) -> str | None:
    try:
        m = _yahoo_get(ticker, {"range": "1d", "interval": "1d"}).get("meta") or {}
    except Exception:
        return None
    return m.get("longName") or m.get("shortName")


_YAHOO_TYPES = {"EQUITY": "CS", "ETF": "ETF", "MUTUALFUND": "FUND", "INDEX": "INDEX", "FUTURE": "FUTURE",
                "CURRENCY": "CURRENCY", "CRYPTOCURRENCY": "CRYPTO"}


def get_instrument_type(ticker: str) -> str | None:
    """证券类型，口径同 Polygon 的 type（CS/ADRC/ETF…）。取不到返回 None，调用方不能把 None 当成"是个股"。

    Yahoo 只分到 EQUITY，普通股和 ADR 都算 CS；优先股、权证分不出来，够用：
    我们要拦的是 ETF、基金这类不属于个股研究范围的东西。
    """
    if has_polygon():
        try:
            from .providers import polygon
            r = polygon._get(f"/v3/reference/tickers/{polygon._sym(ticker)}").get("results") or {}
            if r.get("type"):
                return r["type"]
        except Exception:
            pass
    q = get_intraday(ticker)
    t = q.get("type") if q else None
    return _YAHOO_TYPES.get(t, t)


def get_news(ticker: str, limit: int = 6) -> list[dict]:
    """个股新闻标题：Polygon 付费档（带 AI 情绪标注）→ Finnhub → yfinance。"""
    if has_polygon() and _is_stock(ticker):
        try:
            from .providers import polygon
            items = polygon.get_news(ticker, limit)
            if items:
                return items
        except Exception:
            pass
    if os.environ.get("FINNHUB_API_KEY") and _is_stock(ticker):
        try:
            from .providers import finnhub
            items = finnhub.company_news(ticker, limit=limit)
            if items:
                return items
        except Exception:
            pass
    return _yf_news(ticker, limit)


def get_market_news(limit: int = 8) -> list[dict]:
    """大盘要闻。Finnhub 的 company-news 对 SPY 这类 ETF 基本没内容，所以走它的综合新闻。"""
    if has_polygon():
        return get_news("SPY", limit)
    if os.environ.get("FINNHUB_API_KEY"):
        try:
            from .providers import finnhub
            items = finnhub.general_news(limit)
            if items:
                return items
        except Exception:
            pass
    return _yf_news("SPY", limit)


def _yf_news(ticker: str, limit: int) -> list[dict]:
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


_poly_lock = threading.Lock()
_poly_state: str | None = None


def polygon_status() -> str:
    """ok=付费档可用；free=免费档（快照无权限或被限流）；error=探测失败；no_key=没配 key。整个进程只探测一次。

    免费档每分钟 5 次：只够前几只票，其余 429，来源会混杂，而且两边复权口径偶有出入，所以干脆不用。
    """
    global _poly_state
    if not os.environ.get("POLYGON_API_KEY"):
        return "no_key"
    with _poly_lock:
        if _poly_state is None:
            try:
                r = requests.get("https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/SPY",
                                 params={"apiKey": os.environ["POLYGON_API_KEY"]}, timeout=10)
                _poly_state = "ok" if r.status_code == 200 else "free"
            except requests.RequestException:
                _poly_state = "error"
    return _poly_state


def has_polygon() -> bool:
    return polygon_status() == "ok"


CTB_ENRICH_THRESHOLD = 8.0  # SI占股本%超过此值才花 Ortex credits 补借券费率


def get_short_stats(ticker: str) -> dict | None:
    """空头状态。主力 = Polygon(含在套餐里,不限调用)：官方SI/回补天数/每日做空量占比；
    Ortex 降级为增强：仅对高做空票补借券费率(每票1次调用)。按最近交易日缓存。"""
    if not has_polygon() or not _is_stock(ticker):
        return None
    d = _now_ct().date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    p = CACHE_DIR / f"shortv2_{ticker}_{d.isoformat()}.pkl"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if p.exists():
        return pd.read_pickle(p)

    s = None
    if has_polygon():
        try:
            from .providers import polygon
            s = polygon.get_short_stats(ticker)
        except Exception:
            s = None
    # Ortex 按 credits 计费。Polygon 不可用时不再用它整套回退：观察池上百只票一天就能烧掉一个周期的额度。
    if s and os.environ.get("ORTEX_API_KEY") and (s.get("si_pct_out") or s.get("si_pct_float") or 0) >= CTB_ENRICH_THRESHOLD:
        try:
            from .providers import ortex
            ctb = ortex.ctb_only(ticker)
            if ctb is not None:
                s["ctb_new"] = ctb
        except Exception:
            pass

    _atomic_pickle(s, p)  # 成功结果都缓存，包括"无数据"(None)，避免同日反复打 API
    return s


def get_minute_profile_candidates(ticker: str) -> list | None:
    """分钟级成交密集区候选位（Polygon）。不可用时返回 None，引擎退回日线近似。"""
    if not has_polygon() or not _is_stock(ticker):
        return None
    p = _cache_path(f"minprof_{ticker}", by_session=True)
    if p.exists():
        return pd.read_pickle(p)
    try:
        from .providers import polygon
        mp = polygon.get_minute_profile(ticker)
    except Exception:
        return None
    _atomic_pickle(mp, p)
    return mp


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
    _atomic_pickle(w, p)
    return w


def get_earnings_calendar(days: int = 14, problems: list[str] | None = None) -> list[dict]:
    """未来 N 天财报日历（Finnhub）。没配 key 时返回空列表。没查全的部分写进 problems，报告要说出来。"""
    if not os.environ.get("FINNHUB_API_KEY"):
        return []
    try:
        from .providers import finnhub
        return finnhub.earnings_calendar(days, problems)
    except Exception as e:
        if problems is not None:
            problems.append(f"日历取数失败: {e}")
        return []


def get_next_earnings(ticker: str, days: int = 45, problems: list[str] | None = None) -> str | None:
    """按代码单独查下一次财报日。批量日历查不到的票用（见 watchlist.yaml 的 earnings_lookup）。"""
    if not os.environ.get("FINNHUB_API_KEY"):
        return None
    try:
        from .providers import finnhub
        return finnhub.next_earnings(ticker, days)
    except Exception:
        if problems is not None:
            problems.append(f"{ticker} 单独查询失败")
        return None
