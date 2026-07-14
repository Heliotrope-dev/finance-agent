"""追踪层 —— 记录每次分析发生时的价格，方便日后回看走势对照。

刻意不做"预测准确率"打分：analysis.py 的输出是"依据链"而非"买卖信号"，
强行给它打对错分数既不严谨也会变成变相荐股。这里只做客观记录，
"当时分析怎么说、后来价格怎么走"，让用户自己判断，回看页面展示原始对照。
"""

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

_DB_PATH = Path(__file__).parent / "data" / "track_record.db"


def _conn():
    _DB_PATH.parent.mkdir(exist_ok=True)
    return sqlite3.connect(_DB_PATH)


def init_db():
    with closing(_conn()) as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL DEFAULT '',
                symbol TEXT NOT NULL,
                created_at TEXT NOT NULL,
                price_at_analysis REAL,
                analysis_text TEXT NOT NULL,
                review_price REAL,
                review_at TEXT
            )
            """
        )
        # 老库升级：加登录之前建的库没有 email 列，兼容一下
        cols = [r[1] for r in c.execute("PRAGMA table_info(analyses)").fetchall()]
        if "email" not in cols:
            c.execute("ALTER TABLE analyses ADD COLUMN email TEXT NOT NULL DEFAULT ''")
        c.commit()


def log_analysis(email: str, symbol: str, price_at_analysis: float, analysis_text: str) -> int:
    init_db()
    with closing(_conn()) as c:
        cur = c.execute(
            "INSERT INTO analyses (email, symbol, created_at, price_at_analysis, analysis_text) "
            "VALUES (?, ?, ?, ?, ?)",
            (email, symbol, datetime.now(timezone.utc).isoformat(), price_at_analysis, analysis_text),
        )
        c.commit()
        return cur.lastrowid


def get_history(email: str, symbol: str | None = None, limit: int = 50) -> list[dict]:
    init_db()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        if symbol:
            rows = c.execute(
                "SELECT * FROM analyses WHERE email = ? AND symbol = ? ORDER BY created_at DESC LIMIT ?",
                (email, symbol, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM analyses WHERE email = ? ORDER BY created_at DESC LIMIT ?", (email, limit),
            ).fetchall()
        return [dict(r) for r in rows]


def get_due_for_review(email: str, min_age_days: int = 7) -> list[dict]:
    """找出已经过去足够久、但还没补录回看价格的分析记录（只看当前用户自己的）。"""
    init_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=min_age_days)).isoformat()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM analyses WHERE email = ? AND review_price IS NULL AND created_at <= ?",
            (email, cutoff),
        ).fetchall()
        return [dict(r) for r in rows]


def record_review(analysis_id: int, review_price: float):
    with closing(_conn()) as c:
        c.execute(
            "UPDATE analyses SET review_price = ?, review_at = ? WHERE id = ?",
            (review_price, datetime.now(timezone.utc).isoformat(), analysis_id),
        )
        c.commit()
