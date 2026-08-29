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

    sub.add_parser("alerts", help="盘中检查：谁进入了进场参考区(供定时任务调用)")

    p_ad = sub.add_parser("advise", help="单票模式化买卖建议(结合大盘状态和持仓)")
    p_ad.add_argument("ticker")

    p_corr = sub.add_parser("corr", help="候选票与当前持仓组合的1年日收益相关性(分散度检验)")
    p_corr.add_argument("tickers", nargs="+")

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
        from .report import load_watchlist
        from .stock import analyze
        t = args.ticker.upper()
        mkt = overview()
        a = analyze(t)
        item = next((i for i in load_watchlist() if i["symbol"] == t), {})
        mmode, desc = market_mode(mkt)
        a["advice"] = advise(a, item, mmode)
        json.dump({"market_mode": desc, "ticker": t, "advice": a["advice"],
                   "plan": a["plan"], "dip": a["dip"]}, sys.stdout, ensure_ascii=False, indent=1, default=str)
        print()
    elif args.cmd == "corr":
        import pandas as pd
        from . import data
        from .report import load_watchlist
        pos = {i["symbol"]: float(i["pct"]) for i in load_watchlist()
               if i.get("cost") is not None and i.get("pct")}
        if not pos:
            print(json.dumps({"error": "watchlist 里没有带 pct 的持仓，无法构建组合基准"}, ensure_ascii=False))
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
        print(json.dumps({"corr_vs_持仓组合": out, "阈值参考": "<0.4 好, 0.4-0.5 边缘, >0.5 不建议"}, ensure_ascii=False, indent=1))
    elif args.cmd == "alerts":
        from .alerts import check
        from .report import load_watchlist
        json.dump(check(load_watchlist()), sys.stdout, ensure_ascii=False, indent=1)
        print()
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
