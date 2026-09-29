#!/usr/bin/env python3
"""FutuOpenD 主动探活 + 自愈。

为什么需要这个：2026-09-18 FutuOpenD 挂起（不是进程退出，是连接握手一直
超时），systemd 的 Restart=always 只在进程真正退出时触发，对"进程还在但
不响应"这种挂起完全没用——一直卡到09-19中午才有人手动发现重启，advisor.py
那几个 cron 因此连续失败了一整周都没人知道。

之前那个"系统自监控-报错自愈"（ops_error_watch.py）只是扫日志关键词发
微信通知，从来没有真正连过 FutuOpenD，也不会重启任何东西——两个任务分开，
不合并进那个文件，避免它的职责从"扫日志"意外膨胀成"到处重启服务"。

这里只做一件事：真的建一个连接、真的请求一次数据、超时就重启服务并
微信通知，不静默处理——自愈可以，但用户应该知道发生过一次。
"""
from __future__ import annotations

import subprocess
import sys
import time

import wechat_delivery

_TIMEOUT_SECONDS = 8


def _check_futu_alive() -> tuple[bool, str]:
    """真实建立一次连接并请求全局状态，不是只看进程是不是在跑。

    子进程 + 硬超时：futu SDK 的连接超时参数在"网关进程活着但不响应"这种
    情况下并不总是可靠触发（就是09-18那次的样子），用 subprocess.run 的
    timeout 兜底，这一层不能信任 SDK 自己的超时。
    """
    code = (
        "import futu as ft\n"
        "q = ft.OpenQuoteContext(host='127.0.0.1', port=11111)\n"
        "ret, data = q.get_global_state()\n"
        "q.close()\n"
        "raise SystemExit(0 if ret == 0 else 1)\n"
    )
    try:
        result = subprocess.run(
            ["/root/finance-agent/venv/bin/python3", "-c", code],
            capture_output=True, text=True, timeout=_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False, f"连接测试本身超过{_TIMEOUT_SECONDS}秒未返回（挂起的典型表现）"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return False, (detail[-1] if detail else f"exit={result.returncode}")
    return True, ""


def main() -> int:
    ok, detail = _check_futu_alive()
    if ok:
        print("FutuOpenD 正常")
        return 0

    print(f"FutuOpenD 探活失败: {detail}，尝试重启服务")
    restart = subprocess.run(
        ["systemctl", "restart", "futu-opend"], capture_output=True, text=True, timeout=20,
    )
    if restart.returncode != 0:
        msg = f"⚠️ FutuOpenD 探活失败（{detail}），重启服务也失败：{restart.stderr.strip()[:300]}，需要人工介入"
        print(msg)
        wechat_delivery.send_text(msg)
        return 1

    # 重启不是立刻可用，给它几秒起来再复检一次，确认是真的自愈了还是又会挂住。
    time.sleep(6)
    ok2, detail2 = _check_futu_alive()
    if ok2:
        msg = f"FutuOpenD 探活失败（{detail}），已自动重启并确认恢复正常。"
        print(msg)
        wechat_delivery.send_text(msg)
        return 0

    msg = f"⚠️ FutuOpenD 探活失败（{detail}），重启后复检仍失败（{detail2}），需要人工介入"
    print(msg)
    wechat_delivery.send_text(msg)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
