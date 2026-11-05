"""把报告数据写成简报和推送原文。

数字和格式由这里定死，云端模型只负责把 <<...>> 占位符换成查到的一句事实、把推送原文发出去。
之前让模型自己排版：Haiku 把推送翻成英文，Sonnet 为查原因派了三个子代理搜了几十次；
观察池扩到两百多只之后更不能让它逐只去查。查什么由这里按预算决定。
"""
from __future__ import annotations

PUSH_MAX = 195          # PushNotification 说明：要在 200 字以内，超过手机上会被截断。emoji 按 UTF-16 算两个单位，留点余量
WHY_TICKERS = {"postmarket": 10, "premarket": 6}
WHY_SECTORS = 4
DETAIL_LOW_MAX = 25     # 低位票超过这么多只时，55-69 分的只按细分合并列出
SECTOR_MIN = 50         # 持仓再多也给板块行和低位清单留出这么多字
LOW_MIN = 60


def _s(x, nd=1):
    if x is None:
        return "-"
    return f"{0.0:.{nd}f}" if round(x, nd) == 0 else f"{x:+.{nd}f}"


def _pct(x, nd=1):
    return "-" if x is None else f"{_s(x, nd)}%"


def _tag(sector):
    return f"[{sector}]" if sector else ""


def day_label(session: str, stale: bool = False) -> str:
    """涨跌幅叫什么。盘前看到的是上一交易日的收盘（周一早上是周五）；盘后正常是今天，休市日最新K线还是上一个交易日。"""
    return "今日" if session == "postmarket" and not stale else "上一交易日"


def holdings_part(high_list: list[dict], budget: int = PUSH_MAX) -> str:
    """每只持仓一小段。放不下时先留取数失败和分数高的（最需要看一眼），其余写 +N只，全部清单在简报里。"""
    bits = []
    for r in high_list:
        if r.get("score") is None:
            bits.append(f"{r['ticker']} -")
            continue
        tier = (r.get("tier") or "").replace("接近高位", "接近")
        bits.append(f"{r['ticker']} {r['score']}{tier}{'🆕' if r.get('new_entry') else ''}"
                    f"{'⚠翻倍' if r.get('extreme_runup') else ''}")
    if not bits:
        return ""
    head = "📈持仓: "
    if len(head) + len(" ".join(bits)) <= budget:
        return head + " ".join(bits)
    order = sorted(zip(high_list, bits), key=lambda x: (x[0].get("score") is not None, -(x[0].get("score") or 0)))
    kept: list[str] = []
    used = len(head)
    for _, bit in order:
        if used + len(bit) + 1 > budget - 6:                                 # 给 " +NN只" 留位置
            break
        kept.append(bit)
        used += len(bit) + 1
    return head + " ".join(kept) + f" +{len(bits) - len(kept)}只"


def low_part(low_list: list[dict], label: str, budget: int) -> str:
    """按细分合并。深度低位排最前（最值得看一眼），其次是持仓，放不下的写 +N只，全部清单在简报里。"""
    rows = [r for r in low_list if r["dip"] >= 55]
    if not rows:
        return f"📉{label}: 没有低位票"
    rows = sorted(rows, key=lambda r: (0 if r["dip"] >= 70 else (1 if r.get("held") else 2), not r.get("held"), -r["dip"]))
    deep = sum(r["dip"] >= 70 for r in rows)
    head = f"📉{label}{len(rows)}只(深度{deep}): "
    picked: dict[str, list[str]] = {}
    used = len(head)
    shown = 0
    for r in rows:
        bit = f"{r['ticker']}{'★' if r.get('held') else ''}{int(r['dip'])}"
        sec = r.get("sector") or "未分类"
        cost = len(bit) + 1 if sec in picked else len(sec) + len(bit) + 4    # " | 细分 票" 或 " 票"
        if used + cost > budget - 6:                                         # 给 " +NN只" 留位置
            break
        picked.setdefault(sec, []).append(bit)
        used += cost
        shown += 1
    body = " | ".join(f"{sec} {' '.join(bits)}" for sec, bits in picked.items())
    rest = len(rows) - shown
    return head + body + (f" +{rest}只" if rest else "")


def push_sectors(summary: dict, budget: int = PUSH_MAX, label: str = "收盘") -> str:
    """各大板块今天的等权涨跌，从强到弱。放不下时留两头、省掉中间。"""
    groups = summary.get("groups") or []
    if not groups:
        return ""
    spy = summary.get("spy_chg")
    head = f"📊板块{label}(SPY{_pct(spy)}): " if spy is not None else f"📊板块{label}: "
    bits = [f"{g['group']}{_s(g['avg'])}" for g in groups]
    text = head + " ".join(bits)
    keep = len(bits)
    while len(text) > budget and keep > 2:
        keep -= 1
        top = (keep + 1) // 2
        text = head + " ".join(bits[:top]) + " … " + " ".join(bits[-(keep - top):])
    return text


def pushes(session: str, high_list: list[dict], low_list: list[dict], summary: dict | None,
           vix_note: str = "", n_ok: int | None = None, n_failed: int = 0, stale_as_of: str | None = None) -> list[str]:
    """要发的推送原文。收盘两条：持仓+板块、低位清单。盘前一条：持仓+低位（名单和昨晚基本一样）。

    有票取数失败时开头写明，手机上截断也看得到：清单少了票不等于这些票不在低位。一只都没取到时不能说"没有低位票"。
    stale_as_of：盘后跑但今天没有新K线（休市日），标上数据是哪天的。
    """
    post = session == "postmarket"
    if n_failed and not n_ok:
        return [f"⚠{'盘后' if post else '盘前'}报告失败：观察池 {n_failed} 只全部取数失败，今天没有清单"]
    warn = f"⚠{n_failed}只取数失败，结果不全\n" if n_failed else ""
    tail = f"\n{vix_note}" if vix_note else ""
    if post:
        label = f"{stale_as_of[5:]}收盘" if stale_as_of else "收盘"
        hold = holdings_part(high_list, PUSH_MAX - len(warn) - SECTOR_MIN)
        sectors = push_sectors(summary or {}, PUSH_MAX - len(warn) - len(hold) - 1, label)
        first = "\n".join(x for x in (hold, sectors) if x)
        second = low_part(low_list, f"{label}低位", PUSH_MAX - len(warn) - len(tail)) + tail
        return [warn + x for x in (first, second) if x]
    hold = holdings_part(high_list, PUSH_MAX - len(warn) - len(tail) - LOW_MIN)
    low = low_part(low_list, "低位", PUSH_MAX - len(warn) - len(tail) - (len(hold) + 1 if hold else 0))
    return [warn + "\n".join(x for x in (hold, low) if x) + tail]


def sector_lines(summary: dict, why_sectors: set[str]) -> list[str]:
    lines = []
    p = summary.get("pool")
    if p:
        lines.append(f"观察池 {p['n']} 只等权 {_pct(p['avg'], 2)}（涨 {p['up']} 跌 {p['down']}），"
                     f"SPY {_pct(summary.get('spy_chg'), 2)}。等权平均不等于行业指数。")
    for i, g in enumerate(summary.get("groups") or [], 1):
        etf = f"；{g['etf']} {_pct(g['etf_chg'])}" if g.get("etf") and g.get("etf_chg") is not None else ""
        vs = f"，相对SPY {_s(g['vs_spy'])}" if g.get("vs_spy") is not None else ""
        lines.append(f"{i}. {g['group']} {_pct(g['avg'])}（涨{g['up']} 跌{g['down']}{vs}{etf}）"
                     f"| 5日{_pct(g['ret5'])} 20日{_pct(g['ret20'])}"
                     f"| 低位{g['n_low']}（深度{g['n_deep']}）高位{g['n_high']}")
        if len(g["subs"]) > 1:
            lines.append("   细分：" + " ".join(f"{s['sub']}{_s(s['avg'])}" for s in g["subs"]))
        lines.append(f"   领涨 {g['best']['ticker']} {_pct(g['best']['chg'])}，领跌 {g['worst']['ticker']} {_pct(g['worst']['chg'])}")
        if g["group"] in why_sectors:
            lines.append(f"   原因：<<板块:{g['group']}>>")
    return lines


def pick_why(session: str, high_list: list[dict], low_list: list[dict], summary: dict | None) -> dict:
    """决定这次要查哪些原因：持仓优先，其次深度低位里今天跌得最多的；板块取相对大盘最强和最弱的。"""
    limit = WHY_TICKERS.get(session, 6)
    up, down = [], []
    for r in high_list:
        if r.get("tier"):
            up.append(r["ticker"])
    for r in low_list:
        if r.get("held") and r["dip"] >= 55:
            down.append(r["ticker"])
    deep = [r for r in low_list if r["dip"] >= 70 and not r.get("held")]
    deep.sort(key=lambda r: r.get("chg_pct") if r.get("chg_pct") is not None else 0)
    down += [r["ticker"] for r in deep]
    down = list(dict.fromkeys(t for t in down if t not in up))
    room = max(0, limit - len(up))
    sectors: list[str] = []
    if summary and summary.get("groups") and session == "postmarket":
        gs = [g for g in summary["groups"] if g.get("vs_spy") is not None and abs(g["vs_spy"]) >= 0.5]
        gs.sort(key=lambda g: -g["vs_spy"])
        half = WHY_SECTORS // 2
        sectors = [g["group"] for g in gs[:half] if g["vs_spy"] > 0]
        sectors += [g["group"] for g in gs[-half:] if g["vs_spy"] < 0 and g["group"] not in sectors]
    return {"up": up[:limit], "down": down[:room], "sectors": sectors}


def brief_md(session: str, high_list: list[dict], high_ctx: dict, low_list: list[dict], low_ctx: dict,
             summary: dict | None, why: dict, why_news: dict | None = None,
             as_of: str | None = None, stale: bool = False, failed: list[str] | None = None) -> str:
    post = session == "postmarket"
    day = day_label(session, stale)
    out: list[str] = []

    if as_of:
        out.append(f"数据日期：{as_of} 收盘" + ("（今天没有新K线）" if stale else ""))
    if failed:
        names = " ".join(failed[:30]) + (" …" if len(failed) > 30 else "")
        out.append(f"⚠ 观察池 {len(failed)} 只取数失败，下面的清单和板块数字不全：{names}")

    # ---- 持仓高位 ----
    out.append("【持仓高位】")
    quiet = []
    for r in high_list:
        if r.get("error") or r.get("score") is None:
            out.append(f"{r['ticker']} {r.get('band') or '取数失败'}：{r.get('error') or ''}")
            continue
        if not r.get("tier"):
            quiet.append(f"{r['ticker']}{_tag(r.get('sector'))} {r['score']}")
            continue
        flags = ("🆕" if r.get("new_entry") else "") + ("⚠" if r.get("extreme_runup") else "")
        if r.get("days_to_earnings") is not None and 0 <= r["days_to_earnings"] <= 7:
            flags += f"📅{r['earnings']}"
        why_txt = f" | <<为什么涨:{r['ticker']}>>" if r["ticker"] in why["up"] else ""
        out.append(f"{r['ticker']}{_tag(r.get('sector'))} {r['tier']} {r['score']}（上一日 {'-' if r.get('prev_score') is None else r['prev_score']}）{flags}"
                   f" | {day}{_pct(r.get('chg_pct'))} | 20日涨幅{_pct(r.get('runup20_pct'))}"
                   f" | 距52周高{_pct(r.get('dist52h_pct'))} | {'；'.join(r.get('reasons') or [])}{why_txt}")
        out.extend(f"   · {n}" for n in r.get("notes") or [])
    if quiet:
        out.append("不在高位：" + "、".join(quiet))
    if high_list:
        out.append(f"{high_ctx.get('market_line', '')} {high_ctx.get('footnote', '')}".strip())
    else:
        out.append("holdings.txt 里没有持仓。")

    # ---- 板块总结（只有收盘后）----
    if post and summary:
        out.append("")
        out.append("【板块总结】")
        out.extend(sector_lines(summary, set(why["sectors"])))

    # ---- 低位清单 ----
    out.append("")
    deep = [r for r in low_list if r["dip"] >= 70]
    low = [r for r in low_list if 55 <= r["dip"] < 70]
    near = [r for r in low_list if r["dip"] < 55]
    out.append(f"【{'收盘' if post else ''}低位清单】深度低位 {len(deep)} 只，低位 {len(low)} 只，接近低位 {len(near)} 只")
    detail = list(deep) + [r for r in low if r.get("held")]
    if len(deep) + len(low) <= DETAIL_LOW_MAX:
        detail = deep + low
    detail.sort(key=lambda r: -r["dip"])
    in_detail = {r["ticker"] for r in detail}
    for r in detail:
        flags = ""
        if r.get("days_to_earnings") is not None and 0 <= r["days_to_earnings"] <= 7:
            flags = f" 📅{r['earnings']}"
        why_txt = f" | <<为什么跌:{r['ticker']}>>" if r["ticker"] in why["down"] else ""
        out.append(f"{r['ticker']}{'★' if r.get('held') else ''}{_tag(r.get('sector'))} {r['tier']} {int(r['dip'])}{flags}"
                   f" | {day}{_pct(r.get('chg_pct'))} | 20日回撤{_pct(r.get('dd20_pct'))}"
                   f" | 距52周高{_pct(r.get('off_52w_pct'))}{why_txt}")
    rest = [r for r in low if r["ticker"] not in in_detail]
    if rest:
        by: dict[str, list[str]] = {}
        for r in rest:
            by.setdefault(r.get("sector") or "未分类", []).append(f"{r['ticker']} {int(r['dip'])}")
        out.append("其余低位（55-69）按细分：" + "；".join(f"{k}：{' '.join(v)}" for k, v in by.items()))
    if near:
        out.append("接近低位：" + " ".join(r["ticker"] for r in near))

    # ---- 大盘 ----
    out.append("")
    out.append("【今日大盘】" if post and not stale else "【大盘】")
    out.append(low_ctx.get("vix_line", ""))
    out.append("<<大盘要闻>>")

    # ---- 给查原因的模型用的参考，发出去之前删掉 ----
    if why_news:
        out.append("")
        out.append("===== 参考：近 7 天新闻标题，只用来判断原因，不要原样输出，发送前删掉这一段 =====")
        for t, titles in why_news.items():
            if titles:
                out.append(f"{t}: " + " / ".join(titles))
    return "\n".join(out) + "\n"
