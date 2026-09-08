# -*- coding: utf-8 -*-
"""盘前Top3推荐——09:00港股/21:00美股的编排入口，全部北京时间。

2026-09-08新增。流程：交易日守卫 -> advisor.judge_market_watchlist(市场50支
候选池打分) -> daily_plan.build_market_plan(生成Top3) -> render_market_text
(推送文案) -> 落盘market快照。全部资料链路复用advisor._judge_one()现成的
完整数据收集（财务+季度趋势+估值+技术面+新闻+价格位置+分析师一致预期+
筹码面+公司行为+市场环境），不简化、不裁剪——这一步是整个系统的地基。

不在这里发微信：OpenClaw的agentTurn读这个脚本的stdout，脚本本身只负责
产出正文，不直接调用微信接口。非交易日（周末或法定假日）打印NO_REPLY，
让上游完全不发消息——法定假日落在工作日时cron的星期几过滤挡不住，得
靠这里的交易日判断兜底，不能让"今天休市"这种消息也发出去。真正的交易日
但AI判断全部失败，仍然要打印说明（不是NO_REPLY）——那是真出问题了，
用户在等这份报告，不能悄无声息地什么都不说。
"""
import subprocess
import sys
from pathlib import Path

import advisor
import daily_plan

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


def run_premarket(market: str) -> int:
    if not _is_trading_day(market):
        print("NO_REPLY")
        return 0

    advisor._load_secrets_into_env()
    judged = advisor.judge_market_watchlist(market)
    if not judged:
        print(f"{market}候选池今天没有判断出任何有效结果（可能是数据源或AI供应商全挂了），不生成推荐。")
        return 1

    plan = daily_plan.build_market_plan(market)
    text = daily_plan.render_market_text(plan)
    print(text)

    import json
    out = Path(__file__).resolve().parent / "data" / f"daily_plan_{market.lower()}.json"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        print(f"[market_recommendation/{market}] 落盘失败: {e}")
    return 0


if __name__ == "__main__":
    if "--market" not in sys.argv:
        print("用法: python3 market_recommendation.py --market HK|US")
        sys.exit(2)
    _mi = sys.argv.index("--market")
    _market = sys.argv[_mi + 1].upper() if _mi + 1 < len(sys.argv) else ""
    if _market not in ("HK", "US"):
        print(f"--market 只支持 HK/US，收到: {_market!r}")
        sys.exit(2)
    code = run_premarket(_market)
    sys.stdout.flush()
    __import__("os")._exit(code)
