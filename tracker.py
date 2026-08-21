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
        # WAL模式：_conn()每次都是新开关一次连接（没有常驻连接池），默认的
        # rollback journal模式下写操作会短暂独占锁、并发读写容易互相等待；
        # WAL允许读和写并发进行，对这种"频繁短连接"的用法更合适。
        c.execute("PRAGMA journal_mode=WAL")
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

        # advice：advisor.py（每日投研顾问脚本，不在Streamlit页面里）生成的买卖
        # 建议记录，跟analyses表是同一套"记录判断→回填价格→算一致率"的模式，
        # 但字段语义不同（分基本面/技术面两段、action是买卖持有观望而不是方向
        # 偏多偏空）所以用独立表，不复用analyses。
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS advice (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL DEFAULT '',
                symbol TEXT NOT NULL,
                market TEXT NOT NULL DEFAULT 'A',
                name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                price_at_advice REAL,
                fundamental_verdict TEXT NOT NULL DEFAULT '',
                technical_signal TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL DEFAULT '观望',
                source TEXT NOT NULL DEFAULT 'watchlist',
                review_price REAL,
                review_at TEXT
            )
            """
        )

        # positions：取代 watchlist 的"持仓分析"数据模型。shares=0 表示"只关注
        # 不持仓"（保留旧自选股的语义，详情页"加入自选"按钮不用大改）。
        # cost_total存累计成本(原币种)而不是均价——用户输入的是"股数+金额"，
        # 加仓就是shares+=n,cost_total+=amount，不会滚浮点误差；均价现算
        # cost_total/shares。currency显式存不从market现算，以后遇到"市场跟
        # 计价币不一致"(比如ADR)的情况改一行数据就行，不用改代码逻辑。
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                market TEXT NOT NULL DEFAULT 'A',
                shares REAL NOT NULL DEFAULT 0,
                cost_total REAL NOT NULL DEFAULT 0,
                currency TEXT NOT NULL DEFAULT 'CNY',
                opened_at TEXT NOT NULL DEFAULT '',
                added_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(email, symbol)
            )
            """
        )

        # position_lots：只写不读的买卖流水账，供以后需要精确核算(比如TWR)时用，
        # 现在这些漏记的话以后补不回来，先建上成本很低。
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS position_lots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL DEFAULT 'A',
                action TEXT NOT NULL,
                shares REAL NOT NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT 'CNY',
                traded_at TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT ''
            )
            """
        )

        # portfolio_advice：跟advice表字段语义不同(没有symbol/market/technical_
        # signal，多了total_value_cny/holdings_json)，独立建表——参考advice表
        # 自己的注释，字段语义不同就不硬塞进同一张表。
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_advice (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                total_value_cny REAL,
                holdings_json TEXT NOT NULL DEFAULT '',
                analysis_text TEXT NOT NULL DEFAULT ''
            )
            """
        )

        # 从watchlist一次性迁移进positions，shares/cost_total都是0(纯关注)。
        # UNIQUE(email,symbol)保证INSERT OR IGNORE天然幂等，每次启动跑一遍
        # 无副作用，不会覆盖已经有真实持仓数据的行。
        c.execute(
            """
            INSERT OR IGNORE INTO positions
                (email, symbol, name, market, shares, cost_total, currency, opened_at, added_at, updated_at)
            SELECT email, symbol, name, market, 0, 0,
                   CASE market WHEN 'HK' THEN 'HKD' WHEN 'US' THEN 'USD' ELSE 'CNY' END,
                   '', added_at, added_at
            FROM watchlist
            """
        )
        c.commit()


_MARKET_CURRENCY = {"A": "CNY", "HK": "HKD", "US": "USD"}


def add_to_watchlist(email: str, symbol: str, name: str, market: str = "A") -> bool:
    """薄封装：在positions里插入一行shares=0(只关注不持仓)的记录——正式的
    "持仓分析"功能上线后，这个函数名保留不改，是为了详情页"加入自选"按钮
    (app.py) 不用跟着大改，数据层换底座、调用方无感知。真正记持仓用
    upsert_position。"""
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    with closing(_conn()) as c:
        try:
            c.execute(
                "INSERT INTO positions (email, symbol, name, market, shares, cost_total, currency, opened_at, added_at, updated_at) "
                "VALUES (?, ?, ?, ?, 0, 0, ?, '', ?, ?)",
                (email, symbol, name, market, _MARKET_CURRENCY.get(market, "CNY"), now, now),
            )
            c.commit()
            return True
        except sqlite3.IntegrityError:
            return False  # 已经在里面了，不重复加


def remove_from_watchlist(email: str, symbol: str):
    """薄封装：等价于delete_position——不管有没有真实持仓，整行删掉。"""
    delete_position(email, symbol)


def is_in_watchlist(email: str, symbol: str) -> bool:
    init_db()
    with closing(_conn()) as c:
        row = c.execute(
            "SELECT 1 FROM positions WHERE email = ? AND symbol = ?", (email, symbol)
        ).fetchone()
        return row is not None


def get_watchlist(email: str) -> list[dict]:
    """薄封装：等价于get_positions，函数名保留给advisor.py等老调用点用。"""
    return get_positions(email)


def get_positions(email: str) -> list[dict]:
    init_db()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM positions WHERE email = ? ORDER BY added_at DESC", (email,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_position_emails() -> list[str]:
    """给advisor.py的组合AI分析用——真正有持仓(shares>0)的用户邮箱列表，
    只关注不持仓的不算"持仓"，不需要跑组合分析。"""
    init_db()
    with closing(_conn()) as c:
        rows = c.execute("SELECT DISTINCT email FROM positions WHERE shares > 0").fetchall()
        return [r[0] for r in rows]


def log_position_lot(
    email: str, symbol: str, market: str, action: str, shares: float, amount: float,
    currency: str = "CNY", note: str = "",
):
    with closing(_conn()) as c:
        c.execute(
            "INSERT INTO position_lots (email, symbol, market, action, shares, amount, currency, traded_at, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (email, symbol, market, action, shares, amount, currency, datetime.now(timezone.utc).isoformat(), note),
        )
        c.commit()


def upsert_position(
    email: str, symbol: str, name: str, market: str, add_shares: float, add_amount: float,
    currency: str | None = None,
) -> None:
    """买入/加仓。加权平均成本：shares和cost_total都是累加，均价现算
    cost_total/shares，不会因为多次加仓滚浮点误差。同时记一笔买入流水。"""
    init_db()
    currency = currency or _MARKET_CURRENCY.get(market, "CNY")
    now = datetime.now(timezone.utc).isoformat()
    with closing(_conn()) as c:
        row = c.execute(
            "SELECT shares, opened_at FROM positions WHERE email = ? AND symbol = ?", (email, symbol)
        ).fetchone()
        if row is None:
            c.execute(
                "INSERT INTO positions (email, symbol, name, market, shares, cost_total, currency, opened_at, added_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (email, symbol, name, market, add_shares, add_amount, currency, now, now, now),
            )
        else:
            prev_shares, opened_at = row
            # 之前是"只关注"(shares=0)转成真正持仓，opened_at补上这次建仓时间
            new_opened_at = opened_at if prev_shares and prev_shares > 0 and opened_at else now
            c.execute(
                "UPDATE positions SET shares = shares + ?, cost_total = cost_total + ?, "
                "name = ?, currency = ?, opened_at = ?, updated_at = ? WHERE email = ? AND symbol = ?",
                (add_shares, add_amount, name, currency, new_opened_at, now, email, symbol),
            )
        c.commit()
    log_position_lot(email, symbol, market, "买入", add_shares, add_amount, currency)


def reduce_position(email: str, symbol: str, sell_shares: float, sell_amount: float) -> None:
    """卖出/减仓。按当前均价比例扣减cost_total，避免"先卖出高成本部分"这类
    分批假设——这个工具存的是加权平均成本，不区分批次。减到≤0直接删掉这行
    (用户明确要求：卖光了就不留"仅关注"状态，这跟"从来没买过"的shares=0
    是两回事，不要混）。同时记一笔卖出流水。"""
    init_db()
    with closing(_conn()) as c:
        row = c.execute(
            "SELECT shares, cost_total, market, currency FROM positions WHERE email = ? AND symbol = ?",
            (email, symbol),
        ).fetchone()
        if row is None:
            return
        shares, cost_total, market, currency = row
        sell_shares = min(sell_shares, shares) if shares else sell_shares
        avg_cost = (cost_total / shares) if shares else 0
        new_shares = shares - sell_shares
        new_cost_total = max(cost_total - avg_cost * sell_shares, 0)
        if new_shares <= 1e-6:
            c.execute("DELETE FROM positions WHERE email = ? AND symbol = ?", (email, symbol))
        else:
            c.execute(
                "UPDATE positions SET shares = ?, cost_total = ?, updated_at = ? WHERE email = ? AND symbol = ?",
                (new_shares, new_cost_total, datetime.now(timezone.utc).isoformat(), email, symbol),
            )
        c.commit()
    log_position_lot(email, symbol, market, "卖出", sell_shares, sell_amount, currency)


def delete_position(email: str, symbol: str):
    with closing(_conn()) as c:
        c.execute("DELETE FROM positions WHERE email = ? AND symbol = ?", (email, symbol))
        c.commit()


def log_portfolio_advice(email: str, total_value_cny: float | None, holdings_json: str, analysis_text: str) -> int:
    init_db()
    with closing(_conn()) as c:
        cur = c.execute(
            "INSERT INTO portfolio_advice (email, created_at, total_value_cny, holdings_json, analysis_text) "
            "VALUES (?, ?, ?, ?, ?)",
            (email, datetime.now(timezone.utc).isoformat(), total_value_cny, holdings_json, analysis_text),
        )
        c.commit()
        return cur.lastrowid


def get_latest_portfolio_advice(email: str) -> dict | None:
    init_db()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT * FROM portfolio_advice WHERE email = ? ORDER BY created_at DESC LIMIT 1", (email,)
        ).fetchone()
        return dict(row) if row else None


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


def get_due_for_review(email: str, min_age_days: int = 7, limit: int = 20) -> list[dict]:
    """找出已经过去足够久、但还没补录回看价格的分析记录（只看当前用户自己的）。

    limit：这份列表拿到手后，调用方要逐条发网络请求去查实时价格补录——不加上限
    的话，用户攒得越多，侧边栏「历史回看」这个st.expander每次页面渲染（不管
    展没展开都会执行内部代码）就要发越多请求，越用越慢。默认给20条，按
    created_at从旧到新补，配合调用方改成并发请求。
    """
    init_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=min_age_days)).isoformat()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM analyses WHERE email = ? AND review_price IS NULL AND created_at <= ? "
            "ORDER BY created_at ASC LIMIT ?",
            (email, cutoff, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def record_review(analysis_id: int, review_price: float):
    with closing(_conn()) as c:
        c.execute(
            "UPDATE analyses SET review_price = ?, review_at = ? WHERE id = ?",
            (review_price, datetime.now(timezone.utc).isoformat(), analysis_id),
        )
        c.commit()


def _accuracy_from_rows(rows: list[dict]) -> dict:
    if not rows:
        return {"总数": 0, "一致数": 0, "一致率": None}
    match = 0
    for r in rows:
        went_up = r["review_price"] > r["price_at_analysis"]
        if (r["verdict"] == "偏多" and went_up) or (r["verdict"] == "偏空" and not went_up):
            match += 1
    return {"总数": len(rows), "一致数": match, "一致率": match / len(rows) * 100}


def get_accuracy_stats(email: str) -> dict:
    """方向倾向 vs 实际价格走势的一致率——只统计已经回访过（review_price不为空）
    且verdict不是"中性"的记录（中性不算方向判断，不参与统计）。

    这不是"AI荐股胜率"，是历史方向标签和事后价格的客观比对，页面上展示时
    必须带"不代表未来表现"的说明，避免被理解成投资建议或收益承诺。

    除了总体一致率，额外按市场（A/HK/US）和按方向（偏多/偏空）拆分出子统计
    （"按市场""按方向"两个字段，各自是"分组值 -> 同样结构的统计字典"）——
    笼统的一个数字看不出"AI是在A股准还是在美股准""偏多判断准还是偏空判断准"，
    拆开看才有实际分析价值。样本量小的分组（比如只有1-2条）算出来的百分比
    统计意义不大，前端展示时会按总数决定要不要显示。
    """
    init_db()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM analyses WHERE email = ? AND review_price IS NOT NULL AND verdict != '中性'",
            (email,),
        ).fetchall()
    rows = [dict(r) for r in rows]

    stats = _accuracy_from_rows(rows)
    stats["按市场"] = {
        m: _accuracy_from_rows([r for r in rows if r.get("market", "A") == m])
        for m in sorted(set(r.get("market", "A") for r in rows))
    }
    stats["按方向"] = {
        v: _accuracy_from_rows([r for r in rows if r["verdict"] == v])
        for v in ("偏多", "偏空")
        if any(r["verdict"] == v for r in rows)
    }
    return stats


def get_accuracy_trend(email: str, window: int = 5) -> list[dict]:
    """"方向一致率"随时间的变化趋势——之前只有 get_accuracy_stats 算出来的
    一个孤零零的总体百分比，看不出这个数字是一直稳定在这附近，还是最近变好/
    变差了。这里按 created_at 升序排列已回看的记录（口径跟 get_accuracy_stats
    一致：只看 review_price 不为空、verdict 不是"中性"的），用滑动窗口
    （默认最近5次）算出每个时间点"往前数window次"的区间一致率，串成一条趋势线。

    样本少于 window+1 条时趋势没有意义（只能画出0-1个点），直接返回空列表，
    调用方应该判断空列表就不画图、只保留原来那个总体的 st.metric。
    """
    init_db()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM analyses WHERE email = ? AND review_price IS NOT NULL AND verdict != '中性' "
            "ORDER BY created_at ASC",
            (email,),
        ).fetchall()
    rows = [dict(r) for r in rows]
    if len(rows) < window + 1:
        return []

    trend = []
    for i in range(window - 1, len(rows)):
        window_rows = rows[i - window + 1 : i + 1]
        acc = _accuracy_from_rows(window_rows)["一致率"]
        trend.append({
            "序号": i + 1,
            "日期": window_rows[-1]["created_at"][:10],
            "一致率": acc,
        })
    return trend


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


def log_advice(
    email: str, symbol: str, price_at_advice: float, fundamental_verdict: str,
    technical_signal: str, action: str = "观望", market: str = "A", name: str = "",
    source: str = "watchlist",
) -> int:
    init_db()
    with closing(_conn()) as c:
        cur = c.execute(
            "INSERT INTO advice (email, symbol, market, name, created_at, price_at_advice, "
            "fundamental_verdict, technical_signal, action, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (email, symbol, market, name, datetime.now(timezone.utc).isoformat(), price_at_advice,
             fundamental_verdict, technical_signal, action, source),
        )
        c.commit()
        return cur.lastrowid


def get_due_for_advice_review(email: str, min_age_days: int = 7, limit: int = 20) -> list[dict]:
    """同 get_due_for_review 的逻辑，找出该回填实际价格的历史建议（只看当前用户）。"""
    init_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=min_age_days)).isoformat()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM advice WHERE email = ? AND review_price IS NULL AND created_at <= ? "
            "ORDER BY created_at ASC LIMIT ?",
            (email, cutoff, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def record_advice_review(advice_id: int, review_price: float):
    with closing(_conn()) as c:
        c.execute(
            "UPDATE advice SET review_price = ?, review_at = ? WHERE id = ?",
            (review_price, datetime.now(timezone.utc).isoformat(), advice_id),
        )
        c.commit()


def _advice_accuracy_from_rows(rows: list[dict]) -> dict:
    """跟_accuracy_from_rows同样的口径，只是判断"一致"的标准换成action：
    买入=事后应该涨，卖出=事后应该跌；持有/观望不算方向判断，不参与统计。"""
    scored = [r for r in rows if r["action"] in ("买入", "卖出")]
    if not scored:
        return {"总数": 0, "一致数": 0, "一致率": None}
    match = 0
    for r in scored:
        went_up = r["review_price"] > r["price_at_advice"]
        if (r["action"] == "买入" and went_up) or (r["action"] == "卖出" and not went_up):
            match += 1
    return {"总数": len(scored), "一致数": match, "一致率": match / len(scored) * 100}


def get_advice_accuracy(email: str) -> dict:
    """advice表版本的方向一致率统计，字段/口径跟get_accuracy_stats对齐，只是
    数据源换成advice表、方向标签换成买入/卖出。同样不代表未来表现，只是历史
    建议和事后价格的客观比对。"""
    init_db()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM advice WHERE email = ? AND review_price IS NOT NULL",
            (email,),
        ).fetchall()
    rows = [dict(r) for r in rows]

    stats = _advice_accuracy_from_rows(rows)
    stats["按市场"] = {
        m: _advice_accuracy_from_rows([r for r in rows if r.get("market", "A") == m])
        for m in sorted(set(r.get("market", "A") for r in rows))
    }
    stats["按来源"] = {
        s: _advice_accuracy_from_rows([r for r in rows if r.get("source", "watchlist") == s])
        for s in sorted(set(r.get("source", "watchlist") for r in rows))
    }
    return stats


_ADVICE_ACTION_PRIORITY = {"买入": 0, "持有": 1, "观望": 2, "卖出": 3}


def get_latest_advice(limit_per_market: int = 3) -> dict:
    """给首页"投研候选"模块用：只读最近一次 advisor.py 跑出来的结果，不现场
    重新跑（那一次要跑几分钟、几十次AI调用，公开首页每次访问都触发一遍
    既慢又烧钱）。advice 表没有显式的"批次id"，用"最近一条记录所在的那个
    自然日"当作一批——advisor.py 一天正常只跑一次，这个近似足够用；就算
    同一天手动多跑了几次，也只是同一天的记录被合并当一批，不影响正确性。

    只挑source='screen'的记录（这个表目前只有这一种来源，写死是为了以后
    万一加别的来源时不用改这里的调用方）。按action优先级(买入>持有>观望>
    卖出)排，跟advisor.py里_top_picks的逻辑保持一致——避免两处各写一套
    排序规则将来跑偏。

    返回 {"run_date": "YYYY-MM-DD"|None, "US": [...], "HK": [...]}，
    run_date为None表示还没有任何历史记录（比如cron还没跑过第一次）。
    """
    init_db()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        latest = c.execute(
            "SELECT created_at FROM advice WHERE source = 'screen' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if latest is None:
            return {"run_date": None, "US": [], "HK": []}
        run_date = latest["created_at"][:10]
        rows = c.execute(
            "SELECT * FROM advice WHERE source = 'screen' AND created_at LIKE ? ORDER BY created_at",
            (f"{run_date}%",),
        ).fetchall()
    rows = [dict(r) for r in rows]

    result = {"run_date": run_date}
    for market in ("US", "HK"):
        pool = sorted(
            (r for r in rows if r.get("market") == market),
            key=lambda r: _ADVICE_ACTION_PRIORITY.get(r["action"], 9),
        )
        result[market] = pool[:limit_per_market]
    return result


def get_watchlist_advice(email: str) -> dict:
    """给自选股列表用：每支持仓股票最新的一条AI判断(source='watchlist')，
    按symbol取最近一条——跟get_latest_advice(首页候选，全局只有一批"最近
    一次")不同，这里每个symbol各自独立找"这支股票最近一次判断"，因为自选股
    是逐支持续跟踪的，不是同一批筛选结果。返回{symbol: row}，没有判断过的
    symbol不会出现在返回的dict里，调用方用.get(symbol)处理"还没判断过"。
    """
    init_db()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            """
            SELECT * FROM advice WHERE email = ? AND source = 'watchlist'
            AND id IN (
                SELECT MAX(id) FROM advice WHERE email = ? AND source = 'watchlist' GROUP BY symbol
            )
            """,
            (email, email),
        ).fetchall()
        return {r["symbol"]: dict(r) for r in rows}


def get_search_history(email: str, limit: int = 10) -> list[dict]:
    init_db()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM search_history WHERE email = ? ORDER BY searched_at DESC LIMIT ?",
            (email, limit),
        ).fetchall()
        return [dict(r) for r in rows]
