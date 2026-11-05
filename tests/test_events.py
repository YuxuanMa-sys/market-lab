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
