#!/usr/bin/env python3
"""Run OpenClaw error_watch without paying for an agent turn on quiet checks."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import wechat_delivery

_ERROR_WATCH = Path("/root/.openclaw/workspace/scripts/error_watch.py")
_MAX_MESSAGE_CHARS = 1400


def _sanitize(text: str) -> str:
    """Keep diagnostics readable and remove decorative symbols from user output."""
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]", "", text)
    return text.strip()[:_MAX_MESSAGE_CHARS]


def main() -> int:
    result = subprocess.run(
        [sys.executable, str(_ERROR_WATCH)], capture_output=True, text=True, timeout=30,
    )
    output = (result.stdout or "").strip()
    if result.returncode:
        print(f"error_watch 执行失败: {(result.stderr or output).strip()[:400]}")
        return result.returncode or 1
    if not output or output.startswith("NO_NEW_ERRORS"):
        print("NO_NEW_ERRORS")
        return 0

    message = _sanitize("系统巡检发现新异常：\n" + output)
    if not message:
        print("巡检异常内容为空，未发送")
        return 1
    if not wechat_delivery.send_text(message):
        return 1
    print("异常已通过微信桥投递")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
