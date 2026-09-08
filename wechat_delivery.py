# -*- coding: utf-8 -*-
"""Context-bound Weixin delivery for deterministic finance notifications.

The OpenClaw CLI reports a queued send as successful even when its fresh
process has no in-memory Weixin context token.  This wrapper uses the companion
Node bridge, which restores the plugin's persisted token and fails closed when
the platform cannot accept a context-bound message.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

WECHAT_TARGET = "o9cq80_APBq3j8dLdECzrOB0opJs@im.wechat"
WECHAT_ACCOUNT = "b329c51975ab-im-bot"
_BRIDGE = Path(__file__).with_name("wechat_gateway_send.mjs")


def send_text(message: str, *, timeout: int = 45) -> bool:
    """Send one message and return true only after the Weixin API accepts it."""
    try:
        result = subprocess.run(
            ["node", str(_BRIDGE), WECHAT_ACCOUNT, WECHAT_TARGET, message],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:
        print(f"微信发送异常: {exc}")
        return False

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().replace("\n", " ")
        print(f"微信发送失败(exit={result.returncode}): {detail[:400]}")
        return False

    try:
        receipt = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        print("微信发送失败: 发送桥未返回可验证回执")
        return False
    if not receipt.get("contextBound") or not receipt.get("messageId"):
        print("微信发送失败: 回执缺少上下文绑定或消息编号")
        return False
    print(f"微信已由接口受理: {receipt['messageId']}")
    return True
