"""Finnhub：分片封顶、翻页、按代码查财报。不走网络。"""
from datetime import date, datetime, timedelta, timezone

from market_lab.providers import finnhub


def test_calendar_chunk_at_cap_is_split(monkeypatch):
    """回归：财报季 5 天一片也会封顶（2026-11-02~05 实测 1519 条），被截掉的票就没有财报日。"""
    calls = []
    def fake(path, **p):
        d0, d1 = date.fromisoformat(p["from"]), date.fromisoformat(p["to"])
        calls.append((d0, d1))
        days = (d1 - d0).days + 1
        if days > 2:
            return {"earningsCalendar": [{"symbol": f"X{i}", "date": str(d0)} for i in range(finnhub.CAL_ROW_CAP)]}
        return {"earningsCalendar": [{"symbol": f"T{d0}{i}", "date": str(d0 + timedelta(days=i % days))} for i in range(4)]}
    monkeypatch.setattr(finnhub, "_get", fake)
    rows = finnhub.earnings_calendar(5)
    assert not any(r["symbol"].startswith("X") for r in rows)      # 封顶那一片不用，对半重查
    assert len(calls) > 1 and all((b - a).days <= 5 for a, b in calls)
    assert rows and rows == sorted(rows, key=lambda r: r["date"])


def test_calendar_failure_and_single_day_cap_are_reported(monkeypatch, capsys):
    def fake(path, **p):
        if p["from"] == str(date.today()):
            raise RuntimeError("boom")
        return {"earningsCalendar": [{"symbol": f"X{i}", "date": p["from"]} for i in range(finnhub.CAL_ROW_CAP)]}
    monkeypatch.setattr(finnhub, "_get", fake)
    finnhub.earnings_calendar(6)      # 第一片失败，第二片每天都封顶
    err = capsys.readouterr().err
    assert "财报日历不全" in err and "取数失败" in err and "单日触到" in err


def test_next_earnings_takes_date_only(monkeypatch):
    monkeypatch.setattr(finnhub, "_get", lambda path, **p: {"earningsCalendar": [
        {"symbol": "ABX.TO", "date": "2027-02-01"}, {"symbol": "ABX.TO", "date": "2026-11-09"}]})
    assert finnhub.next_earnings("B") == "2026-11-09"
    monkeypatch.setattr(finnhub, "_get", lambda path, **p: {"earningsCalendar": []})
    assert finnhub.next_earnings("GOOG") is None
    seen = {}
    monkeypatch.setattr(finnhub, "_get", lambda path, **p: seen.update(p) or {})
    finnhub.next_earnings("BRK-B")
    assert seen["symbol"] == "BRK.B"


def _rows(n, day, start=0):
    ts = int(datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc).timestamp())
    return [{"id": start + i, "headline": f"h{start + i}", "datetime": ts} for i in range(n)]


def test_news_pages_back_until_window_start(monkeypatch):
    """回归：单次最多约 250 条，NVDA 10 天的窗口只够 1.4 天，却算作扫过。"""
    today = datetime.now(timezone.utc).date()
    calls = []
    def fake(path, **p):
        calls.append(p["to"])
        to = date.fromisoformat(p["to"])
        if to == today:
            return _rows(250, today - timedelta(days=2))
        return _rows(100, today - timedelta(days=9), start=1000)
    monkeypatch.setattr(finnhub, "_get", fake)
    items, covered = finnhub.company_news_all("NVDA", days=10)
    assert len(items) == 350 and covered is None and len(calls) == 2
    assert calls[1] == str(today - timedelta(days=2))


def test_news_full_page_ending_on_start_day_keeps_going(monkeypatch):
    """回归：最后一页到了窗口第一天就停，那一天其实没取全（NVDA 10-26 少了 141/190 条），却报告成覆盖全了。"""
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=10)
    calls = []
    def fake(path, **p):
        calls.append(p["to"])
        to = date.fromisoformat(p["to"])
        if to == today:
            return _rows(250, start)
        return _rows(40, start, start=1000)
    monkeypatch.setattr(finnhub, "_get", fake)
    items, covered = finnhub.company_news_all("NVDA", days=10)
    assert len(items) == 290 and covered is None and calls == [str(today), str(start)]
    # 窗口第一天本身就超过上限：取不全，要报告出来，而且要停下来
    monkeypatch.setattr(finnhub, "_get", lambda path, **p: _rows(250, start))
    items, covered = finnhub.company_news_all("NVDA", days=10)
    assert len(items) == 250 and covered is not None


def test_news_short_page_is_complete(monkeypatch):
    today = datetime.now(timezone.utc).date()
    monkeypatch.setattr(finnhub, "_get", lambda path, **p: _rows(20, today))
    items, covered = finnhub.company_news_all("KO", days=10)
    assert len(items) == 20 and covered is None
    monkeypatch.setattr(finnhub, "_get", lambda path, **p: [])
    assert finnhub.company_news_all("KO", days=10) == ([], None)


def test_news_reports_coverage_when_it_cannot_finish(monkeypatch):
    today = datetime.now(timezone.utc).date()
    def fake(path, **p):
        to = date.fromisoformat(p["to"])
        return _rows(250, to, start=(today - to).days * 1000)     # 每天都超过上限
    monkeypatch.setattr(finnhub, "_get", fake)
    monkeypatch.setattr(finnhub, "NEWS_MAX_PAGES", 3)
    items, covered = finnhub.company_news_all("NVDA", days=10)
    assert len(items) == 750 and covered["days"] < 10 and len(covered["cut_days"]) == 3


def test_calendar_problems_reach_the_caller(monkeypatch):
    """回归：日历 429 时简报和 data 行都不提，📅 全部消失却看起来一切正常。"""
    from market_lab import data
    monkeypatch.setenv("FINNHUB_API_KEY", "x")
    def boom(path, **p):
        raise RuntimeError("429")
    monkeypatch.setattr(finnhub, "_get", boom)
    problems = []
    assert data.get_earnings_calendar(6, problems) == []
    assert problems and "取数失败" in problems[0]
    problems = []
    assert data.get_next_earnings("B", 45, problems) is None
    assert problems == ["B 单独查询失败"]
class _Resp:
    def __init__(self, code, reset=None):
        self.status_code, self.headers = code, ({"X-Ratelimit-Reset": str(reset)} if reset else {})
    def json(self):
        return {"ok": True}
    def raise_for_status(self):
        if self.status_code >= 400:
            raise finnhub.requests.HTTPError(str(self.status_code))


def test_429_waits_for_window_reset_then_stops_waiting(monkeypatch):
    """回归：另一个客户端占了窗口时，等 5 秒重试一次还是 429，窗口重置前每只票都算没扫成。"""
    monkeypatch.setenv("FINNHUB_API_KEY", "x")
    monkeypatch.setattr(finnhub, "_last_call", 0.0)
    monkeypatch.setattr(finnhub, "_wait_left", 50.0)
    slept = []
    monkeypatch.setattr(finnhub.time, "sleep", slept.append)
    now = finnhub.time.time()
    answers = [_Resp(429, int(now) + 30), _Resp(200)]
    monkeypatch.setattr(finnhub.requests, "get", lambda *a, **k: answers.pop(0))
    assert finnhub._get("x") == {"ok": True}
    assert any(25 < x <= 32 for x in slept)
    # 等待额度用完：不再等，直接算失败
    monkeypatch.setattr(finnhub, "_wait_left", 0.0)
    slept.clear()
    answers[:] = [_Resp(429, int(now) + 30)]
    try:
        finnhub._get("x")
        raise AssertionError("应该抛错")
    except finnhub.requests.HTTPError:
        pass
    assert all(x < 2 for x in slept)
