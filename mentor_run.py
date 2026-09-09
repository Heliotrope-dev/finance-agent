# -*- coding: utf-8 -*-
"""盘中导师式推荐——把mentor_scan(机械盯盘)和mentor_interpret(AI解读)串起来
的CLI入口，给OpenClaw cron的exec步骤调用。

默认只打印结果——没有事件时打印"NO_REPLY"。加 ``--deliver`` 时，真实
事件直接通过已验证回执的微信桥投递，不再让 OpenClaw 的 agentTurn 先读
stdout 再决定是否发送。机械盯盘每 15 分钟都会运行，若每次都唤醒模型，
安静时也会浪费额度；现在只有 mentor_interpret 真正解释事件时才调用 AI。

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
    result = run(_market)
    print(result)
    if "--deliver" in sys.argv and result != "NO_REPLY":
        import wechat_delivery
        if not wechat_delivery.send_text(result):
            sys.exit(1)
    sys.stdout.flush()
    __import__("os")._exit(0)
