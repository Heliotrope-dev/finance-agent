"""AI 调用用量账本 + 付费供应商的每日上限。

2026-10-03 加的。起因：DeepSeek 余额几天内从 ¥11.8 掉到 ¥6.3，查下来大头在这个项目——
Gemini 免费档每天约 500 次，投研顾问一天要判断 400 多支（最多一天 419 条），免费额度
用完后整批落到付费的 DeepSeek，而这里既没记过每次花了多少 token，也没有任何上限，
等于"烧多少算多少"。量化交易系统(htrader)也用同一个 DeepSeek 账户，余额耗尽时两边一起停。

这里做两件事：
1. 每次调用记一行（供应商、用途标签、token、成败），回答"钱花在哪了"不再靠估算。
2. 付费供应商按北京时间自然日设调用次数上限，到了就当作这家今天不可用，交给调用方
   的兜底逻辑（和配额耗尽同一条路径），而不是继续烧。上限可用环境变量覆盖。

独立的 sqlite 文件，不碰 track_record.db 的表结构；cron 里多个进程同时写，用 WAL 和短事务。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

_BJ = timezone(timedelta(hours=8))
DB_PATH = Path(os.environ.get("FA_AI_USAGE_DB", Path(__file__).resolve().parent / "data" / "ai_usage.db"))

# 每个付费供应商每天最多调用多少次。DeepSeek 默认 300 次/天：按 10-03 的实测，
# 量化系统每天约 50 次，投研这边正常一天落到付费档的不到 200 次，300 留了余量又封住了
# "一天 400 多支全落到付费"的情形。免费档不设上限（它们自己会 429）。
# 2026-10-03 用户要求：系统性修复全部完成并确认之前，投研这边先不用 DeepSeek（上限 0 = 完全不落到付费档）。
# 确认后改回 300（或在环境里设 FA_DEEPSEEK_DAILY_CALLS）。量化交易系统(htrader)有自己的调用路径，不受这里影响。
DAILY_CALL_CAPS = {"DeepSeek": int(os.environ.get("FA_DEEPSEEK_DAILY_CALLS", "0"))}


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS ai_usage (id INTEGER PRIMARY KEY, at TEXT NOT NULL, day TEXT NOT NULL,
                 provider TEXT NOT NULL, model TEXT, tag TEXT, ok INTEGER NOT NULL, prompt_tokens INTEGER,
                 completion_tokens INTEGER, total_tokens INTEGER, error TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS ai_usage_day ON ai_usage(day, provider)")
    return c


def today() -> str:
    return datetime.now(_BJ).date().isoformat()


def record(provider: str, model: str, tag: str, ok: bool, usage=None, error: str = "") -> None:
    """记一行。记账失败绝不影响业务调用本身。"""
    try:
        pt = getattr(usage, "prompt_tokens", None) if usage is not None else None
        ct = getattr(usage, "completion_tokens", None) if usage is not None else None
        tt = getattr(usage, "total_tokens", None) if usage is not None else None
        with closing(_conn()) as c:
            c.execute("INSERT INTO ai_usage(at, day, provider, model, tag, ok, prompt_tokens, completion_tokens, "
                      "total_tokens, error) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (datetime.now(timezone.utc).isoformat(timespec="seconds"), today(), provider, model, tag or "",
                       int(bool(ok)), pt, ct, tt, (error or "")[:300]))
    except Exception:  # noqa: BLE001
        pass


def calls_today(provider: str) -> int:
    try:
        with closing(_conn()) as c:
            return int(c.execute("SELECT COUNT(*) FROM ai_usage WHERE day=? AND provider=?",
                                 (today(), provider)).fetchone()[0])
    except Exception:  # noqa: BLE001
        return 0


def over_cap(provider: str) -> bool:
    cap = DAILY_CALL_CAPS.get(provider)
    return cap is not None and calls_today(provider) >= cap


def summary(days: int = 7) -> list[dict]:
    """最近几天按 天×供应商×用途 汇总，给页面/日报用。"""
    since = (datetime.now(_BJ).date() - timedelta(days=days - 1)).isoformat()
    with closing(_conn()) as c:
        # tag 形如 "judge/AAPL"、"debate/MU"：按斜杠前的用途汇总，否则一天几百行看不出钱花在哪类任务上
        rows = c.execute("SELECT day, provider, CASE WHEN instr(tag,'/')>0 THEN substr(tag,1,instr(tag,'/')-1) ELSE tag END t, "
                         "COUNT(*), SUM(ok), SUM(COALESCE(total_tokens,0)) FROM ai_usage "
                         "WHERE day>=? GROUP BY day, provider, t ORDER BY day DESC, 6 DESC", (since,)).fetchall()
    return [{"day": d, "provider": p, "tag": t, "calls": n, "ok": k, "tokens": tok} for d, p, t, n, k, tok in rows]


if __name__ == "__main__":
    for r in summary():
        print(r)
