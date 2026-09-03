"""AI模拟盘日度复盘——2026-09-02用户明确要求"他做汇报总结微信发我，但是他
也要学习，想让他变强大"。这个脚本负责两件事，都是用真实数据机械算出来的，
不经过AI（复用advisor.py那套"精确执行走command cron、组织成可读文字才交给
agentTurn"的架构，跟投研顾问那条WeChat简报同一个模式，避免agentTurn替AI
瞎编数字）：

1. 把过去24小时(覆盖一整个港股+美股交易日)的决策记录/下单记录/净值变化
   汇总打印到stdout——这份输出会被另一个agentTurn cron读取、转述成微信
   消息，不需要AI自己去查数据库。
2. 把这次复盘的真实结论(胜率/盈亏/当天最有效和最失败的一次判断)写进
   sim_agent_lessons表——sim_agent.py下次决策时会把最近几条读出来塞进
   prompt，给"边炒股边学习"这件事一个比单次决策的短期战绩(_HISTORY_
   CONTEXT_SIZE=5)更长的记忆窗口。

用户明确要求过"不是真的强化学习(改模型权重)"这一点在这里同样适用——这个
脚本做的是"把结果整理成结构化的历史事实喂给下一次决策"，不是训练模型。
"""
import json
from datetime import datetime, timedelta, timezone

import advisor
import sim_trader
import tracker

_CN_TZ = timezone(timedelta(hours=8))


def _to_cn(iso_str: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_CN_TZ)


def build_daily_report(email: str) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    runs = tracker.get_sim_agent_runs(email, limit=200)
    recent_runs = [r for r in runs if (_to_cn(r.get("run_at")) or datetime.min.replace(tzinfo=_CN_TZ)) >= cutoff.astimezone(_CN_TZ)]
    acted_runs = [r for r in recent_runs if r.get("status") == "完成"]

    orders = tracker.get_simulated_orders(email, limit=200)
    recent_orders = [o for o in orders if (_to_cn(o.get("created_at")) or datetime.min.replace(tzinfo=_CN_TZ)) >= cutoff.astimezone(_CN_TZ)]
    filled_orders = [o for o in recent_orders if o.get("status") == "成功"]

    snaps = tracker.get_equity_snapshots(email, limit=500)
    recent_snaps = [s for s in snaps if (_to_cn(s.get("snapshot_at")) or datetime.min.replace(tzinfo=_CN_TZ)) >= cutoff.astimezone(_CN_TZ)]
    recent_snaps.sort(key=lambda s: s["snapshot_at"])

    start_net = recent_snaps[0]["net_value_hkd"] if recent_snaps else None
    end_net = recent_snaps[-1]["net_value_hkd"] if recent_snaps else None

    # 胜率算法跟_performance_scoreboard同一套口径：拿"决策前净值"跟"当时之后
    # 到现在这段时间的实际净值变化"比——这里简化成跟当天最后一次快照比，
    # 不追每一轮各自精确的"后续窗口"，日度粗粒度复盘用这个足够。
    results = []
    for r in acted_runs:
        before = r.get("assets_hkd_before")
        try:
            sigs = json.loads(r.get("signals_json") or "[]")
        except Exception:
            sigs = []
        # 只认真正成交的信号。被预算/集中度/非开盘市场拦下来的那些一股都没
        # 买进去，把它们算成"这一轮有操作"会直接污染下面的胜率——分母里混进
        # 一堆根本没交易过的轮次，算出来的"胜率"就不是AI选股的胜率了。
        # 老记录没有_drop_reason/_executed字段，按老口径当已成交处理。
        acted = any(
            s.get("action") in ("买入", "卖出")
            and not s.get("_drop_reason") and s.get("_executed", True)
            for s in sigs
        )
        if acted and before and end_net:
            change_pct = (end_net - before) / before * 100
            results.append((r, change_pct))

    win_rate = (sum(1 for _, c in results if c > 0) / len(results) * 100) if results else None
    best = max(results, key=lambda x: x[1]) if results else None
    worst = min(results, key=lambda x: x[1]) if results else None

    total_fee_hkd = 0.0
    for r in acted_runs:
        note = r.get("note") or ""
        if "手续费共HK$" in note:
            try:
                total_fee_hkd += float(note.split("手续费共HK$")[1].split("，")[0].replace(",", ""))
            except (ValueError, IndexError):
                pass

    return {
        "start_net": start_net, "end_net": end_net,
        "run_count": len(recent_runs), "acted_count": len(acted_runs),
        "order_count": len(filled_orders),
        "win_rate": win_rate,
        "best": best, "worst": worst,
        "total_fee_hkd": total_fee_hkd,
        # 2026-09-02新增：完整的"有操作"决策清单(时间升序)，配合每支变化%——
        # 早上那份print_report只挑最好/最差各一条，晚上8:30用户明确要求的
        # "详细版"要看到当天每一次操作，不只是两个极端，所以把results
        # 原样带出去，不在这里重复算一遍。
        "all_acted": sorted(results, key=lambda x: x[0].get("run_at") or ""),
    }


def _fmt_pct(x):
    return f"{x:+.2f}%" if x is not None else "—"


def print_report(email: str):
    d = build_daily_report(email)
    print(f"==================== AI模拟盘日度复盘 ====================")
    if d["start_net"] is not None and d["end_net"] is not None:
        change = d["end_net"] - d["start_net"]
        change_pct = change / d["start_net"] * 100 if d["start_net"] else 0
        print(f"净值：HK${d['start_net']:,.0f} → HK${d['end_net']:,.0f}（{change:+,.0f}，{change_pct:+.2f}%）")
    else:
        print("净值：过去24小时没有快照数据（可能今天两个市场都没开盘）")
    print(f"决策次数：{d['run_count']}（其中有实际操作{d['acted_count']}次），成交订单{d['order_count']}笔，累计手续费HK${d['total_fee_hkd']:,.2f}")
    print(f"胜率（有操作的决策里，后续净值比决策前高的比例）：{d['win_rate']:.0f}%" if d["win_rate"] is not None else "胜率：暂无可统计的操作记录")
    if d["best"]:
        r, c = d["best"]
        print(f"\n本轮期间表现最好的一次决策（{_to_cn(r['run_at']).strftime('%H:%M') if _to_cn(r['run_at']) else ''}，事后变化{_fmt_pct(c)}）：\n{(r.get('reasoning_text') or '')[:200]}")
    if d["worst"] and d["worst"] != d["best"]:
        r, c = d["worst"]
        print(f"\n本轮期间表现最差的一次决策（{_to_cn(r['run_at']).strftime('%H:%M') if _to_cn(r['run_at']) else ''}，事后变化{_fmt_pct(c)}）：\n{(r.get('reasoning_text') or '')[:200]}")

    # 写进sim_agent_lessons——纯数据拼接，不经过AI，见文件头部说明。
    lesson_parts = [f"{datetime.now(_CN_TZ).strftime('%Y-%m-%d')}复盘：{d['acted_count']}次操作"]
    if d["win_rate"] is not None:
        lesson_parts.append(f"胜率{d['win_rate']:.0f}%")
    if d["start_net"] is not None and d["end_net"] is not None:
        lesson_parts.append(f"净值{_fmt_pct((d['end_net'] - d['start_net']) / d['start_net'] * 100 if d['start_net'] else 0)}")
    if d["worst"]:
        r, c = d["worst"]
        lesson_parts.append(f"最失败的判断（事后{_fmt_pct(c)}）：{(r.get('reasoning_text') or '')[:120]}")
    if d["best"]:
        r, c = d["best"]
        lesson_parts.append(f"最有效的判断（事后{_fmt_pct(c)}）：{(r.get('reasoning_text') or '')[:120]}")
    lesson_text = "；".join(lesson_parts)
    if d["acted_count"] > 0:
        tracker.log_sim_agent_lesson(email, lesson_text)
        print(f"\n（已写入长期经验记录：{lesson_text[:60]}...）")
    else:
        print("\n（今天没有实际操作，不写入长期经验记录）")


def print_detailed_report(email: str):
    """2026-09-02新增：用户明确要求"晚上八点半需要详细版汇报"，跟早上
    print_report(8:10播报，只挑最好/最差各一条)是两个不同定位——早上是
    "开盘前一句话回顾"，这份是"收盘后完整复盘"，要看到当天每一次操作
    （不是只看两个极端）、当前每一笔实际持仓、剩余现金，不只是净值涨跌
    这一个数字。同样不经过AI，纯数据拼接（跟print_report同一个原则，
    见文件头部说明），保证数字是真实查出来的，不是AI编的。
    """
    d = build_daily_report(email)
    print("==================== AI模拟盘详细复盘（晚间版） ====================")
    if d["start_net"] is not None and d["end_net"] is not None:
        change = d["end_net"] - d["start_net"]
        change_pct = change / d["start_net"] * 100 if d["start_net"] else 0
        print(f"净值：HK${d['start_net']:,.0f} → HK${d['end_net']:,.0f}（{change:+,.0f}，{change_pct:+.2f}%）")
    else:
        print("净值：过去24小时没有快照数据（可能今天两个市场都没开盘）")
    print(f"决策次数：{d['run_count']}（其中有实际操作{d['acted_count']}次），成交订单{d['order_count']}笔，累计手续费HK${d['total_fee_hkd']:,.2f}")
    print(f"胜率（有操作的决策里，后续净值比决策前高的比例）：{d['win_rate']:.0f}%" if d["win_rate"] is not None else "胜率：暂无可统计的操作记录")

    print("\n---- 今天每一次实际操作 ----")
    if d["all_acted"]:
        for r, c in d["all_acted"]:
            t = _to_cn(r.get("run_at"))
            try:
                sigs = json.loads(r.get("signals_json") or "[]")
            except Exception:
                sigs = []
            # 这一段的标题是"今天每一次实际操作"，那就只能列真的成交了的——
            # 被系统拦掉的买入放在这里会让日报（每天推到微信）读起来像是真买了。
            acted_sigs = [
                s for s in sigs if s.get("action") in ("买入", "卖出")
                and not s.get("_drop_reason") and s.get("_executed", True)
            ]
            blocked_sigs = [s for s in sigs if s.get("action") in ("买入", "卖出") and s.get("_drop_reason")]
            sig_line = "、".join(f"{s.get('action')}{s.get('name') or s.get('symbol')}{s.get('shares', '')}股" for s in acted_sigs) or "（信号明细缺失）"
            if blocked_sigs:
                sig_line += "［被拦截未成交：" + "、".join(
                    f"{s.get('action')}{s.get('name') or s.get('symbol')}（{s.get('_drop_reason')}）" for s in blocked_sigs
                ) + "］"
            print(f"[{t.strftime('%H:%M') if t else '--:--'}] {sig_line} —— 事后净值变化{_fmt_pct(c)}")
            reasoning = (r.get("reasoning_text") or "")[:150]
            if reasoning:
                print(f"  理由摘要：{reasoning}")
    else:
        print("（今天没有实际操作）")

    print("\n---- 当前实际持仓（含账户现价重估的浮动盈亏） ----")
    try:
        snap = sim_trader.get_agent_snapshot()
        positions = snap.get("positions") or []
        if positions:
            for p in positions:
                pl = p.get("pl_val")
                pl_text = f"{pl:+.2f}{p['currency']}" if pl is not None else "—"
                mv = p.get("market_val_hkd")
                mv_text = f"HK${mv:,.0f}" if mv is not None else "—"
                print(f"  {p['name']}（{p['code']}）{p['qty']:g}股，市值{mv_text}，浮动盈亏{pl_text}")
        else:
            print("  （当前空仓）")
        cash = tracker.get_sim_virtual_cash(email)
        print(f"剩余虚拟现金：HK${cash:,.0f}" if cash is not None else "剩余虚拟现金：未知")
        print(f"持仓总市值：HK${snap.get('holdings_value_hkd', 0):,.0f}")
    except Exception as e:
        print(f"（读取当前持仓快照失败：{e!r}）")

    lessons = tracker.get_sim_agent_lessons(email, limit=3)
    if lessons:
        print("\n---- 最近几天的长期复盘记录 ----")
        for l in lessons:
            print(f"  {l.get('lesson_text', '')[:150]}")


if __name__ == "__main__":
    import sys
    if "--detailed" in sys.argv:
        print_detailed_report(advisor._EMAIL)
    else:
        print_report(advisor._EMAIL)
    import os
    os._exit(0)
