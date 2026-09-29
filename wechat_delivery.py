# -*- coding: utf-8 -*-
"""Disabled notification sink.

Invest Agent deliberately has no messaging-channel dependency. The website,
portfolio snapshots, research and local schedules must keep working when no
push channel is installed. Callers use this function as the delivery boundary;
while delivery is disabled, a message is intentionally consumed rather than
being retried forever or reported as a task failure.
"""
from __future__ import annotations

DELIVERY_ENABLED = False
DELIVERY_STATUS = "disabled"   # disabled | queued | sent | failed (审计P2-18)


def status_word() -> str:
    """日志里用的动词：停用时不能写“已推送”，避免让人以为消息送达了。"""
    return "已推送" if DELIVERY_ENABLED else "已记录（推送通道停用，未发送）"


def send_text(message: str, *, timeout: int = 45) -> bool:
    """Consume a notification when outbound delivery is intentionally disabled.

    ``True`` means the notification pipeline completed its configured action;
    it never means that a user received a message. This lets deduplicating
    alert queues retire events instead of retrying an unavailable legacy route.
    """
    del timeout
    if not isinstance(message, str) or not message.strip():
        print("通知已跳过：内容为空")
        return False
    print("通知投递已停用：消息仅记录在任务日志，未发送到微信")
    return True
