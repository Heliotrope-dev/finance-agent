# -*- coding: utf-8 -*-
"""自选股评分 Top 3 简报——09:00港股/21:00美股的编排入口，全部北京时间。

2026-09-08新增，2026-09-11改版。流程：交易日守卫 -> advisor.judge_market_
watchlist(该市场自选逐支打分) -> render_watchlist_report(评分前三名，详细理由)
-> 落盘market快照。全部资料链路复用
advisor._judge_one()现成的完整数据收集（财务+季度趋势+估值+技术面+新闻+
价格位置+分析师一致预期+筹码面+公司行为+市场环境），不简化、不裁剪。

2026-09-11改版原因：老版本候选池是"自选+热门榜凑到50支"，还要求盈亏比
≥3:1才能进Top3——用户反馈两个问题都是真的：(1)天天在扫一堆自己根本不
关心的热门股，白费token；(2)盈亏比闸门太严，连续好几天Top3是空的，报告
变成"无"，等于没有产出。现在改成：候选池就是自选本身（不再补热门股），
每一支仍完整评分，但微信只播报综合得分前三名；盈亏比仍属于可执行计划的
确定性风控闸门，不混进研究排名。

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
import tracker

_TRADING_CAL = Path("/root/.openclaw/workspace/scripts/trading_cal.py")

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


def _extract_report_reason(text: str, max_len: int = 420) -> str:
    """提取 Top 3 用的完整理由，不把维度分和总分重复塞进正文。"""
    import re

    m = re.search(
        r"\*{0,2}理由\*{0,2}\s*[：:]\s*(.+?)(?=\n\s*(?:\*{0,2}(?:维度打分|综合得分|置信度)\*{0,2})\s*[：:]|\Z)",
        text or "", re.S,
    )
    if not m:
        return advisor._extract_short_reason(text) or "未能解析出详细理由，请以应用内完整判断为准。"
    reason = re.sub(r"\s+", " ", m.group(1)).strip()
    if len(reason) > max_len:
        reason = reason[:max_len].rstrip("，,、；; ") + "…"
    return reason


def _format_breakdown(text: str) -> str:
    import re

    special = re.search(r"专用维度打分\s*[：:]\s*(.+)", text or "")
    if special:
        return special.group(1).strip()
    breakdown = tracker.extract_score_breakdown(text or "")
    labels = (
        ("fundamental", "基本面", 22), ("price_position", "价格位置", 20),
        ("technical", "技术面", 20), ("chips", "筹码面", 20),
        ("analyst", "分析师预期", 8), ("data_certainty", "数据确定性", 10),
    )
    if any(breakdown.get(key) is None for key, _, _ in labels):
        return ""
    return " · ".join(f"{label}{breakdown[key]}/{maximum}" for key, label, maximum in labels)


def render_watchlist_report(market: str, judged: list[dict]) -> str:
    """股票和杠杆产品按各自尺子排名；加密资产只给四级结论。"""
    market_label = {"HK": "港股", "US": "美股"}.get(market, market)
    rows = []
    for e in judged:
        text = e.get("fundamental_verdict", "") or ""
        rows.append({
            "symbol": e.get("symbol", ""),
            "name": e.get("name") or e.get("symbol", ""),
            "action": e.get("action", "观望"),
            "score": e.get("score"),
            "price": e.get("price"),
            "reason": _extract_report_reason(text),
            "brief_reason": advisor._extract_short_reason(text, max_len=150),
            "breakdown": _format_breakdown(text),
            "asset_kind": e.get("asset_kind") or advisor._asset_kind(market, e.get("name") or ""),
        })
    labels = {"equity": "普通股票", "leveraged_inverse": "杠杆/反向产品（专用评分）"}
    lines = [f"【{market_label}自选评分】已完成{len(rows)}支评分；不同资产类别使用不同评分尺子，不跨类混排。"]
    for kind, limit in (("equity", 3), ("leveraged_inverse", 2)):
        group = [r for r in rows if r["asset_kind"] == kind]
        if not group:
            continue
        group.sort(key=lambda r: -(r["score"] if r["score"] is not None else -1))
        top_rows = group[:limit]
        lines.append(f"\n{labels[kind]} Top {len(top_rows)}（类内按分数从高到低）：")
        for rank, r in enumerate(top_rows, start=1):
            score_text = f"{r['score']}分" if r["score"] is not None else "分数未知"
            price_text = f"{r['price']:.2f}" if isinstance(r["price"], (int, float)) else "—"
            lines.append(
                f"\n{rank}. {r['name']}（{r['symbol']}）\n"
                f"现价{price_text} · 结论{r['action']} · 综合{score_text}\n"
                + (f"评分构成：{r['breakdown']}\n" if r["breakdown"] else "")
                + f"详细理由：{r['reason']}"
            )
    # 加密市场的系统性行情高度一致，分数排名会制造不必要的精度错觉。
    # 仅美股晚报带入用户的加密自选，每个标的只给一个可执行的四级结论和短理由。
    crypto_rows = [r for r in rows if r["asset_kind"] == "crypto"]
    if crypto_rows:
        lines.append("\n加密自选判断（不做分数排名）：")
        for r in crypto_rows:
            action = r["action"] if r["action"] in {"买入", "持有", "卖出", "观望"} else "观望"
            reason = r["brief_reason"] or "数据不足，暂以观望处理。"
            lines.append(f"- {r['name']}（{r['symbol']}）：{action}。{reason}")
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
