"""硬事件扫描的标题筛选：要抓到被收购的票，又不能让无关新闻淹没。"""
from market_lab import events


def _hit(title, ticker, name):
    return events.is_hit(title, ticker, events.company_key(name))


def test_company_key():
    assert events.company_key("Lumentum Holdings Inc.") == "lumentum"
    assert events.company_key("Royal Caribbean Cruises Ltd.") == "royal caribbean"
    assert events.company_key("The Walt Disney Company") == "walt"
    assert events.company_key("Bank of America Corporation") == "bank of america"
    assert events.company_key(None) == ""


def test_real_targets_are_caught():
    assert _hit("Marvell to buy Nvidia-backed Lumentum in a $115bn all-stock deal", "LITE", "Lumentum Holdings Inc.")
    assert _hit("Marvell Agrees to $115B All-Stock Deal for Lumentum", "LITE", "Lumentum Holdings Inc.")
    assert _hit("What a $115 Billion Deal to Buy Lumentum Really Means for Marvell Stock Investors", "LITE", "Lumentum Holdings Inc.")
    assert _hit("Sturm Ruger to be acquired by Smith", "RGR", "Sturm, Ruger & Company, Inc.")
    assert _hit("Acme files for Chapter 11 bankruptcy", "ACME", "Acme Corp")
    assert _hit("Nasdaq moves to delist (ON) after filing delay", "ON", "ON Semiconductor Corporation")


def test_noise_is_dropped():
    # 收购别人
    assert not _hit("Caterpillar Acquires John Fabick Tractor; Financial Terms Not Disclosed", "CAT", "Caterpillar Inc.")
    assert not _hit("Vistra acquires Cogentrix, adding 5.5GW of natural gas capacity", "VST", "Vistra Corp.")
    # 只是别的公司的新闻挂到这只票上
    assert not _hit("Skyworks Stock Sinks as Android Woes Delay the Qorvo's Merger Payoff", "AAPL", "Apple Inc.")
    assert not _hit("BrenX Shares Halted On Circuit Breaker To The Upside", "POWL", "Powell Industries, Inc.")
    # 评级、荐股、买产品
    assert not _hit("StoneX Reiterates Buy on Advanced Micro Devices, Maintains $685 Price Target", "AMD", "Advanced Micro Devices, Inc.")
    assert not _hit("Should You Buy Micron Stock Before Its Report?", "MU", "Micron Technology, Inc.")
    assert not _hit("Hive lines up $129 million to buy Nvidia AI chips", "NVDA", "NVIDIA Corporation")
    assert not _hit("Too Late to Buy Coca-Cola? 1 Reason It Isn't.", "KO", "The Coca-Cola Company")
    assert not _hit("Tahnil R Davis Seizes Opportunity: Acquires $367K Worth Of Trade Desk Stock Options", "TTD", "The Trade Desk, Inc.")
    # 一两个字母的代码不能乱匹配
    assert not _hit("Merger talks on hold as markets turn", "ON", "ON Semiconductor Corporation")
    assert not _hit("T-Mobile buyout rumor", "T", "AT&T Inc.")


def test_abbreviated_names_and_aliases():
    """回归：U.S. Bancorp 的 key 曾是 "u s"，真实标题 184 条一条都匹配不上；Yahoo 的公司名也不是标题里的常用叫法。"""
    assert events.company_key("U.S. Bancorp") == "us bancorp"
    assert events.company_key("D.R. Horton, Inc.") == "dr horton"
    assert _hit("PNC to Acquire U.S. Bancorp in $90 billion deal", "USB", "U.S. Bancorp")
    assert _hit("D.R. Horton to Be Acquired in Merger", "DHI", "D.R. Horton, Inc.")
    assert _hit("Disney to be acquired by Apple", "DIS", "The Walt Disney Company")
    assert _hit("Google to Spin Off Waymo", "GOOG", "Alphabet Inc.")
    assert _hit("Citi to Be Acquired by JPMorgan", "C", "Citigroup Inc.")
    assert _hit("Onsemi to Be Acquired by Infineon", "ON", "ON Semiconductor Corporation")
    assert _hit("Southern Co Merger With Duke Announced", "SO", "The Southern Company")
    # 词边界
    assert not _hit("Citizens Financial to be acquired by PNC", "C", "Citigroup Inc.")
    assert not _hit("Southern Copper merger talks", "SO", "The Southern Company")
    assert not events.mentions("Amazon.com results", "USB", "us bancorp")


def test_real_titles_that_were_missed():
    assert _hit("Marvell to purchase Lumentum for $115 billion in AI connectivity push", "LITE", "Lumentum Holdings Inc.")
    assert _hit("Salesforce is buying Braze for $5.7 billion", "BRZE", "Braze, Inc.")
    assert _hit("GM to be acquired by Toyota", "GM", "General Motors Company")
    assert not _hit("GM to buy more chips from Nvidia", "GM", "General Motors Company")


def test_more_event_wordings():
    for title in ("Block to change ticker symbol to XYZ", "Block and Square to merge in $115B deal", "Block sold to Marvell for $115B",
                  "KKR to take Block private in $20B deal", "Block goes bankrupt after failed refinancing",
                  "Block receives Nasdaq deficiency notice", "Trading in Block suspended by NYSE"):
        assert _hit(title, "XYZ", "Block, Inc."), title
    assert not _hit("Block suspended its dividend", "XYZ", "Block, Inc.")


def _scan(monkeypatch, names, news):
    from market_lab import data
    from market_lab.providers import finnhub
    monkeypatch.setenv("FINNHUB_API_KEY", "x")
    monkeypatch.setattr(data, "get_company_name", lambda t: names.get(t))
    monkeypatch.setattr(finnhub, "company_news_all", news)


def test_scan_reports_unknown_names_and_partial_coverage(monkeypatch):
    """回归：Yahoo 限流时公司名全取不到，收购类标题全部漏掉，输出却是"全部扫过、零命中"。"""
    def news(t, days=10):
        if t == "BAD":
            raise RuntimeError("429")
        return [{"title": "Marvell to buy Lumentum"}], (1.4 if t == "NVDA" else None)
    _scan(monkeypatch, {"LITE": "Lumentum Holdings Inc.", "NVDA": "NVIDIA Corporation"}, news)
    res = events.scan(["LITE", "NVDA", "NONAME", "BAD"])
    assert res["hits"] == {"LITE": ["Marvell to buy Lumentum"]}
    assert res["name_unknown"] == ["NONAME", "BAD"] and res["failed"] == ["BAD"]
    assert res["partial"] == {"NVDA": 1.4} and res["scanned"] == 3


def test_candidate_mode_shows_deals_named_differently(monkeypatch):
    """回归：入池检查放过了 DoorDash 收购 Instacart（Yahoo 叫 Maplebear，标题不提这个名字）。"""
    import os
    from market_lab import data
    from market_lab.providers import finnhub
    monkeypatch.setitem(os.environ, "FINNHUB_API_KEY", "x")
    titles = ["DoorDash to acquire Instacart for $17 billion", "Too Late to Buy Instacart?", "Maplebear Q3 revenue rises"]
    monkeypatch.setattr(finnhub, "company_news_all", lambda t, days=10: ([{"title": x} for x in titles], None))
    monkeypatch.setattr(data, "get_company_name", lambda t: "Maplebear Inc.")
    assert events.scan(["CART"])["hits"] == {}
    assert events.scan(["CART"], candidates=True)["hits"] == {"CART": ["DoorDash to acquire Instacart for $17 billion"]}
