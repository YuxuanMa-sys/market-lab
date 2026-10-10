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
from .sizing import RISK_PER_TRADE_PCT, apply_sizing

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

{% if stop_sheet %}
<h2>🛡 今日止损单（全仓 GTC）</h2>
<div class="card scroll">
<table>
<tr><th>票</th><th>股数</th><th>现价</th><th>成本</th><th>止损价</th><th>距现价</th><th>财报</th><th>备注</th></tr>
{% for r in stop_sheet %}
<tr><td><b>{{ r.ticker }}</b></td><td>{{ r.shares if r.shares else "?" }}</td><td>{{ r.price }}</td><td>{{ r.cost }}</td>
<td><b>{% if r.stop is not none %}{{ r.stop }}{% else %}<span class="down">无</span>{% endif %}</b></td>
<td{% if r.dist_pct is not none and r.dist_pct < 4 %} class="down"{% endif %}>{% if r.dist_pct is not none %}{{ r.dist_pct }}%{% if r.dist_atr %} ({{ r.dist_atr }}ATR){% endif %}{% else %}-{% endif %}</td>
<td{% if r.days_to_earnings is not none and r.days_to_earnings <= 21 %} class="down"{% endif %}>{% if r.earnings %}{{ r.earnings }}{% if r.days_to_earnings is not none %} ({{ r.days_to_earnings }}天){% endif %}{% else %}<span class="muted">未知</span>{% endif %}</td>
<td class="muted">{% if r.warn %}<span class="down">{{ r.warn }}</span>{% else %}{% if r.locked_gain %}锁盈(止损>成本){% endif %}{% if r.dist_pct is not none and r.dist_pct < 4 %} 一个ATR内，今日可能触发{% endif %}{% endif %}</td></tr>
{% endfor %}
</table>
<div class="muted">止损价=持仓期内结构止损的历史最高值（只升不降；单日值=支撑区下沿-0.75/1.0 ATR）。Stop Limit 请把 limit 设在止损价下方约1%防跳空不成交——注意财报是盘后发布，跳空发生在次日开盘，止损触发后按市价成交，成交价就是跳空后的价格而不是你挂的止损价。</div>
</div>
{% endif %}

<div class="card">
  <b>风险仪表盘</b>　总开放风险 <b>{{ risk.open_risk_pct }}%</b>/{{ risk.open_risk_cap|int }}%
  {% if risk.cash_pct is not none %}　现金 <b>{{ risk.cash_pct }}%</b>{% endif %}
  {% if risk.deployed_pct %}　已投入 <b>{{ risk.deployed_pct }}%</b>（杠杆 {{ risk.leverage }}:1{% if risk.buying_power %}，购买力 ${{ "{:,}".format(risk.buying_power|int) }}{% endif %}）{% endif %}
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
    {% set tg = s.plan.targets_effective if s.plan.targets_effective is defined else s.plan.targets %}
    {% if tg %}· 止盈参考 {% for t in tg %}<b>TP{{ loop.index }} {{ t.low }}–{{ t.high }}</b>（{{ "%+.1f%%"|format(t.pct_from_entry) }}）{% if not loop.last %} → {% endif %}{% endfor %}
    {% elif s.advice.pnl_pct is not none %}· 止盈 <b>{{ "%.2f"|format(s.advice.cost_tp) if s.advice.cost_tp else "成本×1.10" }}</b>（成本×1.10，压力区无更高目标）
    {% else %}· 基准目标 <b>{{ s.plan.fallback_target }}</b>（+{{ s.plan.pref_min_pct|int }}%）{% endif %}
    {% if s.plan.targets_dropped %}<span class="muted">（已剔除 {% for t in s.plan.targets_dropped %}{{ t.low }}–{{ t.high }}{% if not loop.last %}, {% endif %}{% endfor %}：按进场区算的目标，低于你这笔的出场线）</span>{% endif %}
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


def load_order_style() -> str:
    """挂单风格：single(单笔全仓,当前Fidelity适配) / oco / tranche(1/3梯次)。"""
    try:
        wl = yaml.safe_load((ROOT / "watchlist.yaml").read_text(encoding="utf-8"))
        return str(wl.get("order_style", "tranche"))
    except Exception:
        return "tranche"


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

    # 财报日必须在 advise() 之前就位：原先这段在 80 行之后才跑，导致 advise() 里
    # a.get("earnings_date") 恒为 None、「财报降风险」那条分支从未触发过(死代码)，
    # HTML 上的 📅 横幅只是事后贴上去的装饰，没有任何挂单或动作反映它。
    # 窗口从 14 天拉到 45 天：持仓窗口是 15 个交易日(≈21天)，14 天的前瞻看不到
    # 自己持有期内就会撞上的财报。watchlist 里可用 earnings: YYYY-MM-DD 钉住
    # 公司已确认、而 Finnhub 日历没覆盖到的日期（覆盖很稀：10 个持仓里只认得 2 个）。
    wl_by_symbol = {i["symbol"]: i for i in load_watchlist()}
    wl_symbols = set(wl_by_symbol)
    earnings = [e for e in data.get_earnings_calendar(45) if e.get("symbol") in wl_symbols]
    edates = {e["symbol"]: e["date"] for e in earnings}
    for item in wl_by_symbol.values():
        if item.get("earnings"):
            edates[item["symbol"]] = str(item["earnings"])   # 人工确认日优先于日历
    for s in ok_stocks:
        s["earnings_date"] = edates.get(s["ticker"])
    have = {e["symbol"] for e in earnings}
    for item in wl_by_symbol.values():
        if item.get("earnings") and item["symbol"] not in have:
            earnings.append({"symbol": item["symbol"], "date": str(item["earnings"]),
                             "hour": "", "eps_estimate": None, "manual": True})
    earnings.sort(key=lambda e: e["date"])

    mmode, mmode_desc = market_mode(mkt)
    style = load_order_style()
    for s in ok_stocks:
        s["advice"] = advise(s, wl_by_symbol.get(s["ticker"], {}), mmode, style)

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

    # 今日止损单：每只持仓一行(票/现价/成本/止损价/距离)，按距离升序——用户每天早上照表挂 GTC 止损
    # 2026-10-10 修复：原先 "没有止损单就 continue" 会把【裸奔的持仓】从表里静默抹掉，
    # 而"这只票今天没有保护"恰恰是整张表最该喊出来的一行。现在每个持仓都占一行。
    stop_sheet = []
    for s in ok_stocks:
        item = wl_by_symbol.get(s["ticker"], {})
        if item.get("cost") is None:
            continue
        adv = s["advice"]
        stop_o = next((o for o in adv.get("orders", []) if o["type"] == "止损"), None)
        price, cost, atr_v = s["price"], float(item["cost"]), s.get("atr") or 0
        from .advice import _days_to
        de = _days_to(s.get("earnings_date"))
        row = {
            "ticker": s["ticker"], "price": price, "cost": cost,
            "action": adv.get("action"),
            "earnings": s.get("earnings_date"), "days_to_earnings": de,
            "stop": None, "dist_pct": None, "dist_atr": None,
            "locked_gain": False, "usd": None, "warn": "",
            # 股数用 watchlist 里记的真实持仓数：富达挂单要填数量，
            # 用仓位%反推会得到小数股(挂不上去)。清仓认错的行同样需要它。
            "shares": item.get("shares"),
        }
        if stop_o:
            stop = stop_o["price"]
            row.update({
                "stop": stop,
                "dist_pct": round((price - stop) / price * 100, 1),
                "dist_atr": round((price - stop) / atr_v, 2) if atr_v else None,
                "locked_gain": stop > cost,
                "shares": item.get("shares") or stop_o.get("shares"),
                "usd": stop_o.get("usd"),
            })
            # 噪音宽度告警：止损离现价不到 0.5 ATR，开盘的正常波动就能打掉它。
            # 不改价格(那是风险参数，需要回测背书)，只把事实喊出来让人决定。
            if row["dist_atr"] is not None and row["dist_atr"] < 0.5:
                row["warn"] = (f"⚠ 仅 {row['dist_atr']} ATR({row['dist_pct']}%)——噪音宽度，"
                               f"开盘波动极可能打掉；要么接受被扫，要么手动放宽到支撑区下沿")
            elif row["dist_atr"] is not None and row["dist_atr"] > 3.0:
                # 宽止损的问题通常不是"亏不起"(仓位小的话金额可能仍在预算内)，而是赔率：
                # 用 -28% 的风险去换 +10% 的目标，R:R 只有 0.36:1，再高的胜率也难为正。
                loss_pct = round(float(item.get("pct") or 0) * row["dist_pct"] / 100, 2)
                rr = round(10.0 / row["dist_pct"], 2) if row["dist_pct"] else None
                row["warn"] = (f"⚠ 远达 {row['dist_atr']} ATR({row['dist_pct']}%)：触发即亏账户 {loss_pct}%"
                               f"（单笔预算 {RISK_PER_TRADE_PCT}%，金额{'仍在' if loss_pct <= RISK_PER_TRADE_PCT else '已超'}预算内）。"
                               f"真正的问题是赔率——拿 {row['dist_pct']}% 风险换 +10% 目标，R:R 仅 {rr}:1；"
                               f"你买在最近强支撑上方太远，这笔没有可用的紧止损，要么减仓要么认了这个赔率")
        else:
            sell_now = next((o for o in adv.get("orders", [])
                             if o["side"] == "卖" and o["type"] == "限价"), None)
            if sell_now:
                row["warn"] = (f"⚠ 无止损单——动作是「{adv.get('action')}」，"
                               f"按计划今天就该用 {sell_now['price']} 卖单离场，不是挂止损等")
            else:
                row["warn"] = (f"🔴 无止损单且无卖单——动作「{adv.get('action')}」下这个仓位今天是裸奔的，"
                               f"需要人工决定止损位")
        stop_sheet.append(row)
    # 排序：裸奔/待离场的排最前，其余按距现价由近到远
    stop_sheet.sort(key=lambda r: (r["stop"] is not None, r["dist_pct"] if r["dist_pct"] is not None else -999))
    # 紧凑版：一条推送放得下全部持仓；没有止损的标成 ⚠ 而不是悄悄消失
    stop_line = " ".join(
        f"{r['ticker']}{r['stop']:g}" if r["stop"] is not None else f"{r['ticker']}⚠无止损"
        for r in stop_sheet
    )


    title = f"{'盘前' if session == 'premarket' else '盘后'}分析 · {now.strftime('%Y-%m-%d')}"
    html = TEMPLATE.render(
        title=title,
        generated_at=now.strftime("%Y-%m-%d %H:%M %Z"),
        session=session,
        mkt=mkt,
        mmode_desc=mmode_desc,
        urgent_actions=URGENT_ACTIONS,
        order_sheet=order_sheet,
        stop_sheet=stop_sheet,
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
            {"market_mode": mmode_desc, "order_sheet": order_sheet, "stop_sheet": stop_sheet, "stop_line": stop_line, "risk_dashboard": risk_dashboard,
             "market": mkt, "stocks": stocks, "earnings": earnings},
            ensure_ascii=False, indent=1, default=str,
        ),
        encoding="utf-8",
    )
    return html_path, json_path
