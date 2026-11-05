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
    assert len(items) == 750 and covered is not None and covered < 10
