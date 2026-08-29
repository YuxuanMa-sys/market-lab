"""生成盘前/盘后 HTML 报告（自包含单文件，报告存 reports/）。"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from jinja2 import Template

from . import data, market, stock
from .advice import ACTION_URGENCY, URGENT_ACTIONS, advise, market_mode
from .sizing import apply_sizing

ROOT = Path(__file__).resolve().parents[2]
REPORTS_DIR = ROOT / "reports"

TEMPLATE = Template("""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }}</title>
<style>
:root {
  --bg: #f7f8fa; --card: #ffffff; --text: #1a1f2b; --muted: #6b7280;
  --up: #0a8a4a; --down: #d3372c; --accent: #2456d6; --border: #e5e7eb;
  --warn-bg: #fef3c7; --warn-text: #92400e;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #101318; --card: #1a1f28; --text: #e6e9ef; --muted: #8b93a3;
    --up: #34c97b; --down: #ff6b5e; --accent: #6c96ff; --border: #2a303c;
    --warn-bg: #3a2f10; --warn-text: #fbd38d;
  }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 24px 16px; background: var(--bg); color: var(--text);
  font: 15px/1.65 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }
.wrap { max-width: 880px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 28px 0 10px; }
.sub { color: var(--muted); font-size: 13px; margin-bottom: 20px; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 12px;
  padding: 16px 18px; margin-bottom: 14px; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); }
th { color: var(--muted); font-weight: 500; font-size: 12.5px; }
tr:last-child td { border-bottom: none; }
.up { color: var(--up); } .down { color: var(--down); }
.badge { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 13px;
  border: 1px solid var(--border); margin-right: 6px; }
.badge.regime { background: var(--accent); color: #fff; border: none; }
.gauge { font-size: 14px; }
.gauge b { font-size: 18px; }
.muted { color: var(--muted); font-size: 13px; }
.src { color: var(--muted); font-size: 12px; }
.event { background: var(--warn-bg); color: var(--warn-text); border-radius: 8px;
  padding: 6px 10px; margin: 4px 0; font-size: 13.5px; }
.plan { border-left: 3px solid var(--accent); padding: 6px 10px; margin: 8px 0;
  font-size: 13.5px; background: color-mix(in srgb, var(--accent) 7%, transparent); border-radius: 0 8px 8px 0; }
.advice { border-left: 3px solid var(--up); padding: 6px 10px; margin: 8px 0;
  font-size: 13.5px; background: color-mix(in srgb, var(--up) 8%, transparent); border-radius: 0 8px 8px 0; }
.advice.urgent { border-left-color: var(--down); background: color-mix(in srgb, var(--down) 9%, transparent); }
.ticker-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.ticker-head .sym { font-size: 18px; font-weight: 700; }
.news li { margin: 3px 0; font-size: 13.5px; }
.news a { color: var(--accent); text-decoration: none; }
.scroll { overflow-x: auto; }
.footer { color: var(--muted); font-size: 12px; margin-top: 30px; }
</style>
</head>
<body>
<div class="wrap">
<h1>{{ title }}</h1>
<div class="sub">{{ generated_at }} · 数据源: {{ data_sources }}{% if session == "premarket" %} · 期指为最近成交价{% endif %}</div>

<div class="card">
  <span class="badge regime">市场状态：{{ mkt.regime.label }}</span>
  <span class="muted">{{ mkt.regime.desc }}</span>
  <div class="gauge" style="margin-top:10px">
    大盘抄底温度计：<b>{{ mkt.dip_gauge.score|int }}</b>/100 —— {{ mkt.dip_gauge.band }}
    {% if mkt.dip_gauge.reasons %}
    <ul class="muted" style="margin:4px 0 0">{% for r in mkt.dip_gauge.reasons %}<li>{{ r }}</li>{% endfor %}</ul>
    {% endif %}
  </div>
  {% if mkt.leverage %}
  <div class="gauge" style="margin-top:8px">
    杠杆红绿灯：<b>{{ "🔴" if mkt.leverage.light == "红" else "🟢" if mkt.leverage.light == "绿" else "🟡" }} {{ mkt.leverage.light }}</b>
    <span class="muted">{{ mkt.leverage.desc }}</span>
  </div>
  {% endif %}
  <div class="gauge" style="margin-top:8px">当前调度模式：<b>{{ mmode_desc }}</b></div>
</div>

{% if order_sheet %}
<h2>📋 今日操作单</h2>
<div class="card scroll">
<table>
<tr><th>票</th><th>身份</th><th>动作</th><th>挂单（份额为该票计划仓位的比例）</th></tr>
{% for r in order_sheet %}
<tr><td><b>{{ r.ticker }}</b></td>
<td>{% if r.pnl_pct is not none %}持仓 {{ "%+.1f%%"|format(r.pnl_pct) }}{% else %}候选{% endif %}</td>
<td><b>{{ r.action }}</b></td>
<td>{% for o in r.orders %}{{ o.side }}·{{ o.type }} <b>{{ o.price }}</b>（{{ o.portion }}{% if o.pct is defined and o.pct %} ≈ {{ o.pct }}%仓{% endif %}{% if o.shares is defined and o.shares %} ≈ {{ o.shares }}股{% elif o.usd is defined and o.usd %} ≈ ${{ o.usd }}{% endif %}）{{ o.note }}{% if not loop.last %}<br>{% endif %}{% endfor %}</td></tr>
{% endfor %}
</table>
<div class="muted">其余 {{ n_noop }} 只今日无操作。挂单价为区间代表值，可在区间内酌情微调；止损单建议用 GTC。</div>
</div>

<div class="card">
  <b>风险仪表盘</b>　总开放风险 <b>{{ risk.open_risk_pct }}%</b>/{{ risk.open_risk_cap|int }}%
  {% if risk.cash_pct is not none %}　现金 <b>{{ risk.cash_pct }}%</b>{% endif %}
  　单笔风险 {{ risk.risk_per_trade }}%{% if risk.throttled %}（⛔熔断减半中）{% endif %}
  <div class="muted" style="margin-top:4px">簇占比：{% for c, v in risk.cluster_sums.items() %}{{ c }} {{ v }}%{% if not loop.last %} · {% endif %}{% endfor %}（簇上限 {{ risk.cluster_cap|int }}%）
  {% if risk.sub_cluster_sums %}<br>子簇：{% for c, v in risk.sub_cluster_sums.items() %}{{ c }} {{ v }}%{% if not loop.last %} · {% endif %}{% endfor %}（子簇上限 {{ risk.sub_cluster_cap|int }}%）{% endif %}
  {% if not risk.has_account_file %}<br>提示：本地未找到 account.yaml，挂单只显示仓位%不显示股数（云端属正常）{% endif %}</div>
  {% for w in risk.warnings %}<div class="event">{{ w }}</div>{% endfor %}
</div>
{% endif %}

<h2>指数与隔夜</h2>
<div class="card scroll">
<table>
<tr><th>名称</th><th>最新</th><th>涨跌幅</th></tr>
{% for q in mkt.quotes %}
<tr><td>{{ q.name }} ({{ q.ticker }})</td><td>{{ q.last }}</td>
<td class="{{ 'up' if q.chg_pct and q.chg_pct > 0 else 'down' if q.chg_pct and q.chg_pct < 0 else '' }}">
{{ "%+.2f%%"|format(q.chg_pct) if q.chg_pct is not none else "-" }}</td></tr>
{% endfor %}
</table>
</div>

<h2>市场宽度</h2>
<div class="card">
{% if mkt.breadth %}
大盘股样本({{ mkt.breadth.sample }}只)中：<b>{{ mkt.breadth.pct_above_50dma }}%</b> 站上50日均线，
<b>{{ mkt.breadth.pct_above_200dma }}%</b> 站上200日均线，今日 <b>{{ mkt.breadth.pct_up_today }}%</b> 上涨。
VIX {{ mkt.vix.value }}（2年 {{ mkt.vix.percentile_2y|int }}% 分位{% if mkt.vix.term_ratio %}，期限结构 {{ mkt.vix.term_ratio }}{% if mkt.vix.backwardation %} <b>倒挂——恐慌高潮特征</b>{% endif %}{% endif %}）。
{% else %}宽度数据获取失败。{% endif %}
</div>

{% for idx_name, idx in [("SPY 标普500", mkt.spy), ("QQQ 纳指100", mkt.qqq)] %}
<h2>{{ idx_name }} 关键位（现价 {{ idx.price }}）</h2>
<div class="card scroll">
{% for e in idx.events %}<div class="event">⚠ {{ e.type }} {{ e.zone.low }}–{{ e.zone.high }} 区间（{{ e.bars_ago }}个交易日前{% if e.vol_confirm %}，放量{% endif %}）</div>{% endfor %}
{% if idx.inside_zone %}<div class="event">当前正处于 {{ idx.inside_zone.low }}–{{ idx.inside_zone.high }} 争夺区（强度 {{ idx.inside_zone.score }}）</div>{% endif %}
<table>
<tr><th>方向</th><th>区间</th><th>距离</th><th>强度</th><th>依据</th></tr>
{% for z in idx.resistances|reverse %}
<tr><td class="down">压力</td><td>{{ z.low }} – {{ z.high }}</td><td>{{ "%+.1f%%"|format(z.dist_pct) }}</td><td>{{ z.score }}</td><td class="src">{{ z.sources|join("、") }}</td></tr>
{% endfor %}
{% for z in idx.supports %}
<tr><td class="up">支撑</td><td>{{ z.low }} – {{ z.high }}</td><td>{{ "%+.1f%%"|format(z.dist_pct) }}</td><td>{{ z.score }}</td><td class="src">{{ z.sources|join("、") }}</td></tr>
{% endfor %}
</table>
</div>
{% endfor %}

<h2>自选股</h2>
{% for s in stocks %}
<div class="card">
  <div class="ticker-head">
    <span class="sym">{{ s.ticker }}</span>
    <span>{{ s.price }}</span>
    <span class="{{ 'up' if s.chg_pct > 0 else 'down' if s.chg_pct < 0 else '' }}">{{ "%+.2f%%"|format(s.chg_pct) }}</span>
    <span class="badge">{{ s.trend }}</span>
    <span class="badge">RSI {{ s.rsi }}</span>
    <span class="badge">抄底分 {{ s.dip.score|int }} · {{ s.dip.band }}</span>
    {% if s.short %}<span class="badge">空头:{% if s.short.si_pct_out is defined and s.short.si_pct_out is not none %} SI {{ s.short.si_pct_out }}%股本{% if s.short.si_change_pct is defined and s.short.si_change_pct is not none %}（上期{{ "%+.1f%%"|format(s.short.si_change_pct) }}）{% endif %}{% elif s.short.si_pct_float is defined and s.short.si_pct_float is not none %} SI {{ s.short.si_pct_float }}%流通盘{% endif %}{% if s.short.dtc is defined and s.short.dtc %} · 回补{{ s.short.dtc }}天{% endif %}{% if s.short.svr_5d is defined and s.short.svr_5d %} · 日做空量{{ s.short.svr_5d }}%(5日均){% endif %}{% if s.short.ctb_new is defined and s.short.ctb_new is not none %} · 借券费{{ s.short.ctb_new }}%{% endif %}</span>{% endif %}
    {% if s.note %}<span class="muted">备注: {{ s.note }}</span>{% endif %}
  </div>
  {% if s.advice %}
  <div class="advice{{ ' urgent' if s.advice.action in urgent_actions else '' }}">
    🎯 <b>{{ s.advice.action }}</b>{% if s.advice.pnl_pct is defined and s.advice.pnl_pct is not none %} <b>[持仓 · {{ s.advice.mode }} · 浮盈 {{ "%+.1f%%"|format(s.advice.pnl_pct) }}]</b>{% else %}（{{ s.advice.mode }}）{% endif %}
    — {{ s.advice.reasons|join("；") }}
    {% if s.advice.orders %}<div class="muted" style="margin-top:4px">{% for o in s.advice.orders %}📌 {{ o.side }}·{{ o.type }} @ <b>{{ o.price }}</b>（{{ o.portion }}）{{ o.note }}{% if not loop.last %}<br>{% endif %}{% endfor %}</div>{% endif %}
  </div>
  {% endif %}
  {% if s.earnings_date %}<div class="event">📅 {{ s.earnings_date }} 财报——财报跳空会直接跳过支撑位，财报前带杠杆过夜等于赌跳空</div>{% endif %}
  {% if s.plan and s.plan.status != "无计划" %}
  <div class="plan">
    📋 <b>{{ s.plan.status }}</b> · 进场参考区 <b>{{ s.plan.entry_zone.low }} – {{ s.plan.entry_zone.high }}</b>
    {% if s.plan.targets %}· 止盈参考 {% for t in s.plan.targets %}<b>TP{{ loop.index }} {{ t.low }}–{{ t.high }}</b>（{{ "%+.1f%%"|format(t.pct_from_entry) }}）{% if not loop.last %} → {% endif %}{% endfor %}{% else %}· 基准目标 <b>{{ s.plan.fallback_target }}</b>（+{{ s.plan.pref_min_pct|int }}%）{% endif %}
    · 无效位 {{ s.plan.invalid_below }}{% if s.plan.if_invalid_next_support %}（破了看 {{ s.plan.if_invalid_next_support.low }}–{{ s.plan.if_invalid_next_support.high }}）{% endif %}
    {% if s.plan.risk_reward %} · 盈亏比 {{ s.plan.risk_reward }}{% endif %}
    <div class="muted">{{ s.plan.status_note }}；{{ s.plan.target_note }}</div>
  </div>
  {% endif %}
  {% for e in s.events %}<div class="event">⚠ {{ e.type }} {{ e.zone.low }}–{{ e.zone.high }}（{{ e.bars_ago }}个交易日前{% if e.vol_confirm %}，放量{% endif %}）</div>{% endfor %}
  {% if s.inside_zone %}<div class="event">现价处于 {{ s.inside_zone.low }}–{{ s.inside_zone.high }} 争夺区，多空正在这里交战</div>{% endif %}
  {% if s.dip.reasons %}<div class="muted">抄底依据：{{ s.dip.reasons|join("；") }}</div>{% endif %}
  <div class="scroll" style="margin-top:8px">
  <table>
  <tr><th>方向</th><th>区间</th><th>距离</th><th>强度</th><th>依据</th><th>若跌破，下一档</th></tr>
  {% for z in s.resistances|reverse %}
  <tr><td class="down">压力</td><td>{{ z.low }} – {{ z.high }}</td><td>{{ "%+.1f%%"|format(z.dist_pct) }}</td><td>{{ z.score }}</td><td class="src">{{ z.sources|join("、") }}</td><td>-</td></tr>
  {% endfor %}
  {% for z in s.supports %}
  <tr><td class="up">支撑</td><td>{{ z.low }} – {{ z.high }}</td><td>{{ "%+.1f%%"|format(z.dist_pct) }}</td><td>{{ z.score }}</td><td class="src">{{ z.sources|join("、") }}</td>
  <td>{% if z.if_break_next %}{{ z.if_break_next.low }} – {{ z.if_break_next.high }}（{{ "%+.1f%%"|format(z.if_break_next.dist_pct) }}）{% else %}-{% endif %}</td></tr>
  {% endfor %}
  </table>
  </div>
  {% if s.news %}
  <ul class="news">
  {% for n in s.news[:4] %}<li>{% if n.sentiment %}<b class="{{ 'up' if n.sentiment == '利好' else 'down' if n.sentiment == '利空' else 'muted' }}">[{{ n.sentiment }}]</b> {% endif %}<a href="{{ n.url }}">{{ n.title }}</a> <span class="muted">{{ n.source }}</span></li>{% endfor %}
  </ul>
  {% endif %}
</div>
{% endfor %}

{% if err_stocks %}
<div class="card">
  <b>⚠ 以下自选股本次取数失败，报告中缺席：</b>
  <ul class="muted" style="margin:6px 0 0">{% for e in err_stocks %}<li>{{ e.ticker }}：{{ e.error }}</li>{% endfor %}</ul>
</div>
{% endif %}

{% if earnings %}
<h2>自选股财报日历（未来两周）</h2>
<div class="card scroll">
<table>
<tr><th>代码</th><th>日期</th><th>时段</th><th>EPS预期</th></tr>
{% for e in earnings %}
<tr><td>{{ e.symbol }}</td><td>{{ e.date }}</td><td>{{ e.hour or "-" }}</td><td>{{ e.eps_estimate if e.eps_estimate is not none else "-" }}</td></tr>
{% endfor %}
</table>
<div class="muted">财报是隔夜跳空的最大来源——持仓票临近财报时，支撑位可能直接被跳过。</div>
</div>
{% endif %}

{% if mkt.market_news %}
<h2>大盘相关新闻</h2>
<div class="card">
<ul class="news">
{% for n in mkt.market_news %}<li>{% if n.sentiment %}<b class="{{ 'up' if n.sentiment == '利好' else 'down' if n.sentiment == '利空' else 'muted' }}">[{{ n.sentiment }}]</b> {% endif %}<a href="{{ n.url }}">{{ n.title }}</a> <span class="muted">{{ n.source }}</span></li>{% endfor %}
</ul>
</div>
{% endif %}

<div class="footer">支撑压力位为概率区间而非精确预测；本报告是分析工具输出，不构成投资建议，买卖决定请自行做出。</div>
</div>
</body>
</html>
""", autoescape=True)


def load_watchlist() -> list[dict]:
    wl = yaml.safe_load((ROOT / "watchlist.yaml").read_text(encoding="utf-8"))
    return wl.get("tickers", [])


def generate(session: str = "premarket") -> tuple[Path, Path]:
    """生成报告，返回 (html路径, json路径)。json 给 Claude/程序读，html 给人看。"""
    now = datetime.now(ZoneInfo("America/Chicago"))
    mkt = market.overview(session)

    stocks = []
    for item in load_watchlist():
        sym = item["symbol"]
        try:
            s = stock.analyze(sym)
            s["note"] = item.get("note", "")
            stocks.append(s)
        except Exception as e:
            stocks.append({"ticker": sym, "error": str(e)})

    ok_stocks = [s for s in stocks if "error" not in s]

    mmode, mmode_desc = market_mode(mkt)
    wl_by_symbol = {i["symbol"]: i for i in load_watchlist()}
    for s in ok_stocks:
        s["advice"] = advise(s, wl_by_symbol.get(s["ticker"], {}), mmode)

    # 持仓在前（按动作紧急度），候选在后（按抄底分从高到低）
    ok_stocks.sort(key=lambda s: (
        0 if s["advice"].get("pnl_pct") is not None else 1,
        ACTION_URGENCY.get(s["advice"]["action"], 9),
        -s["dip"]["score"],
    ))

    risk_dashboard = apply_sizing(ok_stocks, wl_by_symbol)

    order_sheet = [
        {"ticker": s["ticker"], "action": s["advice"]["action"],
         "pnl_pct": s["advice"].get("pnl_pct"), "orders": s["advice"]["orders"]}
        for s in ok_stocks if s["advice"].get("orders")
    ]
    n_noop = len(ok_stocks) - len(order_sheet)

    wl_symbols = {item["symbol"] for item in load_watchlist()}
    earnings = [e for e in data.get_earnings_calendar(14) if e.get("symbol") in wl_symbols]
    edates = {e["symbol"]: e["date"] for e in earnings}
    for s in ok_stocks:
        s["earnings_date"] = edates.get(s["ticker"])

    title = f"{'盘前' if session == 'premarket' else '盘后'}分析 · {now.strftime('%Y-%m-%d')}"
    html = TEMPLATE.render(
        title=title,
        generated_at=now.strftime("%Y-%m-%d %H:%M %Z"),
        session=session,
        mkt=mkt,
        mmode_desc=mmode_desc,
        urgent_actions=URGENT_ACTIONS,
        order_sheet=order_sheet,
        n_noop=n_noop,
        risk=risk_dashboard,
        stocks=ok_stocks,
        err_stocks=[s for s in stocks if "error" in s],
        earnings=earnings,
        data_sources=(
            ("Polygon(15分钟延迟)" if data.has_polygon() else "yfinance(免费)")
            + (" + Ortex空头" if os.environ.get("ORTEX_API_KEY") else "")
            + (" + Finnhub财报日历" if os.environ.get("FINNHUB_API_KEY") else "")
        ),
    )

    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = now.strftime("%Y-%m-%d")
    html_path = REPORTS_DIR / f"{stamp}-{session}.html"
    json_path = REPORTS_DIR / f"{stamp}-{session}.json"
    html_path.write_text(html, encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {"market_mode": mmode_desc, "order_sheet": order_sheet, "risk_dashboard": risk_dashboard,
             "market": mkt, "stocks": stocks, "earnings": earnings},
            ensure_ascii=False, indent=1, default=str,
        ),
        encoding="utf-8",
    )
    return html_path, json_path
