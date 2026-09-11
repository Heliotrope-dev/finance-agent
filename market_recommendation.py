# -*- coding: utf-8 -*-
"""自选股逐支评分简报——09:00港股/21:00美股的编排入口，全部北京时间。

2026-09-08新增，2026-09-11改版。流程：交易日守卫 -> advisor.judge_market_
watchlist(该市场自选逐支打分) -> render_watchlist_report(每支都出：评分+
方向+一句话理由) -> render_market_text -> 落盘market快照。全部资料链路复用
advisor._judge_one()现成的完整数据收集（财务+季度趋势+估值+技术面+新闻+
价格位置+分析师一致预期+筹码面+公司行为+市场环境），不简化、不裁剪。

2026-09-11改版原因：老版本候选池是"自选+热门榜凑到50支"，还要求盈亏比
≥3:1才能进Top3——用户反馈两个问题都是真的：(1)天天在扫一堆自己根本不
关心的热门股，白费token；(2)盈亏比闸门太严，连续好几天Top3是空的，报告
变成"无"，等于没有产出。现在改成：候选池就是自选本身（不再补热门股），
每一支都出评分+方向+理由，不再用盈亏比卡掉大多数——盈亏比信息还在，作为
每支的参考数据点，不再是能不能出现在报告里的门槛。

默认只把正文打印到 stdout，方便人工排查；加 ``--deliver`` 后，由项目内
已验证回执的微信桥直接投递。不要经由 OpenClaw agentTurn 转发：这份扫描
会超过通用 exec 工具的 300 秒上限，代理会卡住，结果既不落地也不送达。
非交易日（周末或法定假日）打印 NO_REPLY 且不投递。真正的交易日但 AI 判断
全部失败仍打印说明（不是 NO_REPLY）——那是真出问题了，用户在等这份报告，
不能悄无声息地什么都不说。
"""
import subprocess
import sys
from pathlib import Path

import advisor

_TRADING_CAL = Path("/root/.openclaw/workspace/scripts/trading_cal.py")

# 结论排序：买入排最前面，其次持有，然后观望，卖出排最后——用户翻简报
# 时最想先看到的是"现在能不能买"，其次是"已经在拿的还要不要留"，"不用管"
# 的排后面，跟“操作紧迫度”对齐，不是随便挑的顺序。
_ACTION_ORDER = {"买入": 0, "持有": 1, "观望": 2, "卖出": 3}


def _is_trading_day(market: str) -> bool:
    if not _TRADING_CAL.exists():
        return True
    try:
        r = subprocess.run(
            ["python3", str(_TRADING_CAL), market], capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() != "False"
    except Exception:
        return True


def render_watchlist_report(market: str, judged: list[dict]) -> str:
    """每支自选都出一行：名称(代码) 方向 评分分 | 一句话理由。按"结论紧迫度
    优先、同结论内按分数降序"排序——跟老版本"只挑几支"的Top3思路不同，这里
    是"全员点名"，用户自己的自选股不该有谁被悄悄漏掉不提。"""
    market_label = {"HK": "港股", "US": "美股"}.get(market, market)
    rows = []
    for e in judged:
        text = e.get("fundamental_verdict", "") or ""
        reason = advisor._extract_short_reason(text) or "（未能解析出理由，见完整判断记录）"
        rows.append({
            "symbol": e.get("symbol", ""),
            "name": e.get("name") or e.get("symbol", ""),
            "action": e.get("action", "观望"),
            "score": e.get("score"),
            "price": e.get("price"),
            "reason": reason,
        })
    rows.sort(key=lambda r: (_ACTION_ORDER.get(r["action"], 9), -(r["score"] or 0)))

    lines = [f"【{market_label}自选评分】{len(rows)}支，逐支给方向和理由，不是下单指令："]
    for r in rows:
        score_text = f"{r['score']}分" if r["score"] is not None else "分数未知"
        price_text = f"{r['price']:.2f}" if isinstance(r["price"], (int, float)) else "—"
        lines.append(
            f"\n{r['name']}({r['symbol']}) 现价{price_text} · {r['action']} · {score_text}\n"
            f"  {r['reason']}"
        )
    lines.append(
        "\n仅供参考，不构成投资建议——过往判断的方向一致率参见「我的」页"
        "AI判断准确率，目前还在被验证阶段，不是确定性预测。"
    )
    return "\n".join(lines)


def run_premarket(market: str, *, deliver: bool = False) -> int:
    if not _is_trading_day(market):
        print("NO_REPLY")
        return 0

    advisor._load_secrets_into_env()
    judged = advisor.judge_market_watchlist(market)
    if not judged:
        text = f"{market}自选今天没有判断出任何有效结果（可能是自选为空、数据源或AI供应商全挂了），不生成推荐。"
        print(text)
        if deliver:
            import wechat_delivery
            wechat_delivery.send_text(text)
        return 1

    text = render_watchlist_report(market, judged)
    print(text)

    import datetime as _dt
    import json
    data_dir = Path(__file__).resolve().parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # 逐支评分的原始结果落在自己的文件里。
    #
    # 2026-09-11真实故障：这里原来写的是 daily_plan_{market}.json——那是
    # daily_plan.py 的产物，schema 完全不同（关注候选/新开仓状态/买入区间/
    # 止损/目标/盈亏比 那一整套经过仓位测算和闸门校验的字段）。这个函数拿
    # {市场,候选池规模,结果} 把它整个覆盖掉，结果是首页"今日可执行清单"、
    # plan_push.py、mentor_scan.py、mentor_interpret.py、intraday_watch.py
    # 五个下游读到的全是没有这些键的字典，一律退化成空——用户看到的就是
    # 首页那块天天什么都没有。两份产物各用各的文件名，不再互相覆盖。
    try:
        (data_dir / f"watchlist_report_{market.lower()}.json").write_text(
            json.dumps({
                "市场": market,
                "日期": _dt.date.today().isoformat(),
                "候选池规模": len(judged),
                "结果": judged,
            }, ensure_ascii=False, indent=1, default=str),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[market_recommendation/{market}] 评分快照落盘失败: {e}")

    # 评分结果已经进了 advice 表（judge_market_watchlist 里 log_advice 写的
    # source=watchlist_{market}），daily_plan 正是从那张表读候选池。所以这里
    # 顺手把当天的可执行计划也算出来——同一次运行产出两样东西：给微信的
    # 逐支评分正文，和给首页"今日可执行清单"的带仓位/止损/目标的计划。
    # 以前这一步靠一个单独的 cron 跑 daily_plan.py，那个 cron 早就停了，
    # 于是计划文件一直是旧的。
    try:
        import daily_plan
        plan = daily_plan.build_market_plan(market)
        (data_dir / f"daily_plan_{market.lower()}.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=1, default=str), encoding="utf-8",
        )
        print(f"[market_recommendation/{market}] 可执行计划已更新："
              f"关注候选{len(plan.get('关注候选') or [])}条")
    except Exception as e:
        print(f"[market_recommendation/{market}] 可执行计划生成失败: {e}")
    if deliver:
        import wechat_delivery
        if not wechat_delivery.send_text(text):
            return 1
    return 0


if __name__ == "__main__":
    if "--market" not in sys.argv:
        print("用法: python3 market_recommendation.py --market HK|US [--deliver]")
        sys.exit(2)
    _mi = sys.argv.index("--market")
    _market = sys.argv[_mi + 1].upper() if _mi + 1 < len(sys.argv) else ""
    if _market not in ("HK", "US"):
        print(f"--market 只支持 HK/US，收到: {_market!r}")
        sys.exit(2)
    code = run_premarket(_market, deliver="--deliver" in sys.argv)
    sys.stdout.flush()
    __import__("os")._exit(code)
