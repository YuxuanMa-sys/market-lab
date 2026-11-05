"""观察池分类、板块总结、简报与推送的口径。"""
import yaml

from market_lab import brief, sectors
from market_lab.report import ROOT


def _wl():
    return yaml.safe_load((ROOT / "watchlist.yaml").read_text(encoding="utf-8"))["tickers"]


def test_watchlist_is_clean():
    wl = _wl()
    syms = [i["symbol"] for i in wl]
    # YAML 会把没加引号的 ON 读成 True（安森美的代码就是 ON）
    assert all(isinstance(s, str) and s == s.upper() for s in syms)
    assert len(syms) == len(set(syms))
    cov = sectors.coverage(wl)
    assert cov["unclassified"] == [] and cov["thin"] == []
    assert all(i.get("note") for i in wl)


def test_taxonomy_subs_are_unique():
    tax = sectors.load_taxonomy()
    subs = [s for v in tax.values() for s in v["subs"]]
    assert len(subs) == len(set(subs))           # 细分名是 watchlist 里的外键，不能重名


def _stock(t, chg, dip=10, high=0, ret5=1.0, ret20=2.0):
    return {"ticker": t, "chg_pct": chg, "ret5": ret5, "ret20": ret20, "dip": {"score": dip}, "high": {"score": high}}


TAX = {"甲": {"etf": "AAA", "subs": ["a1", "a2"]}, "乙": {"etf": None, "subs": ["b1"]}, "丙": {"etf": None, "subs": ["c1"]}}
WL = {"X": {"sector": "a1"}, "Y": {"sector": "a1"}, "Z": {"sector": "a2"}, "W": {"sector": "b1"}, "H": {}}


def test_sector_summary_numbers_and_order():
    stocks = [_stock("X", -2.0, dip=60), _stock("Y", -4.0, dip=75), _stock("Z", 3.0, high=60),
              _stock("W", 1.0), _stock("H", 50.0)]       # H 是持仓但不在池子：不算进板块
    s = sectors.build_sector_summary(stocks, WL, spy_chg=0.5, etf_chg={"AAA": -1.0}, tax=TAX)
    assert [g["group"] for g in s["groups"]] == ["乙", "甲"]        # 丙没有票，不出现；按今日涨跌排序
    jia = s["groups"][1]
    assert jia["n"] == 3 and jia["avg"] == -1.0 and jia["vs_spy"] == -1.5 and jia["etf_chg"] == -1.0
    assert (jia["up"], jia["down"], jia["n_low"], jia["n_deep"], jia["n_high"]) == (1, 2, 2, 1, 1)
    assert jia["worst"] == {"ticker": "Y", "chg": -4.0} and jia["best"] == {"ticker": "Z", "chg": 3.0}
    assert [x["sub"] for x in jia["subs"]] == ["a2", "a1"] and jia["subs"][1]["avg"] == -3.0
    assert s["pool"]["n"] == 4
    assert brief.push_sectors(s).startswith("📊板块收盘(SPY+0.5%): 乙+1.0 甲-1.0")


def _low(t, dip, sector="存储", held=False, chg=-3.0):
    tier = "深度低位" if dip >= 70 else ("低位" if dip >= 55 else "接近低位")
    return {"ticker": t, "dip": dip, "tier": tier, "sector": sector, "held": held, "chg_pct": chg,
            "dd20_pct": -20.0, "off_52w_pct": -30.0, "earnings": None, "days_to_earnings": None}


def test_low_part_order_and_length():
    lows = [_low(f"T{i:02d}", 80 - i, sector=f"板块{i % 5}") for i in range(30)] + [_low("MINE", 56, "算力云", held=True)]
    text = brief.low_part(lows, "收盘低位", brief.PUSH_MAX)
    assert len(text) <= brief.PUSH_MAX
    assert text.startswith("📉收盘低位27只(深度11): 板块0 T0080")    # 深度低位在前
    assert "MINE★56" in text                                         # 持仓紧随其后，排在普通低位前面
    assert " +" in text and text.endswith("只")                       # 放不下的写 +N只


def test_low_part_stays_under_limit_with_many_small_sectors():
    lows = [_low(f"T{i:02d}", 90 - (i % 30), sector=f"很长的细分名{i}") for i in range(40)]
    text = brief.low_part(lows, "收盘低位", brief.PUSH_MAX)
    assert len(text) <= brief.PUSH_MAX and text.endswith("只")


HIGH = [{"ticker": "RGR", "score": 63, "tier": "高位", "new_entry": True, "extreme_runup": False},
        {"ticker": "QCOM", "score": 10, "tier": None}]


def test_postmarket_sends_two_pushes():
    s = sectors.build_sector_summary([_stock("X", -2.0), _stock("W", 1.0)], WL, spy_chg=0.5, tax=TAX)
    p = brief.pushes("postmarket", HIGH, [_low("A", 80)], s, "VIX 16.6 平静")
    assert len(p) == 2
    assert p[0] == "📈持仓: RGR 63高位🆕 QCOM 10\n📊板块收盘(SPY+0.5%): 乙+1.0 甲-2.0"
    assert p[1] == "📉收盘低位1只(深度1): 存储 A80\nVIX 16.6 平静"


def test_sector_push_keeps_both_ends_when_too_long():
    groups = [{"group": f"板块{i:02d}", "avg": 5.0 - i} for i in range(40)]
    text = brief.push_sectors({"groups": groups, "spy_chg": 0.1}, budget=100)
    assert len(text) <= 100 and "板块00+5.0" in text and "板块39-34.0" in text and " … " in text


def test_premarket_single_push_and_empty_cases():
    p = brief.pushes("premarket", HIGH, [_low("A", 80)], None, "")
    assert p == ["📈持仓: RGR 63高位🆕 QCOM 10\n📉低位1只(深度1): 存储 A80"]
    assert brief.pushes("premarket", [], [], None, "") == ["📉低位: 没有低位票"]
    assert "没有低位票" in brief.pushes("postmarket", [], [_low("A", 50)], None, "")[0]


def test_zero_is_not_signed():
    assert brief._pct(-0.02) == "0.0%" and brief._s(0.04) == "0.0" and brief._s(-1.26) == "-1.3" and brief._pct(None) == "-"


def test_pick_why_budget():
    lows = [_low(f"D{i}", 75, chg=-i) for i in range(20)] + [_low("MINE", 60, held=True)]
    high = [{"ticker": "RGR", "score": 63, "tier": "高位"}, {"ticker": "QCOM", "score": 10, "tier": None}]
    s = {"groups": [{"group": g, "vs_spy": v} for g, v in [("强1", 2.0), ("强2", 1.0), ("平", 0.1), ("弱2", -1.0), ("弱1", -2.0)]]}
    w = brief.pick_why("postmarket", high, lows, s)
    assert w["up"] == ["RGR"]
    assert w["down"][0] == "MINE" and w["down"][1] == "D19"        # 持仓优先，其次今天跌得最多的深度低位
    assert len(w["up"]) + len(w["down"]) == brief.WHY_TICKERS["postmarket"]
    assert w["sectors"] == ["强1", "强2", "弱2", "弱1"]             # 相对大盘太接近的板块不查
    assert brief.pick_why("premarket", high, lows, s)["sectors"] == []


def test_brief_collapses_long_low_list():
    lows = [_low("DEEP", 80)] + [_low(f"L{i:02d}", 60, sector="设备") for i in range(30)] + [_low("MINE", 58, held=True), _low("NEAR", 50)]
    why = {"up": [], "down": ["DEEP"], "sectors": []}
    md = brief.brief_md("postmarket", [], {}, lows, {"vix_line": "大盘环境。"}, None, why)
    assert "<<为什么跌:DEEP>>" in md and "MINE★" in md
    assert "L00[设备]" not in md and "其余低位（55-69）按细分：设备：L00 60" in md
    assert "接近低位：NEAR" in md and "<<大盘要闻>>" in md
    # 不给买点、止损、止盈
    for word in ("止损", "止盈", "买点", "该卖了", "容易回落"):
        assert word not in md


def _hold(n, score=63, tier="高位", new=False, extreme=False):
    return [{"ticker": f"H{i:02d}", "score": score, "tier": tier, "new_entry": new, "extreme_runup": extreme} for i in range(n)]


def _summary(n=15):
    return {"spy_chg": -1.6, "groups": [{"group": f"可选消费{i}", "avg": -3.2} for i in range(n)]}


def test_pushes_stay_within_budget_with_many_holdings():
    """回归：持仓段没有预算，20 只全在高位时盘前 384 字，排在后面的低位清单和 VIX 被手机截掉。"""
    lows = [_low(f"T{i:02d}", 80 - i, sector=f"板块{i % 5}") for i in range(40)]
    for n in (0, 1, 7, 15, 20, 60):
        for hold in (_hold(n), _hold(n, 72, "深度高位", True, True), _hold(n, 10, None)):
            for failed in (0, 3, 120):
                for session in ("premarket", "postmarket"):
                    for text in brief.pushes(session, hold, lows, _summary(), "VIX 16.6 平静", n_ok=100, n_failed=failed,
                                             stale_as_of="2026-11-04" if failed == 3 else None):
                        assert len(text) <= brief.PUSH_MAX, (n, failed, session, len(text), text)
                        if failed:
                            assert text.startswith(f"⚠{failed}只取数失败")
                    pre = brief.pushes(session, hold, lows, _summary(), "VIX 16.6 平静", n_ok=100, n_failed=failed)
                    if session == "premarket":
                        assert "📉低位26只" in pre[0] and pre[0].endswith("VIX 16.6 平静")
                    else:
                        assert "📊板块" in pre[0] and "📉收盘低位26只" in pre[1]


def test_holdings_part_keeps_failures_and_high_scores_first():
    rows = _hold(30, 10, None) + [{"ticker": "FAILX", "score": None}, {"ticker": "HOTT", "score": 88, "tier": "深度高位"}]
    text = brief.holdings_part(rows, 60)
    assert text.startswith("📈持仓: FAILX - HOTT 88深度高位") and text.endswith("只") and len(text) <= 60


def test_all_failed_never_says_no_low_stocks():
    """回归：Yahoo 限流时全部失败，推送原文是"没有低位票"，一个字不改发给用户。"""
    for session in ("premarket", "postmarket"):
        out = brief.pushes(session, _hold(2), [], {}, "VIX 16.6 平静", n_ok=0, n_failed=231)
        assert len(out) == 1 and "231" in out[0] and "失败" in out[0] and "没有低位票" not in out[0]
    assert "没有低位票" in brief.pushes("postmarket", _hold(2), [], {}, "", n_ok=231, n_failed=0)[1]


def test_stale_day_is_labelled():
    """回归：休市日 cron 照常触发，上一交易日的涨跌写成"今日"和"板块收盘"。"""
    assert brief.day_label("postmarket") == "今日" and brief.day_label("premarket") == "昨日"
    assert brief.day_label("postmarket", stale=True) == "上一交易日"
    out = brief.pushes("postmarket", _hold(1), [_low("T", 60)], _summary(2), "", n_ok=2, stale_as_of="2026-11-04")
    assert "📊板块11-04收盘" in out[0] and "📉11-04收盘低位" in out[1]
    why = {"up": [], "down": [], "sectors": []}
    md = brief.brief_md("postmarket", [], {}, [_low("T", 60)], {"vix_line": ""}, None, why,
                        as_of="2026-11-04", stale=True, failed=["AAA", "BBB"])
    assert md.startswith("数据日期：2026-11-04 收盘（今天没有新K线）\n⚠ 观察池 2 只取数失败")
    assert "AAA BBB" in md and "上一交易日-3.0%" in md and "今日" not in md
    md = brief.brief_md("postmarket", [], {}, [_low("T", 60)], {"vix_line": ""}, None, why, as_of="2026-11-05")
    assert md.startswith("数据日期：2026-11-05 收盘\n") and "今日-3.0%" in md and "【今日大盘】" in md and "失败" not in md


def test_brief_prints_dash_when_prev_score_missing():
    rows = [{"ticker": "SAP", "score": 53, "tier": "接近高位", "prev_score": None, "chg_pct": 4.2, "reasons": [], "notes": []}]
    md = brief.brief_md("postmarket", rows, {}, [], {"vix_line": ""}, None, {"up": [], "down": [], "sectors": []})
    assert "（上一日 -）" in md and "None" not in md
