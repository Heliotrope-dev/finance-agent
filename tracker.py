"""追踪层 —— 记录每次分析发生时的价格，方便日后回看走势对照。

这里存了AI输出里解析出的"方向倾向"（偏多/偏空/中性）跟事后价格实际涨跌方向做
比对，算一个"方向一致率"。用户明确要这个功能，知道风险（可能被解读成"AI荐股
胜率"）后仍然选择要，所以做了——但界面上要清楚标注这不是投资建议、不保证未来
表现，只是历史记录的客观统计，避免误导。
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
                market TEXT NOT NULL DEFAULT 'A',
                name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                price_at_analysis REAL,
                analysis_text TEXT NOT NULL,
                verdict TEXT NOT NULL DEFAULT '中性',
                review_price REAL,
                review_at TEXT
            )
            """
        )
        # 老库升级：加登录之前建的库没有 email 列，兼容一下
        cols = [r[1] for r in c.execute("PRAGMA table_info(analyses)").fetchall()]
        if "email" not in cols:
            c.execute("ALTER TABLE analyses ADD COLUMN email TEXT NOT NULL DEFAULT ''")
        if "verdict" not in cols:
            c.execute("ALTER TABLE analyses ADD COLUMN verdict TEXT NOT NULL DEFAULT '中性'")
        if "market" not in cols:
            c.execute("ALTER TABLE analyses ADD COLUMN market TEXT NOT NULL DEFAULT 'A'")
        if "name" not in cols:
            c.execute("ALTER TABLE analyses ADD COLUMN name TEXT NOT NULL DEFAULT ''")

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                market TEXT NOT NULL DEFAULT 'A',
                added_at TEXT NOT NULL,
                UNIQUE(email, symbol)
            )
            """
        )
        # 老库升级：多市场之前建的自选表没有 market 列，统一按A股兼容
        wcols = [r[1] for r in c.execute("PRAGMA table_info(watchlist)").fetchall()]
        if "market" not in wcols:
            c.execute("ALTER TABLE watchlist ADD COLUMN market TEXT NOT NULL DEFAULT 'A'")

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS search_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                query TEXT NOT NULL,
                market TEXT NOT NULL DEFAULT 'A',
                searched_at TEXT NOT NULL
            )
            """
        )
        c.commit()


def add_to_watchlist(email: str, symbol: str, name: str, market: str = "A") -> bool:
    init_db()
    with closing(_conn()) as c:
        try:
            c.execute(
                "INSERT INTO watchlist (email, symbol, name, market, added_at) VALUES (?, ?, ?, ?, ?)",
                (email, symbol, name, market, datetime.now(timezone.utc).isoformat()),
            )
            c.commit()
            return True
        except sqlite3.IntegrityError:
            return False  # 已经在自选里了，不重复加


def remove_from_watchlist(email: str, symbol: str):
    with closing(_conn()) as c:
        c.execute("DELETE FROM watchlist WHERE email = ? AND symbol = ?", (email, symbol))
        c.commit()


def is_in_watchlist(email: str, symbol: str) -> bool:
    init_db()
    with closing(_conn()) as c:
        row = c.execute(
            "SELECT 1 FROM watchlist WHERE email = ? AND symbol = ?", (email, symbol)
        ).fetchone()
        return row is not None


def get_watchlist(email: str) -> list[dict]:
    init_db()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM watchlist WHERE email = ? ORDER BY added_at DESC", (email,)
        ).fetchall()
        return [dict(r) for r in rows]


def log_analysis(
    email: str, symbol: str, price_at_analysis: float, analysis_text: str,
    verdict: str = "中性", market: str = "A", name: str = "",
) -> int:
    init_db()
    with closing(_conn()) as c:
        cur = c.execute(
            "INSERT INTO analyses (email, symbol, market, name, created_at, price_at_analysis, analysis_text, verdict) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (email, symbol, market, name, datetime.now(timezone.utc).isoformat(), price_at_analysis, analysis_text, verdict),
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


def get_accuracy_stats(email: str) -> dict:
    """方向倾向 vs 实际价格走势的一致率——只统计已经回访过（review_price不为空）
    且verdict不是"中性"的记录（中性不算方向判断，不参与统计）。

    这不是"AI荐股胜率"，是历史方向标签和事后价格的客观比对，页面上展示时
    必须带"不代表未来表现"的说明，避免被理解成投资建议或收益承诺。
    """
    init_db()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM analyses WHERE email = ? AND review_price IS NOT NULL AND verdict != '中性'",
            (email,),
        ).fetchall()
    rows = [dict(r) for r in rows]
    if not rows:
        return {"总数": 0, "一致数": 0, "一致率": None}
    match = 0
    for r in rows:
        went_up = r["review_price"] > r["price_at_analysis"]
        if (r["verdict"] == "偏多" and went_up) or (r["verdict"] == "偏空" and not went_up):
            match += 1
    return {"总数": len(rows), "一致数": match, "一致率": match / len(rows) * 100}


def add_search_history(email: str, query: str, market: str = "A"):
    """记一笔"添加自选股"时搜过的关键词——给搜索弹窗里的历史记录用，方便
    常用的名字不用每次重新打字。同一个词短时间内重复搜不重复记（去重靠
    先删旧的再插入），每个用户只保留最近20条，太老的自动清掉。
    """
    init_db()
    with closing(_conn()) as c:
        c.execute("DELETE FROM search_history WHERE email = ? AND query = ?", (email, query))
        c.execute(
            "INSERT INTO search_history (email, query, market, searched_at) VALUES (?, ?, ?, ?)",
            (email, query, market, datetime.now(timezone.utc).isoformat()),
        )
        c.execute(
            """
            DELETE FROM search_history WHERE id IN (
                SELECT id FROM search_history WHERE email = ?
                ORDER BY searched_at DESC LIMIT -1 OFFSET 20
            )
            """,
            (email,),
        )
        c.commit()


def get_search_history(email: str, limit: int = 10) -> list[dict]:
    init_db()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM search_history WHERE email = ? ORDER BY searched_at DESC LIMIT ?",
            (email, limit),
        ).fetchall()
        return [dict(r) for r in rows]
