# -*- coding: utf-8 -*-
"""开盘前把操作清单推到微信。纯脚本，不经过任何模型。

不走 agent 的理由跟 trade_alert_push.py 是同一个，而且这次更硬：清单里全是
价位和止损数字，用户要照着在汇丰下单。让模型复述一遍这些数字，等于凭空
给每个数字加一次被改写的机会——模型没有任何理由把 154.00 写成 145.00，
但它确实会。脚本直出，数字从数据库到微信中间没有第二次生成。

时点按用户的实际交易节奏定：
  08:25  港股开盘（09:30）前一小时。
  21:00  美股开盘（21:30 夏令时）前半小时。
两次都在开盘前而不是收盘后——用户要的是"现在该做什么"，不是"昨天发生了
什么"。收盘后的复盘另有任务在跑。

失败处理跟成交推送一致：发送失败就报非零退出码，让 cron 的日志留痕，
不静默吞掉。清单晚到一次可以接受，不知道它没到不行。
"""
import subprocess
import sys

import advisor
import daily_plan

_WECHAT_TARGET = "o9cq80_APBq3j8dLdECzrOB0opJs@im.wechat"
_WECHAT_ACCOUNT = "b329c51975ab-im-bot"
_CHANNEL = "openclaw-weixin"

# 微信单条消息过长会被截断，清单又是"截断了就少几条标的"的性质，
# 所以超长时分段发而不是硬截。
_MAX_CHARS = 1800


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
        print(f"发送异常: {e}")
        return False
    if r.returncode != 0:
        print(f"发送失败(exit={r.returncode}): {(r.stderr or r.stdout or '').strip()[:300]}")
        return False
    return True


def _split(text: str) -> list[str]:
    """按行分段，不在一条标的中间断开。"""
    out, cur = [], []
    n = 0
    for line in text.split("\n"):
        if n + len(line) > _MAX_CHARS and cur:
            out.append("\n".join(cur))
            cur, n = [], 0
        cur.append(line)
        n += len(line) + 1
    if cur:
        out.append("\n".join(cur))
    return out


def main() -> int:
    advisor._load_secrets_into_env()
    plan = daily_plan.build_plan()

    n_pos = len(plan.get("持仓处理") or [])
    n_watch = len(plan.get("关注候选") or [])
    if not n_pos and not n_watch:
        # 没有达标标的时也发一条。用户要的是"不掉链子"——静默等于让他不知道
        # 是今天没机会，还是任务挂了。
        text = (f"投研站 · {plan['日期']} 操作清单\n\n"
                "今天没有达到阈值的标的，持仓也没有需要处理的。\n"
                "空仓是一种决定，不必为了有事做而下单。")
        ok = _send(text)
        print("已推送（空清单）" if ok else "推送失败")
        return 0 if ok else 1

    text = daily_plan.render_text(plan)
    parts = _split(text)
    for i, p in enumerate(parts, 1):
        tag = f"（{i}/{len(parts)}）" if len(parts) > 1 else ""
        if not _send(p if not tag else f"{p}\n{tag}"):
            return 1
    print(f"已推送 {len(parts)} 段，持仓{n_pos}支 候选{n_watch}支")
    return 0


if __name__ == "__main__":
    code = main()
    # 富途SDK线程非daemon，必须强制退出；os._exit 跳过stdout刷新，先flush。
    sys.stdout.flush()
    __import__("os")._exit(code)
