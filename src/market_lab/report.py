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

<h2>📈 持仓高位分</h2>
{% if high_list %}
<div class="card">
  {{ high_context.definition }}
  <div class="muted" style="margin-top:4px">{{ high_context.market_line }}</div>
</div>
<div class="card scroll">
<table>
<tr><th>持仓</th><th>档位</th><th>高位分</th><th>昨日</th><th>现价</th><th>今日</th><th>RSI</th><th>ATR%</th><th>高于20日线</th><th>20日涨幅</th><th>距52周高</th><th>抄底分</th><th>财报</th><th>为什么高</th></tr>
{% for r in high_list %}
<tr><td><b>{{ r.ticker }}</b>{% if r.new_entry %} 🆕{% endif %}{% if r.extreme_runup %} ⚠{% endif %}</td>
{% if r.error %}<td colspan="13" class="muted">{{ r.band or "取数失败" }}：{{ r.error }}</td>{% else %}
<td{% if r.tier == "深度高位" %} class="up"{% endif %}><b>{{ r.tier or "不在高位" }}</b></td>
<td>{{ r.score if r.score is not none else "-" }}</td><td>{{ r.prev_score if r.prev_score is not none else "-" }}</td><td>{{ r.price }}</td>
<td class="{{ 'up' if r.chg_pct > 0 else 'down' if r.chg_pct < 0 else '' }}">{{ "%+.1f%%"|format(r.chg_pct) }}</td>
<td>{{ r.rsi14 if r.rsi14 is not none else "-" }}</td><td>{{ "%s%%"|format(r.atr_pct) if r.atr_pct is not none else "-" }}</td>
<td>{{ "%.1f ATR"|format(r.dev_atr) if r.dev_atr is not none else "-" }}</td>
<td>{{ "%.0f%%"|format(r.runup20_pct) if r.runup20_pct is not none else "-" }}</td><td>{{ "%.0f%%"|format(r.dist52h_pct) if r.dist52h_pct is not none else "-" }}</td>
<td>{{ r.dip|int }}</td>
<td{% if r.days_to_earnings is not none and r.days_to_earnings <= 14 %} class="down"{% endif %}>{% if r.earnings %}{{ r.earnings }}{% if r.days_to_earnings is not none %}（{{ r.days_to_earnings }}天）{% endif %}{% else %}-{% endif %}</td>
<td class="muted">{{ r.reasons|join("；") }}{% for n in r.notes %}<br>{{ n }}{% endfor %}</td>{% endif %}</tr>
{% endfor %}
</table>
<div class="muted">档位：深度高位 ≥70，高位 55–69，接近高位 45–54。🆕 = 写进 holdings.txt 以来第一次到 ≥55；⚠ = 实验性附注（20 日翻倍且高波动）。{{ high_context.footnote }}</div>
</div>
{% else %}
<div class="card muted">holdings.txt 里没有持仓。</div>
{% endif %}

<h2>📉 低位清单</h2>
<div class="card">
  {{ low_context.vix_line }}
  <div class="muted" style="margin-top:4px">{{ low_context.definition }}</div>
</div>
{% if low_list %}
<div class="card scroll">
<table>
<tr><th>票</th><th>档位</th><th>抄底分</th><th>现价</th><th>今日</th><th>RSI</th><th>ATR%</th><th>低于20日线</th><th>20日回撤</th><th>距52周高</th><th>财报</th><th>为什么低</th></tr>
{% for r in low_list %}
<tr><td><b>{{ r.ticker }}</b>{% if r.held %} ★{% endif %}</td>
<td{% if r.tier == "深度低位" %} class="down"{% endif %}><b>{{ r.tier }}</b></td>
<td>{{ r.dip|int }}</td><td>{{ r.price }}</td>
<td class="{{ 'up' if r.chg_pct > 0 else 'down' if r.chg_pct < 0 else '' }}">{{ "%+.1f%%"|format(r.chg_pct) }}</td>
<td>{{ r.rsi }}</td><td>{{ r.atr_pct }}%</td><td>{{ "%.1f"|format(r.dev_atr) }} ATR</td>
<td>{{ "%.0f%%"|format(r.dd20_pct) }}</td><td>{{ "%.0f%%"|format(r.off_52w_pct) }}</td>
<td{% if r.days_to_earnings is not none and r.days_to_earnings <= 14 %} class="down"{% endif %}>{% if r.earnings %}{{ r.earnings }}{% if r.days_to_earnings is not none %}（{{ r.days_to_earnings }}天）{% endif %}{% else %}-{% endif %}</td>
<td class="muted">{{ r.reasons|join("；") }}</td></tr>
{% endfor %}
</table>
<div class="muted">★ = 持仓。档位：深度低位 抄底分≥70，低位 55–69，接近低位 45–54（收盘若恐慌放量可能升档）。只说明"现在跌得多深"，排序不代表更值得买；不提供买点、止损或卖点。</div>
</div>
{% else %}
<div class="card muted">今天自选股里没有处在低位的票（抄底分都低于 45）。</div>
{% endif %}

{% if show_trading %}
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
<tr><th>票</th><th>股数</th><th>现价</th><th>成本</th><th>止损价</th><th>限价</th><th>距现价</th><th>财报</th><th>备注</th></tr>
{% for r in stop_sheet %}
<tr><td><b>{{ r.ticker }}</b></td><td>{{ r.shares if r.shares else "?" }}</td><td>{{ r.price }}</td><td>{{ r.cost }}</td>
<td><b>{% if r.stop is not none %}{{ r.stop }}{% else %}<span class="down">无</span>{% endif %}</b></td>
<td>{% if r.limit is defined and r.limit %}{{ r.limit }}{% else %}-{% endif %}</td>
<td{% if r.dist_pct is not none and r.dist_pct < 4 %} class="down"{% endif %}>{% if r.dist_pct is not none %}{{ r.dist_pct }}%{% if r.dist_atr %} ({{ r.dist_atr }}ATR){% endif %}{% else %}-{% endif %}</td>
<td{% if r.days_to_earnings is not none and r.days_to_earnings <= 21 %} class="down"{% endif %}>{% if r.earnings %}{{ r.earnings }}{% if r.days_to_earnings is not none %} ({{ r.days_to_earnings }}天){% endif %}{% else %}<span class="muted">未知</span>{% endif %}</td>
<td class="muted">{% if r.warn %}<span class="down">{{ r.warn }}</span>{% else %}{% if r.locked_gain %}锁盈(止损>成本){% endif %}{% if r.dist_pct is not none and r.dist_pct < 4 %} 一个ATR内，今日可能触发{% endif %}{% endif %}</td></tr>
{% endfor %}
</table>
<div class="muted">止损价=持仓期内结构止损的历史最高值（只升不降）。挂 <b>Stop Limit</b>：触发价填"止损价"，限价填"限价"列（= 止损价 − max(3%×止损价, 0.5×ATR)，已算好）。<b>触发后若开盘跳空跌破限价没成交，不要撤单；收盘前仍未成交就按市价卖出（看不了盘则最迟次日开盘卖）</b>，不要挂着等反弹——那等于取消了止损。也可直接用 Stop Loss（止损市价单），5年回测期望与此规则无差别，但跳空日会以开盘价成交。</div>
</div>
{% endif %}

<div class="card">
  <b>风险仪表盘</b>　总开放风险 <b>{{ risk.open_risk_pct }}%</b>/{{ risk.open_risk_cap|int }}%
  {% if risk.cash_pct is not none %}　现金 <b>{{ risk.cash_pct }}%</b>{% endif %}
  {% if risk.deployed_pct %}　已投入 <b>{{ risk.deployed_pct }}%</b>{% if risk.leverage > 1 %}（⚠ 杠杆 {{ risk.leverage }}:1）{% endif %}{% endif %}
  　单笔风险 {{ risk.risk_per_trade }}%{% if risk.throttled %}（⛔熔断减半中）{% endif %}
  <div class="muted" style="margin-top:4px">簇占比：{% for c, v in risk.cluster_sums.items() %}{{ c }} {{ v }}%{% if not loop.last %} · {% endif %}{% endfor %}（簇上限 {{ risk.cluster_cap|int }}%）
  {% if risk.sub_cluster_sums %}<br>子簇：{% for c, v in risk.sub_cluster_sums.items() %}{{ c }} {{ v }}%{% if not loop.last %} · {% endif %}{% endfor %}（子簇上限 {{ risk.sub_cluster_cap|int }}%）{% endif %}
  {% if not risk.has_account_file %}<br>提示：本地未找到 account.yaml，挂单只显示仓位%不显示股数（云端属正常）{% endif %}</div>
  {% for w in risk.warnings %}<div class="event">{{ w }}</div>{% endfor %}
</div>
{% endif %}
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

<h2>低位个股详情</h2>
{% for s in card_stocks %}
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
  {% if show_trading and s.advice %}
  <div class="advice{{ ' urgent' if s.advice.action in urgent_actions else '' }}">
    🎯 <b>{{ s.advice.action }}</b>{% if s.advice.pnl_pct is defined and s.advice.pnl_pct is not none %} <b>[持仓 · {{ s.advice.mode }} · 浮盈 {{ "%+.1f%%"|format(s.advice.pnl_pct) }}]</b>{% else %}（{{ s.advice.mode }}）{% endif %}
    — {{ s.advice.reasons|join("；") }}
    {% if s.advice.orders %}<div class="muted" style="margin-top:4px">{% for o in s.advice.orders %}📌 {{ o.side }}·{{ o.type }} @ <b>{{ o.price }}</b>（{{ o.portion }}）{{ o.note }}{% if not loop.last %}<br>{% endif %}{% endfor %}</div>{% endif %}
  </div>
  {% endif %}
  {% if s.earnings_date %}<div class="event">📅 {{ s.earnings_date }} 财报——财报前后常有大幅跳空</div>{% endif %}
  {% if show_trading and s.plan and s.plan.status != "无计划" %}
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
  {% if show_trading %}
  {% for e in s.events %}<div class="event">⚠ {{ e.type }} {{ e.zone.low }}–{{ e.zone.high }}（{{ e.bars_ago }}个交易日前{% if e.vol_confirm %}，放量{% endif %}）</div>{% endfor %}
  {% if s.inside_zone %}<div class="event">现价处于 {{ s.inside_zone.low }}–{{ s.inside_zone.high }} 争夺区，多空正在这里交战</div>{% endif %}
  {% endif %}
  {% if s.low_reasons %}<div class="muted">为什么低：{{ s.low_reasons|join("；") }}</div>{% endif %}
  {% if show_trading %}
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
  {% endif %}
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


HOLDINGS_FILE = ROOT / "holdings.txt"


def parse_holdings(text: str) -> list[str]:
    """holdings.txt：只写代码，空格/逗号/换行分隔都行，# 后面是注释。大小写不限，重复的去掉。"""
    out: list[str] = []
    for line in text.splitlines():
        for tok in line.split("#", 1)[0].replace(",", " ").replace("，", " ").split():
            t = tok.strip().upper()
            if t and t not in out:
                out.append(t)
    return out


def load_holdings() -> list[str]:
    """用户当前持仓（只要代码，不要成本）。

    2026-10-17 起持仓以 holdings.txt 为准——用户每天自己更新它，系统不管本钱多少。
    文件不存在时回退到旧口径：watchlist.yaml 里带 cost/pct 字段的票。
    """
    if HOLDINGS_FILE.exists():
        return parse_holdings(HOLDINGS_FILE.read_text(encoding="utf-8"))
    return [i["symbol"] for i in load_watchlist()
            if i.get("cost") is not None or i.get("pct") is not None]


def load_universe() -> list[dict]:
    """每天要分析的票 = watchlist ∪ 持仓。持仓里不在 watchlist 的票也要分析（要给它算高位分）。"""
    wl = load_watchlist()
    have = {i["symbol"] for i in wl}
    return wl + [{"symbol": t} for t in load_holdings() if t not in have]


def load_order_style() -> str:
    """挂单风格：single(单笔全仓,当前Fidelity适配) / oco / tranche(1/3梯次)。"""
    try:
        wl = yaml.safe_load((ROOT / "watchlist.yaml").read_text(encoding="utf-8"))
        return str(wl.get("order_style", "tranche"))
    except Exception:
        return "tranche"


def _tier(dip: float) -> str | None:
    if dip >= 70:
        return "深度低位"
    if dip >= 55:
        return "低位"
    if dip >= 45:
        return "接近低位"
    return None


def build_low_list(ok_stocks: list[dict], wl_by_symbol: dict, held: set[str] | None = None) -> list[dict]:
    """低位清单：抄底分 ≥45 的自选股，按抄底分从高到低。

    2026-10-17 起系统只做这一件事（用户："你就告诉我哪支票在低位就行，价格、止损、卖出我自己看"）。
    为什么用抄底分定义"低位"：2026-10-17 的无偏全市场研究里，唯一在前后两半和 2022 都站得住的条件是
    dip≥55 对 dip<55 —— 同样挂单深度下止跌率 37.2% 对 34.0%（+3.1pp，21日块 CI +1.9~+4.5）。
    同一研究证明支撑区本身没有止跌能力（与随机价位无差），所以这里不给任何价位。
    """
    from .advice import _days_to
    if held is None:
        held = {t for t, i in wl_by_symbol.items() if i.get("cost") is not None}
    out = []
    for s in ok_stocks:
        dip = float(s["dip"]["score"])
        tier = _tier(dip)
        if tier is None:
            continue
        item = wl_by_symbol.get(s["ticker"], {})
        df = data.get_daily(s["ticker"], "2y")
        hi20 = float(df["High"].tail(20).max())
        ma20 = (s.get("ma", {}).get(20) or {}).get("value")
        out.append({
            "ticker": s["ticker"], "held": s["ticker"] in held, "tier": tier, "dip": dip,
            "price": s["price"], "chg_pct": s["chg_pct"], "rsi": s["rsi"],
            "atr_pct": round(s["atr"] / s["price"] * 100, 1) if s.get("atr") and s.get("price") else None,
            "dev_atr": round((s["price"] - ma20) / s["atr"], 2) if ma20 and s.get("atr") else 0.0,
            "dd20_pct": round((s["price"] / hi20 - 1) * 100, 1) if hi20 else 0.0,
            "off_52w_pct": round((s["price"] / s["high_52w"] - 1) * 100, 1) if s.get("high_52w") else 0.0,
            "earnings": s.get("earnings_date"), "days_to_earnings": _days_to(s.get("earnings_date")),
            # 抄底分里"贴近/距强支撑区"那一项照常计分（研究验证的就是这个分数），但不显示区间价位：
            # 支撑区与随机价位止跌力无差，显示 "贴近强支撑区 137.39-152.10" 会被读成买点。
            "reasons": [x for x in (s["dip"].get("reasons") or []) if "支撑区" not in x],
            "note": item.get("note", ""),
        })
    out.sort(key=lambda r: -r["dip"])
    return out


def _high_tier(score) -> str | None:
    if score is None:
        return None
    if score >= 70:
        return "深度高位"
    if score >= 55:
        return "高位"
    if score >= 45:
        return "接近高位"
    return None


# 推送/报告用语。数字全部来自 2026-10-18 高位分研究（无偏全市场面板，样本外 H2 = 2024-04-16→2026-10-15，
# 4 路对抗复核后仍成立或已修正的版本）。不得改写成"容易回落/该卖了"——两半样本方向相反，研究明确禁止。
HIGH_TEXT = {
    "definition": ("高位分 = RSI 超买、高于 20 日线的幅度、20 日涨幅、放量上涨、贴近 52 周高，只描述\"最近涨得多猛\"。"
                   "全市场 5 年无偏检验（含退市股）：高位分 ≥55 之后 10/20 天相对同日随机股 −0.4pp [−1.0,+0.1] / "
                   "−0.5pp [−1.2,+0.1]（按事件平均 +0.1 / +0.2），≥70 也不比 55–69 更差——既不代表会回落，也不代表会继续涨。"
                   "不是卖出信号。"),
    "tier": {
        "接近高位": "接近高位：最近涨得比较猛；历史上之后 10 天相对随机股 −0.3pp [−0.8,+0.1]，和随机没区别。",
        "高位": ("高位：高位分只衡量\"最近涨得多猛\"，不是卖出信号。历史上之后 10/20 天相对随机股 −0.5pp [−1.0,0.0] / "
                "−0.6pp [−1.3,0.0]，按事件平均约 0，校正后不显著——所以既不代表会回落，也不代表会继续涨。"),
        "深度高位": ("深度高位：历史上并不比 55–69 更容易回落；10 天相对随机股 −0.3pp [−0.9,+0.3]；"
                    "10 天内跌到今天收盘下方 1 个 ATR 的概率 61%，所有股票 60%。"),
    },
    "pullback": ("10 天内回撤 1 个 ATR 很常见：高位票约 63% [60,66]（前半段 58%，两段方向相反），所有股票约 60%；"
                 "先碰 −1ATR 和先碰 +1ATR 约一半一半（46% 对 48%），不是信号。"),
    "high_vol": ("高波动票到高位后振幅上下都更宽：10 天平均最深 −11.3%、最高 +13.3%（同波动的全部票 −9.2% / +11.3%），"
                 "方向上没有可靠差别。"),
    "spy_below": ("大盘在 200 日线下方：这时还在高位的多是低 β 抗跌股，历史上大盘随后反弹时它们常涨得少"
                  "（样本外只有 2 段，10 天 −3.0pp，区间 [−5.8,+0.6] 含 0）。这是\"抗跌股跟不上反弹\"，不是\"会下跌\"。"),
    "spy_above": "大盘在 200 日线上方（平常市况）：历史上高位票之后 10/20 天相对随机股 −0.2pp / −0.1pp，没有差别。",
    "new_entry": ("这是写进持仓（{since}）以来第一次到高位。如果这是你低位买进后第一次涨到高位："
                  "历史上抄底买进的票第一次到 ≥55 时（样本外 5,617 次），继续拿 10/20 天相对大盘 "
                  "−0.0pp / −0.3pp（区间含 0）；之后 20 天约 58% 还会再涨 ≥5%、约 57% 会回落 ≥5%（常常两种都发生）；"
                  "\"一到高位就卖\"也不比在同样持有时长的随机时点卖更好。"),
    "extreme": ("【实验性附注，事后发现，未经前瞻验证】近 20 天从低点涨了 {run:.0f}%（≥100%）且波动很大（ATR {atr:.0f}%）。"
                "历史上这类票之后 10 天相对同波动票 −4.4pp [−5.7,−3.1]（前半段 −5.0），10 天原始收益中位数 −19%，"
                "收涨的只有约 27%；换统计方法幅度在 −1.5 到 −4.4pp 之间。仅供参考，不是卖出建议。"),
    "footnote": "数字是全市场历史平均，不含分红和交易成本，不代表你这只票；已有收购要约的票不适用。",
}


def holding_since(holdings: list[str]) -> dict[str, str | None]:
    """每只持仓"从哪天起在 holdings.txt 里"（YYYY-MM-DD）。用户不记成本也不记日期，所以从 git 历史里读：
    按时间顺序回放 holdings.txt 的每个版本，取这只票最近一次被加进去的提交日期（卖掉再买回会重新计）。
    - 文件最早版本里就有的票：如果 watchlist 里有更早的 since（旧口径的买入日），用它。
    - 工作区里新加、还没提交的票：记为今天。
    - git 不可用，或浅克隆导致看不到这只票是哪天加的：None（这时不判断"第一次到高位"）。
    "卖 vs 继续拿"那句只适用于持有后第一次 ≥55（研究口径），需要这个日期才能判断。
    """
    import subprocess
    from datetime import date as _date
    out: dict[str, str | None] = {t: None for t in holdings}
    try:
        def _git(*a):
            return subprocess.run(["git", "-C", str(ROOT), *a], capture_output=True, text=True,
                                  timeout=30, check=True).stdout
        commits = [ln.split() for ln in _git("log", "--reverse", "--format=%H %cs", "--", "holdings.txt").splitlines()
                   if ln.strip()]
        shallow = _git("rev-parse", "--is-shallow-repository").strip() == "true"
        added: dict[str, tuple[str, bool]] = {}
        prev: set[str] = set()
        for i, (h, d) in enumerate(commits):
            try:
                cur = set(parse_holdings(_git("show", f"{h}:holdings.txt")))
            except Exception:
                cur = set()
            for t in cur - prev:
                added[t] = (d, i == 0)
            for t in prev - cur:
                added.pop(t, None)
            prev = cur
    except Exception:
        return out
    wl = {i["symbol"]: i for i in load_watchlist()}
    today = _date.today().isoformat()
    for t in holdings:
        if t not in prev:                      # 已写进文件但还没提交
            out[t] = today
            continue
        d, in_first = added.get(t, (None, True))
        ws = wl.get(t, {}).get("since")
        if in_first:
            if ws:
                d = min(str(ws), d) if d else str(ws)
            elif shallow:
                d = None                       # 浅克隆：最早可见版本之前的历史看不到
        out[t] = d
    return out


def first_high_since(df, since: str | None, score) -> bool:
    """今天 ≥55，且从 since 当天起（不含今天）没有任何一天 ≥55。since 未知时返回 False。最多回看 252 根。"""
    import pandas as pd
    from .stock import high_score
    if since is None or score is None or score < 55:
        return False
    idx = df.index[(df.index >= pd.Timestamp(since)) & (df.index < df.index[-1])][-252:]
    return all((high_score(df.loc[:d]).get("score") or 0) < 55 for d in idx)


def build_high_list(ok_stocks: list[dict], holdings: list[str], errors: dict | None = None,
                    spy_below: bool = False, since: dict | None = None) -> list[dict]:
    """持仓高位分：holdings.txt 里每只票一行（不论分数高低），按高位分从高到低。

    2026-10-18 用户："每天更新持仓（不管本钱），给我票是否在高位的信号，也有分数，我自己考虑该不该卖"。
    分数和文案的依据见 stock.high_score 与 HIGH_TEXT。不给卖点、止盈价。
    """
    from .advice import _days_to
    from .stock import high_score
    by = {s["ticker"]: s for s in ok_stocks}
    out = []
    for t in holdings:
        s = by.get(t)
        if s is None or not s.get("high"):
            out.append({"ticker": t, "error": (errors or {}).get(t, "没有分析结果"), "score": None, "tier": None,
                        "notes": []})
            continue
        h = s["high"]
        f = h.get("facts") or {}
        score = h.get("score")
        t_since = (since or {}).get(t)
        if score is None:          # 历史太短（新股/刚复牌），无法评分：走"无法评分"行，模板不格式化空字段
            out.append({"ticker": t, "error": "；".join(h.get("reasons") or ["数据不足"]), "score": None,
                        "tier": None, "band": h.get("band"), "since": t_since, "notes": []})
            continue
        df = data.get_daily(t, "2y")
        prev_score = high_score(df.iloc[:-1]).get("score") if len(df) > 1 else None
        new_entry = first_high_since(df, t_since, score)
        tier = _high_tier(score)
        notes = []
        if tier:
            notes.append(HIGH_TEXT["tier"][tier])
        if score is not None and score >= 55:
            notes.append(HIGH_TEXT["pullback"])
            if (f.get("atr_pct") or 0) >= 0.04:
                notes.append(HIGH_TEXT["high_vol"])
            if spy_below:
                notes.append(HIGH_TEXT["spy_below"])
        if new_entry:
            notes.append(HIGH_TEXT["new_entry"].format(since=t_since))
        if f.get("extreme_runup"):
            notes.append(HIGH_TEXT["extreme"].format(run=f["runup20"] * 100, atr=f["atr_pct"] * 100))
        out.append({
            "ticker": t, "score": score, "prev_score": prev_score, "tier": tier, "band": h.get("band"),
            "since": t_since, "new_entry": new_entry, "extreme_runup": bool(f.get("extreme_runup")),
            "price": s["price"], "chg_pct": s["chg_pct"], "rsi14": f.get("rsi14"),
            "atr_pct": round(f["atr_pct"] * 100, 1) if f.get("atr_pct") is not None else None,
            "dev_atr": f.get("dev_atr"), "runup20_pct": round(f["runup20"] * 100, 1) if f.get("runup20") is not None else None,
            "dist52h_pct": round(f["dist52h"] * 100, 1) if f.get("dist52h") is not None else None,
            "dip": float(s["dip"]["score"]),
            "earnings": s.get("earnings_date"), "days_to_earnings": _days_to(s.get("earnings_date")),
            "reasons": h.get("reasons") or [], "notes": notes,
        })
    out.sort(key=lambda r: -(r["score"] if r.get("score") is not None else -1))
    return out


def high_context(spy_below: bool) -> dict:
    return {"definition": HIGH_TEXT["definition"],
            "market_line": HIGH_TEXT["spy_below" if spy_below else "spy_above"],
            "footnote": HIGH_TEXT["footnote"]}


def low_context(mkt: dict) -> dict:
    """清单开头的大盘环境句：择时（同状态下低位票与随机股各自的原始收益）和选股（低位票相对随机股）分开说。

    数字来自 2026-10-17 无偏全市场研究（dip≥55，按日等权的 10 日收益，H1=2024-04-16 前 / H2=之后）。
    主状态用"SPY 是否站上 200 日线"（H1 事先选定），VIX 作附注——不因 VIX 在 H2 的 R² 更高就事后换主状态。
    注：这两张查表是研究最后一步补算的，没有经过独立复核（与已复核结果重合的数字一致）。
    """
    from . import data as _data
    # (低位票原始 H1, H2, 随机股原始 H1, H2, 超额 H1, H2)
    SPY_TBL = {"上": (-0.59, 0.05, -0.35, 0.23, -0.26, -0.18), "下": (1.20, 2.90, 0.45, 2.62, 0.77, 0.35)}
    VIX_TBL = {"平静": (-0.16, -0.55, 0.13, -0.13, -0.30, -0.41), "中性": (-0.23, 1.14, -0.60, 0.96, 0.35, 0.20),
               "恐慌": (1.19, 4.88, 0.63, 3.87, 0.58, 1.09)}
    v = (mkt.get("vix") or {}).get("value")
    spy_side = spy_dist = None
    try:
        c = _data.get_daily("SPY", "2y")["Close"]
        sma = float(c.tail(200).mean()); spy_dist = float(c.iloc[-1]) / sma - 1
        spy_side = "上" if spy_dist >= 0 else "下"
    except Exception:
        pass
    vix_state = None if v is None else ("平静" if v < 18 else ("中性" if v < 25 else "恐慌"))
    parts = []
    if spy_side:
        a = SPY_TBL[spy_side]
        parts.append(f"SPY {'站上' if spy_side == '上' else '跌破'} 200 日线（{spy_dist * 100:+.1f}%）")
    if vix_state:
        parts.append(f"VIX {v}（{vix_state}）")
    line = "大盘环境：" + "，".join(parts) + "。" if parts else "大盘环境：取数失败。"
    if spy_side:
        a = SPY_TBL[spy_side]
        line += (f"历史同状态下，低位票之后 10 天平均 {a[0]:+.1f}% / {a[1]:+.1f}%（前半/后半），"
                 f"同日随机股 {a[2]:+.1f}% / {a[3]:+.1f}%；低位票相对随机股 {a[4]:+.2f} / {a[5]:+.2f} 个百分点。")
    if vix_state:
        b = VIX_TBL[vix_state]
        line += f"按 VIX 档位：低位票 {b[0]:+.1f}% / {b[1]:+.1f}%，随机股 {b[2]:+.1f}% / {b[3]:+.1f}%。"
    if spy_side == "下" or vix_state == "恐慌":
        line += ("注意：这时的正收益主要是大盘反弹（随机股也涨），而且样本只有少数几段行情；"
                 "2022 年那种长熊里只有约 +0.1%~+0.6%。")
    elif spy_side == "上":
        line += "即：现在这种环境，历史上没有顺风，低位票也没有选股优势。"
    return {
        "vix": v, "spy_dist200": spy_dist, "vix_line": line,
        "definition": ("低位 = 抄底分高（RSI 超卖、低于 20 日线的幅度、20 日回撤、恐慌放量），只描述\"跌得多深\"。"
                       "全市场 5 年无偏回测：按日计算，低位票之后 10 天并不比同日随机股好；"
                       "历史上的正收益主要出现在大盘崩盘之后（那时所有股票一起反弹）。"
                       "清单按分数排序只表示跌得深浅，不代表排前面的更值得买。"
                       "低位票买入后 10 天内的平均最大浮亏约 −6%（高波动票约 −7.4%）。"),
    }


def generate(session: str = "premarket") -> tuple[Path, Path]:
    """生成报告，返回 (html路径, json路径)。json 给 Claude/程序读，html 给人看。"""
    now = datetime.now(ZoneInfo("America/Chicago"))
    mkt = market.overview(session)

    stocks = []
    holdings = load_holdings()
    for item in load_universe():
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
    wl_by_symbol = {i["symbol"]: i for i in load_universe()}
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
                # Stop Limit 的限价：止损价 − max(3%×止损价, 0.5×ATR)。
                # 5年回测240次止损(真实撮合)：-1%限价有8次整天没成交，此规则2次，-5%为0次；
                # 期望值与止损市价单分不出差别(ΔR +0.006)。本周 SNDK 在-1%下至今未成交。
                "limit": round(stop - max(0.03 * stop, 0.5 * atr_v), 2) if atr_v else round(stop * 0.97, 2),
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
        (f"{r['ticker']}{r['stop']:g}/{r['limit']:g}" if r.get("limit") else f"{r['ticker']}{r['stop']:g}")
        if r["stop"] is not None else f"{r['ticker']}⚠无止损"
        for r in stop_sheet
    )


    low_list = build_low_list(ok_stocks, wl_by_symbol, set(holdings))
    low_ctx = low_context(mkt)
    spy_below = low_ctx.get("spy_dist200") is not None and low_ctx["spy_dist200"] < 0
    high_list = build_high_list(ok_stocks, holdings, {s["ticker"]: s["error"] for s in stocks if "error" in s},
                                spy_below, holding_since(holdings))
    high_ctx = high_context(spy_below)
    low_set = {r["ticker"] for r in low_list}
    card_stocks = sorted([s for s in ok_stocks if s["ticker"] in low_set], key=lambda s: -s["dip"]["score"])
    for s in card_stocks:
        s["low_reasons"] = [x for x in (s["dip"].get("reasons") or []) if "支撑区" not in x]

    title = f"{'盘前' if session == 'premarket' else '盘后'}持仓高位 + 低位清单 · {now.strftime('%Y-%m-%d')}"
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
        card_stocks=card_stocks,
        low_list=low_list,
        low_context=low_ctx,
        high_list=high_list,
        high_context=high_ctx,
        # 2026-10-17 用户决定自己看价格买、自己止损、自己卖：操作单/止损单/仓位仪表盘/支撑区计划不再展示。
        # 代码和 JSON 字段保留，改回 True 即可恢复。
        show_trading=False,
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
            {"holdings": holdings, "high_list": high_list, "high_context": high_ctx,
             "low_list": low_list, "low_context": low_ctx,
             "market_mode": mmode_desc, "order_sheet": order_sheet, "stop_sheet": stop_sheet, "stop_line": stop_line, "risk_dashboard": risk_dashboard,
             "market": mkt, "stocks": stocks, "earnings": earnings},
            ensure_ascii=False, indent=1, default=str,
        ),
        encoding="utf-8",
    )
    return html_path, json_path
