# -*- coding: utf-8 -*-
"""盘中导师式推荐——把mentor_scan(机械盯盘)和mentor_interpret(AI解读)串起来
的CLI入口，给OpenClaw cron的exec步骤调用。

跟trade_alert_push.py/老版intraday_watch.py不一样：这里不直接调用微信
接口发送，只是打印结果——没有事件时打印"NO_REPLY"（复用error_watch.py
already established的"约定关键词"模式），有事件时打印导师解读正文。
OpenClaw的agentTurn读这个脚本的stdout，只有不是NO_REPLY时才用announce
把内容发到微信，这样脚本本身没有绕开delivery配置的能力，跟今天
trade_alert_push.py踩过的坑是同一类问题、这次从设计上就避免。

平静期（scan返回"静默"）：不调用mentor_interpret，零AI调用，打印NO_REPLY。
"""
import sys

import mentor_interpret
import mentor_scan


def run(market: str) -> str:
    result = mentor_scan.scan(market)
    status = result.get("状态")
    if status in ("跳过", "静默"):
        return "NO_REPLY"
    events = result.get("事件") or []
    if not events:
        return "NO_REPLY"
    now = __import__("datetime").datetime.now().strftime("%H:%M")
    header = f"投研站 · {market}盘中导师 {now}\n"
    return header + "\n" + mentor_interpret.interpret_events(events)


if __name__ == "__main__":
    if "--market" not in sys.argv:
        print("用法: python3 mentor_run.py --market HK|US")
        sys.exit(2)
    _mi = sys.argv.index("--market")
    _market = sys.argv[_mi + 1].upper() if _mi + 1 < len(sys.argv) else ""
    if _market not in ("HK", "US"):
        print(f"--market 只支持 HK/US，收到: {_market!r}")
        sys.exit(2)
    print(run(_market))
    sys.stdout.flush()
    __import__("os")._exit(0)
