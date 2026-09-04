# -*- coding: utf-8 -*-
"""把AI模拟盘的新成交推送到微信。定时任务直接跑这个脚本，不经过任何模型。

2026-09-04用户反馈"open claw说什么乱话呢"——微信上每5分钟收到一条
"我来检查AI模拟炒股的成交通知情况。"。查下来是这条提醒任务原本是 agentTurn：
提示词要求"没有待推送就只输出 NO_REPLY"，但模型不照做，先说了一句开场白，
而 cron 的 announce 投递会把最终回复原样发出去，于是开场白就成了推送内容。

问题不在提示词写得不够严，在于这件事根本不该交给模型：查数据库、有就发、
发完销账，全是确定性逻辑，没有一步需要判断。交给模型等于凭空引入两种故障
——它可能不按格式回答，也可能把该发的漏掉——还要为此每天烧掉两三百次调用。
改成纯脚本之后这两种故障和这笔开销一起消失了。

发送走 openclaw message send 命令行，跟模型无关。销账严格在发送成功之后，
失败就不销账、下一轮重试：重复推一次可以接受，漏掉不行。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import advisor
import tracker

_WECHAT_TARGET = "o9cq80_APBq3j8dLdECzrOB0opJs@im.wechat"
_WECHAT_ACCOUNT = "b329c51975ab-im-bot"
_CHANNEL = "openclaw-weixin"
_RUN_LOG = Path(__file__).resolve().parent / "data" / "last_sim_agent_run.log"


def _clean_name(name: str, action: str) -> str:
    """name 字段历史上混进过"买入 快手-W"这种带动作前缀的值，剥掉，
    免得推送出来变成"买入 买入 快手-W"。"""
    name = (name or "").strip()
    for act in ("买入", "卖出"):
        if name.startswith(act):
            name = name[len(act):].strip()
    return name or "-"


def _reason_text() -> str:
    """从上一轮决策日志里取一句理由。取不到就返回空——宁可只报成交事实，
    也不编一个理由出来。"""
    try:
        raw = _RUN_LOG.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    # 文件开头可能混有streamlit的WARNING日志行，只取最后那段JSON
    start = raw.rfind("{")
    if start == -1:
        return ""
    try:
        data = json.loads(raw[start:])
    except Exception:
        return ""
    reason = (data.get("reasoning") or "").strip().replace("\n", " ")
    return reason[:160] + ("…" if len(reason) > 160 else "") if reason else ""


def _build_message(orders: list[dict]) -> str:
    lines = [f"AI模拟盘新成交 {len(orders)} 笔"]
    for o in orders:
        shares = o.get("shares_ordered") or 0
        shares_text = f"{shares:.0f}" if float(shares).is_integer() else f"{shares}"
        lines.append(
            f"{o.get('action')} {_clean_name(o.get('name'), o.get('action'))}"
            f"（{o.get('symbol')}·{o.get('market')}）{shares_text}股"
        )
    reason = _reason_text()
    if reason:
        lines.append("")
        lines.append(f"本轮理由：{reason}")
    return "\n".join(lines)


def _send(message: str) -> bool:
    try:
        r = subprocess.run(
            ["openclaw", "message", "send",
             "--channel", _CHANNEL,
             "--target", _WECHAT_TARGET,
             "--account", _WECHAT_ACCOUNT,
             "--message", message],
            capture_output=True, text=True, timeout=120,
        )
    except Exception as e:
        print(f"发送异常，本轮不销账，下一轮重试: {e}")
        return False
    if r.returncode != 0:
        print(f"发送失败(exit={r.returncode})，本轮不销账，下一轮重试: "
              f"{(r.stderr or r.stdout or '').strip()[:300]}")
        return False
    return True


def main() -> int:
    email = advisor._EMAIL
    orders = tracker.get_unnotified_orders(email)
    if not orders:
        print("NO_PENDING")
        return 0

    message = _build_message(orders)
    if not _send(message):
        return 1

    # 严格在发送成功之后才销账。反过来（销了账却没发出去）这几笔就永远不会
    # 再被推送，那正是用户明确不想要的"漏掉"；而"发出去了但销账失败"最多
    # 下一轮重复推一次，可以接受。
    n = tracker.mark_orders_notified([o["id"] for o in orders])
    print(f"已推送 {len(orders)} 笔，销账 {n} 笔")
    return 0


if __name__ == "__main__":
    code = main()
    # advisor 会间接把富途相关模块带进来，那些线程不是daemon线程（项目老坑），
    # 不强制退出进程会挂住。os._exit 跳过stdout缓冲区刷新，管道输出会整个丢掉，
    # 所以必须先flush。
    sys.stdout.flush()
    os._exit(code)
