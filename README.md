# market-lab

美股支撑/压力位分析 + 大盘状态 + 抄底评分 + 盘前盘后自动报告。

## 常用命令

```bash
# 分析单只股票（JSON 输出：位置图谱、站稳/破位事件、抄底分）
uv run market-lab analyze NVDA

# 生成盘前/盘后报告（HTML 给人看 + JSON 给程序读，存 reports/）
uv run market-lab report premarket
uv run market-lab report postmarket

# 只看大盘概览
uv run market-lab market

# 回测抄底分各分数段的历史胜率（明细存 reports/backtest-*.json）
uv run market-lab backtest --period 5y
```

## 自选股

编辑 `watchlist.yaml`，`note` 字段可以写成本、计划等备注，会显示在报告里。

## 方法说明

- **支撑/压力位**：摆动高低点、成交量密集区、20/50/200日均线、未回补缺口、52周高低点、整数关口各自产出候选位，价格相近的聚成区间，重叠越多强度分越高。输出永远是"区间+强度"，不是单一价格。
- **站稳/跌破**：收盘价穿越区间边界+量能确认的规则化判定，口径每天一致。
- **抄底分(0-100)**：RSI 超卖 + 偏离20日均线幅度(按ATR) + 短期回撤深度 + 是否贴近强支撑 + 恐慌放量，专为"大跌进场"策略设计。**进场线=55**（5年回测：45-54段胜率37.7%勿动手；55-69段52.5%；70+段配宽止损67.8%）。止损自适应：55-69段=区间下沿-0.75ATR，70+段=-1.0ATR（回测扫描+样本外验证）。低波动票(ATR<2.5%)自动警示(该类+10%目标胜率仅30%)。
- **盘中提醒**：`market-lab alerts` 检查现价是否跌入进场区，定时任务每小时跑（工作日9-14点CT）。
- **大盘抄底温度计**：VIX 分位 + 标普 RSI + 市场宽度(大盘股站上50日线比例) + 偏离度。

## 数据源（已接入，key 在 .env）

- **Polygon**（Developer 档）：股票/ETF 日线+快照+新闻（新闻带 AI 情绪标注→利好/利空自动分类）。15分钟延迟，请求不限次。无期权权限。指数(^VIX)和期货(ES=F)自动回落到 yfinance。
- **Ortex**（EOD 档）：SI占流通盘+3周趋势；SI≥8% 的高做空票追加借券费(ctb/new)和回补天数(dtc)。
  **按 credits 计费**（每次调用~1.2，剩余量在报告 JSON 的 credits_left 里）；限速1次/秒；
  每票每天只取一次（缓存）。credits 吃紧就调高 `providers/ortex.py` 的 `DEEP_THRESHOLD`。
  公开 API 无 utilization 端点，用借券费+DTC 替代。
- **Finnhub**（免费档，60次/分）：财报日历、新闻备源、实时报价备源。
- 无 key 时全部自动降级到 yfinance，分析照跑。

**`.env` 永远不要提交到 git。**

## 定时任务

由 Claude 的 Scheduled Tasks 驱动（不在本仓库里）：工作日 7:45 盘前、15:15 盘后（美中时间），
自动跑报告 + 搜新闻 + 发简报。要求 Claude 桌面 App 处于打开状态，错过的运行会在下次打开时补跑。
