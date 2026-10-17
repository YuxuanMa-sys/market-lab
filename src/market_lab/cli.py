import argparse
import json
import sys

from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(prog="market-lab", description="美股支撑压力位与大盘分析")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_an = sub.add_parser("analyze", help="分析单只股票")
    p_an.add_argument("ticker")
    p_an.add_argument("--no-news", action="store_true")

    p_rep = sub.add_parser("report", help="生成盘前/盘后报告")
    p_rep.add_argument("session", choices=["premarket", "postmarket"])

    sub.add_parser("market", help="只输出大盘概览 JSON")

    p_bt = sub.add_parser("backtest", help="回测抄底分各分数段的历史胜率")
    p_bt.add_argument("--period", default="5y")

    p_lbt = sub.add_parser("levelbt", help="位置质量回测：支撑区守住率按强度分桶(引擎改动的裁判)")
    p_lbt.add_argument("--period", default="5y")
    p_lbt.add_argument("--tag", default="run")
    p_lbt.add_argument("--side", default="sup", choices=["sup", "res"])

    sub.add_parser("exitbt", help="出场策略锦标赛：同一信号集上对比9种止盈/止损策略")

    sub.add_parser("alerts", help="盘中检查：谁进入了进场参考区(供定时任务调用)")

    p_ad = sub.add_parser("advise", help="单票模式化买卖建议(结合大盘状态和持仓)")
    p_ad.add_argument("ticker")

    p_corr = sub.add_parser("corr", help="候选票与当前持仓组合的1年日收益相关性(分散度检验)")
    p_corr.add_argument("tickers", nargs="+")

    p_hy = sub.add_parser("hygiene", help="周度维护的数据卫生检查：个股类型/价格/流动性/数据完整(不评价值不值得买)")
    p_hy.add_argument("tickers", nargs="*", help="只查这些票；新票不在 watchlist 里也可以查。不填=查整个 watchlist")

    args = ap.parse_args()

    if args.cmd == "analyze":
        from .stock import analyze
        result = analyze(args.ticker.upper(), with_news=not args.no_news)
        json.dump(result, sys.stdout, ensure_ascii=False, indent=1, default=str)
        print()
    elif args.cmd == "market":
        from .market import overview
        json.dump(overview(), sys.stdout, ensure_ascii=False, indent=1, default=str)
        print()
    elif args.cmd == "report":
        from .report import generate
        html_path, json_path = generate(args.session)
        print(f"HTML: {html_path}")
        print(f"JSON: {json_path}")
    elif args.cmd == "advise":
        from .advice import advise, market_mode
        from .market import overview
        from .report import load_order_style, load_watchlist
        from .stock import analyze
        t = args.ticker.upper()
        mkt = overview()
        a = analyze(t)
        item = next((i for i in load_watchlist() if i["symbol"] == t), {})
        mmode, desc = market_mode(mkt)
        a["advice"] = advise(a, item, mmode, load_order_style())
        json.dump({"market_mode": desc, "ticker": t, "advice": a["advice"],
                   "plan": a["plan"], "dip": a["dip"]}, sys.stdout, ensure_ascii=False, indent=1, default=str)
        print()
    elif args.cmd == "corr":
        import pandas as pd
        from . import data
        from .report import load_watchlist
        wl_all = load_watchlist()
        pos = {i["symbol"]: float(i["pct"]) for i in wl_all
               if i.get("cost") is not None and i.get("pct")}
        basis = "持仓组合(按pct加权)"
        if not pos:
            # 空仓时的回退：用候选池等权组合当基准。空仓期筛新票仍需要分散度判断，
            # 此时"与池子整体的相关性"是合理代理——低相关票在池里仍是独立弹药。
            cand = [i["symbol"] for i in wl_all if i.get("cost") is None]
            targets = {t.upper() for t in args.tickers}
            pos = {t: 1.0 for t in cand if t.upper() not in targets}
            basis = f"候选池等权({len(pos)}只，因当前空仓)"
            if not pos:
                print(json.dumps({"error": "watchlist 为空，无法构建基准"}, ensure_ascii=False))
                return
        frames = {}
        for t, w in pos.items():
            try:
                frames[t] = data.get_daily(t, "1y")["Close"].pct_change() * w
            except Exception:
                continue
        basket = pd.DataFrame(frames).sum(axis=1) / sum(pos.values())
        out = {}
        for t in args.tickers:
            t = t.upper()
            try:
                r = data.get_daily(t, "1y")["Close"].pct_change()
                c = pd.concat([basket, r], axis=1, sort=True).dropna().corr().iloc[0, 1]
                out[t] = round(float(c), 2)
            except Exception as e:
                out[t] = f"FAIL: {e}"
        print(json.dumps({"基准": basis, "corr_vs_基准": out,
                          "阈值参考": "<0.4 好, 0.4-0.5 边缘, >0.5 不建议"},
                         ensure_ascii=False, indent=1))
    elif args.cmd == "hygiene":
        from .hygiene import check as hygiene_check
        from .report import load_watchlist
        wl_all = load_watchlist()
        only = [t.upper() for t in args.tickers] or None
        if only:   # 不在池子里的新候选也能查
            known = {i["symbol"] for i in wl_all}
            wl_all = wl_all + [{"symbol": t} for t in only if t not in known]
        json.dump(hygiene_check(wl_all, only), sys.stdout, ensure_ascii=False, indent=1, default=str)
        print()
    elif args.cmd == "alerts":
        from .alerts import check
        from .report import load_watchlist
        json.dump(check(load_watchlist()), sys.stdout, ensure_ascii=False, indent=1)
        print()
    elif args.cmd == "levelbt":
        from pathlib import Path
        from datetime import date
        from .levelbt import run as lrun
        from .report import load_watchlist
        tickers = [x["symbol"] for x in load_watchlist()]
        result = lrun(tickers, period=args.period, side=args.side)
        out = Path(__file__).resolve().parents[2] / "reports" / f"levelbt-{date.today().isoformat()}-{args.tag}.json"
        out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=1))
    elif args.cmd == "exitbt":
        from pathlib import Path
        from datetime import date
        from .backtest import exit_tournament
        from .report import load_watchlist
        tickers = [x["symbol"] for x in load_watchlist()]
        result = exit_tournament(tickers)
        out = Path(__file__).resolve().parents[2] / "reports" / f"exitbt-{date.today().isoformat()}.json"
        out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=1))
    elif args.cmd == "backtest":
        from pathlib import Path
        from datetime import date
        from .backtest import run
        from .report import load_watchlist
        tickers = [x["symbol"] for x in load_watchlist()]
        result = run(tickers, period=args.period)
        out = Path(__file__).resolve().parents[2] / "reports" / f"backtest-{date.today().isoformat()}.json"
        out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps({k: v for k, v in result.items() if k != "entries"}, ensure_ascii=False, indent=1))
        print(f"明细: {out}")


if __name__ == "__main__":
    main()
