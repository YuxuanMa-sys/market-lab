"""季度复测：向量化分数必须和生产公式逐行一致，收益和统计口径要对。"""
import numpy as np
import pandas as pd

from market_lab import retest
from market_lab.stock import dip_score, high_score


def _walk(n=600, seed=1, drift=0.0, vol=0.025):
    rng = np.random.default_rng(seed)
    c = 100 * np.cumprod(1 + rng.normal(drift, vol, n))
    h = c * (1 + np.abs(rng.normal(0, 0.01, n)))
    l = c * (1 - np.abs(rng.normal(0, 0.01, n)))
    v = rng.lognormal(14, 0.5, n)
    return pd.DataFrame({"Open": np.r_[c[0], c[:-1]], "High": h, "Low": l, "Close": c, "Volume": v},
                        index=pd.bdate_range("2024-01-01", periods=n))


def test_vectorized_scores_match_production():
    for seed in (1, 2, 3):
        df = _walk(seed=seed)
        base, high = retest.dip_base(df), retest.high_points(df)
        for i in range(300, len(df), 3):
            sub = df.iloc[: i + 1]
            assert base.iloc[i] == dip_score(sub, [], 0)["score"], i     # 没有支撑区时就是 base
            assert high.iloc[i] == high_score(sub)["score"], i


def test_forward_returns_entry_and_delisting():
    idx = pd.bdate_range("2026-10-01", periods=30)
    c = np.linspace(100, 129, 30)
    df = pd.DataFrame({"Open": c - 1, "High": c + 1, "Low": c - 2, "Close": c, "Volume": 1e6}, index=idx)
    f = retest.forward_returns(df, idx[-1])
    assert abs(f["ret5"][0] - (c[5] / (c[1] - 1) - 1) * 100) < 1e-9       # t+1 开盘买、t+5 收盘卖
    assert np.isnan(f["ret5"][-2])                                        # 还在交易、结果没出来
    assert f["mdd10"][0] <= 0
    # 已停止交易：窗口超出末尾也要有结果。价格腰斩的按 0 回收，平稳的按最后收盘
    crash = df.copy()
    crash.loc[idx[-1], "Close"] = 40.0
    later = idx[-1] + pd.Timedelta(days=30)
    assert retest.forward_returns(crash, later)["ret20"][-3] == -100.0
    calm = retest.forward_returns(df, later)
    assert abs(calm["ret20"][-3] - (c[-1] / df["Open"].iloc[-2] - 1) * 100) < 1e-9


def _rows():
    days = pd.bdate_range("2026-10-16", periods=60)
    recs = []
    for d in days:
        for t, dip, r in (("A", 80, 2.0), ("B", 60, 1.0), ("C", 10, 0.0), ("D", 10, 0.0)):
            recs.append({"date": d, "ticker": t, "member": True, "dip": dip, "base": dip - 20 if dip > 50 else dip,
                         "high": 0, "ret5": r, "ret10": r, "ret20": r, "ret60": r, "mdd10": -5.0})
    recs.append({"date": days[0], "ticker": "X", "member": False, "dip": 90, "base": 90, "high": 0,
                 "ret5": 99, "ret10": 99, "ret20": 99, "ret60": 99, "mdd10": -5.0})
    return pd.DataFrame(recs)


def test_excess_groups_and_verdicts():
    rows = retest.add_excess(_rows())
    assert "X" not in set(rows["ticker"])                                # 非成员不进基准也不进统计
    res = retest.analyze_rows(rows)
    x = res["low"]["≥70"]["exc10"]
    assert x["mean"] == 1.25 and x["days"] == 50 and x["lo"] == x["hi"] == 1.25   # 最后 10 天窗口没走完
    assert res["low"]["<45"]["exc10"]["mean"] == -0.75
    assert res["compare"]["低位 ≥70 减 55-69（10日）"]["mean"] == 1.0
    assert retest.verdict(x) == "新数据里明显跑赢"
    assert retest.verdict({"days": 10, "lo": 1.0, "hi": 2.0}) == "样本还太小"
    assert retest.verdict({"days": 80, "lo": -1.0, "hi": 1.0}) == "和随机股没有可靠差别"


def test_unfinished_windows_are_blank_even_for_delisted():
    rows = _rows()
    last_days = sorted(rows["date"].unique())[-5:]
    # 窗口没走完时只有已停止交易的票有结果，这种日子不能算
    rows.loc[rows["date"].isin(last_days) & (rows["ticker"] != "A"), ["ret10"]] = np.nan
    res = retest.add_excess(rows)
    assert res.loc[res["date"].isin(last_days), "exc10"].isna().all()
    assert res.loc[res["date"] == last_days[0], "ret5"].isna().all()         # 倒数第 5 天，5 日窗口也没走完
    assert res.loc[res["date"] == sorted(rows["date"].unique())[-6], "ret5"].notna().all()


def test_report_never_changes_formula_on_small_sample():
    rows = retest.add_excess(_rows().query("date < '2026-11-02'"))
    res = retest.analyze_rows(rows)
    md, push = retest.render(res, {"run_date": "2027-01-05", "last_date": "2026-11-02", "universe": 4, "fetched": 4,
                                   "missing": 0, "missing_share": 0.0, "stale": 0})
    assert "样本太小" in md and "公式不动" in push
    assert "止盈" not in md and "该卖" not in md


def test_universe_file_is_fixed_and_clean():
    names = retest.load_universe()
    assert len(names) == len(set(names)) > 1500
    assert all(n == n.upper() for n in names) and "NVDA" in names and "BRK-B" in names
