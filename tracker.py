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


_db_initialized = False  # 进程级标志位，见init_db()末尾的说明


def init_db():
    # 这个文件里几乎每个读写函数（每次页面渲染大概率会调用好几个）开头都会
    # 调一次init_db()，而这个函数每次都要开关一次sqlite连接、对好几张表各跑
    # 一遍PRAGMA table_info做迁移检测——建表语句本身"IF NOT EXISTS"很便宜，
    # 但这些迁移检测代价不是零，一次页面渲染里被重复触发几十次纯属浪费。
    # schema在一个进程生命周期里只会变一次（就是这次init_db()真正执行的
    # 这一遍），后面的调用直接跳过就行；用一个模块级标志位做这个"只跑一次"
    # 判断，不影响多进程/多worker各自独立初始化。
    global _db_initialized
    if _db_initialized:
        return
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
        if "score" not in cols:
            # 2026-08-26新增：AI深度分析页"总结性分析"卡片的0-100综合评分
            # (analysis.py summarize_overall/extract_score)原来现算现扔，
            # 从来没被存过——排查用户反馈时发现这个分数没法回溯验证准不准，
            # 跟verdict字段（有get_accuracy_stats）不是同一回事，补上才能
            # 开始积累数据。NULL=还没来得及记（老记录/AI没按格式输出分数），
            # 不能默认成0——见advisor.py _extract_score同一条注释，0分是
            # "证据高度一致看空"这个真实结论，跟"没解析到"完全是两回事。
            c.execute("ALTER TABLE analyses ADD COLUMN score INTEGER")

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
        # 老库升级：多市场之前建的watchlist表没有 market 列，统一按A股兼容
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
                source TEXT NOT NULL DEFAULT 'position',
                review_price REAL,
                review_at TEXT
            )
            """
        )
        # 老库升级：2026-08-25加的排行榜综合得分，之前建的库没有这一列。
        _advice_cols = [r[1] for r in c.execute("PRAGMA table_info(advice)").fetchall()]
        if "score" not in _advice_cols:
            c.execute("ALTER TABLE advice ADD COLUMN score INTEGER")

        # positions：取代 watchlist 表的"持仓分析"数据模型。shares=0 表示"只
        # 关注不持仓"（详情页"关注"按钮走这个状态）。
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
                analysis_text TEXT NOT NULL DEFAULT '',
                signals_json TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # 老库升级：signals_json是后加的字段（结构化交易信号，见advisor.py
        # _parse_trade_signals），CREATE TABLE IF NOT EXISTS对已存在的老表
        # 不会补列，要显式ALTER。
        pa_cols = [r[1] for r in c.execute("PRAGMA table_info(portfolio_advice)").fetchall()]
        if "signals_json" not in pa_cols:
            c.execute("ALTER TABLE portfolio_advice ADD COLUMN signals_json TEXT NOT NULL DEFAULT ''")

        # user_settings：目前只有一个字段(最大资金投入上限，折人民币)，用户
        # 明确要求"AI要知道我们总共有多少钱，不能盲目加仓"——advise_portfolio
        # 只看得到已经买了多少，看不到用户自己设的资金天花板，这里补上让AI
        # 能算"还剩多少额度"。email是主键，一人一条，不用像positions那样
        # UNIQUE(email,symbol)按标的区分。
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                email TEXT PRIMARY KEY,
                max_capital_cny REAL,
                updated_at TEXT NOT NULL
            )
            """
        )

        # 从watchlist表一次性迁移进positions，shares/cost_total都是0(纯关注)。
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
        # 2026-08-21曾经在这里加过一条"UPDATE advice SET source='position'
        # WHERE source='watchlist'"，清理"持仓分析"上线前advice表里的旧
        # source标签——2026-08-28给"推荐股排行榜"重新用了'watchlist'这个
        # 值（跟这张watchlist表同名但完全是另一回事），那条一次性迁移语句
        # 因为每次进程启动都无条件重跑，把新功能刚写进去的数据当场吃掉，
        # 排查了很久才找到。删掉了——以后不要再对advice.source做基于字符串
        # 值的无条件UPDATE，新老含义容易撞名。
        c.commit()
    _db_initialized = True


_MARKET_CURRENCY = {"A": "CNY", "HK": "HKD", "US": "USD"}


def add_watch_only(email: str, symbol: str, name: str, market: str = "A") -> bool:
    """在positions里插入一行shares=0(只关注不持仓)的记录——用户点详情页"关注"
    按钮、或添加持仓弹窗里不填股数时走这条路径。真正记持仓用upsert_position。"""
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


def is_position_tracked(email: str, symbol: str) -> bool:
    init_db()
    with closing(_conn()) as c:
        row = c.execute(
            "SELECT 1 FROM positions WHERE email = ? AND symbol = ?", (email, symbol)
        ).fetchone()
        return row is not None


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
    # 这个文件里几乎每个读写函数开头都调了init_db()，唯独这个漏了——如果
    # 进程刚启动、这是第一个碰数据库的调用（比如某个独立脚本只导入tracker
    # 就直接调用log_position_lot），position_lots表还没建过，会报
    # "no such table"。
    init_db()
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


def log_portfolio_advice(
    email: str, total_value_cny: float | None, holdings_json: str, analysis_text: str,
    signals_json: str = "",
) -> int:
    init_db()
    with closing(_conn()) as c:
        cur = c.execute(
            "INSERT INTO portfolio_advice (email, created_at, total_value_cny, holdings_json, analysis_text, signals_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (email, datetime.now(timezone.utc).isoformat(), total_value_cny, holdings_json, analysis_text, signals_json),
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


def set_max_capital(email: str, amount: float | None):
    """amount=None表示用户清空了这个设置(不想设上限)，跟"设成0"是两种状态——
    0是"明确不打算再投钱"，None是"没设置/不想告诉AI这个信息"，advise_portfolio
    要能区分这两种，不能把None当0处理。"""
    init_db()
    with closing(_conn()) as c:
        c.execute(
            "INSERT INTO user_settings (email, max_capital_cny, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET max_capital_cny = excluded.max_capital_cny, updated_at = excluded.updated_at",
            (email, amount, datetime.now(timezone.utc).isoformat()),
        )
        c.commit()


def get_max_capital(email: str) -> float | None:
    init_db()
    with closing(_conn()) as c:
        row = c.execute("SELECT max_capital_cny FROM user_settings WHERE email = ?", (email,)).fetchone()
        return row[0] if row else None


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


def record_overall_score(email: str, symbol: str, market: str, score: int):
    """给"总结性分析"那个0-100综合评分补记到最近一条analyses记录上——不是新建
    一行，是UPDATE刚刚log_analysis插入的那条（同一次页面渲染里，cross_validate
    先跑完调用log_analysis，summarize_overall后跑完再调这个函数，两者间隔
    只有一次页面渲染的时间，"最近一条"在实际使用场景下不会指错行）。

    SQLite的UPDATE不支持直接ORDER BY+LIMIT，用子查询先选出最新那条id再更新，
    这是标准写法（不是绕弯，就是SQLite的正常用法）。
    """
    with closing(_conn()) as c:
        c.execute(
            "UPDATE analyses SET score = ? WHERE id = ("
            "  SELECT id FROM analyses WHERE email = ? AND symbol = ? AND market = ? "
            "  ORDER BY created_at DESC LIMIT 1"
            ")",
            (score, email, symbol, market),
        )
        c.commit()


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


def get_daily_accuracy(email: str, days: int = 91) -> list[dict]:
    """按天聚合的方向一致率——给"回看"页的日历热力图用。口径跟
    get_accuracy_stats/get_accuracy_trend保持一致（只算review_price不为空、
    verdict不是"中性"的记录），区别是这里按created_at的日期分组，每天算
    一个独立的一致率，而不是总体或滑动窗口。

    91天≈13周，选这个长度是因为日历热力图按"7天一列"排布，13周正好是
    常见的季度回看窗口，比GitHub贡献图默认的52周更贴合这个工具本身的
    使用频率（一个人一天顶多分析几只股票，不是每天几十次提交）。
    """
    init_db()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM analyses WHERE email = ? AND review_price IS NOT NULL "
            "AND verdict != '中性' AND created_at >= ? ORDER BY created_at ASC",
            (email, since),
        ).fetchall()
    rows = [dict(r) for r in rows]

    by_day: dict[str, list[dict]] = {}
    for r in rows:
        day = r["created_at"][:10]
        by_day.setdefault(day, []).append(r)

    return [
        {"日期": day, **_accuracy_from_rows(day_rows)}
        for day, day_rows in sorted(by_day.items())
    ]


def add_search_history(email: str, query: str, market: str = "A"):
    """记一笔"添加持仓"时搜过的关键词——给搜索弹窗里的历史记录用，方便
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
    source: str = "position", score: int | None = None,
) -> int:
    init_db()
    with closing(_conn()) as c:
        cur = c.execute(
            "INSERT INTO advice (email, symbol, market, name, created_at, price_at_advice, "
            "fundamental_verdict, technical_signal, action, source, score) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (email, symbol, market, name, datetime.now(timezone.utc).isoformat(), price_at_advice,
             fundamental_verdict, technical_signal, action, source, score),
        )
        c.commit()
        return cur.lastrowid


def get_due_for_advice_review(
    email: str, min_age_days: int = 7, limit: int = 20, source: str | None = None,
) -> list[dict]:
    """同 get_due_for_review 的逻辑，找出该回填实际价格的历史建议（只看当前用户）。

    source: 不同来源的建议该等多久才回填价格，标准不一样——position/screen是
    "这次判断能不能扛得住一段时间"，默认7天；watchlist（固定观察池，每天都
    重新判断一遍）是"次日核对"，调用方会传source='watchlist'、min_age_days=1
    单独查，不跟另外两个来源混在一次查询里（不然没法对同一批due记录分别用
    不同的回填时间窗）。传None时不按source过滤，是老调用点的行为，不动。
    """
    init_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=min_age_days)).isoformat()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        if source:
            rows = c.execute(
                "SELECT * FROM advice WHERE email = ? AND source = ? AND review_price IS NULL "
                "AND created_at <= ? ORDER BY created_at ASC LIMIT ?",
                (email, source, cutoff, limit),
            ).fetchall()
        else:
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


_SCORE_BANDS = [(90, 100, "90-100"), (70, 89, "70-89"), (50, 69, "50-69"), (30, 49, "30-49"), (0, 29, "0-29")]


def get_score_band_backtest(source: str = "screen", min_sample: int = 5) -> dict:
    """按综合得分分档统计事后真实收益——参考开源项目TradingAgents(TauricResearch)
    v0.2.4"结果驱动复盘日志"的思路(2026-08-26)：排行榜的0-100分打分体系上线以来
    从未被拿去跟事后价格核对过，是这套系统当时最大的可信度缺口(排行榜本身分数
    是否真的有预测力，完全没验证过)。这里不是自己说分数准不准，是把
    record_advice_review回填进来的真实事后价格摆出来，按分数区间算平均涨跌幅和
    上涨比例，让"分数越高是不是真的表现越好"这件事可以被客观检验。

    min_sample：单个分数区间样本数低于这个值时，只报样本数、不算平均收益/胜率
    ——小样本的极端值很容易被误读成"规律"，与其给一个可能带偏差的数字，不如
    如实说"数据还不够"，这是这个项目一贯"不编数字"的原则在这里的延伸。
    """
    init_db()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT score, price_at_advice, review_price FROM advice WHERE source = ? "
            "AND score IS NOT NULL AND review_price IS NOT NULL "
            "AND price_at_advice IS NOT NULL AND price_at_advice > 0",
            (source,),
        ).fetchall()
    rows = [dict(r) for r in rows]

    bands = []
    for lo, hi, label in _SCORE_BANDS:
        band_rows = [r for r in rows if lo <= r["score"] <= hi]
        n = len(band_rows)
        if n == 0:
            bands.append({"band": label, "count": 0, "avg_return_pct": None, "win_rate_pct": None})
            continue
        returns = [(r["review_price"] - r["price_at_advice"]) / r["price_at_advice"] * 100 for r in band_rows]
        entry = {"band": label, "count": n, "avg_return_pct": None, "win_rate_pct": None}
        if n >= min_sample:
            entry["avg_return_pct"] = round(sum(returns) / n, 2)
            entry["win_rate_pct"] = round(sum(1 for x in returns if x > 0) / n * 100, 1)
        bands.append(entry)
    return {"total_reviewed": len(rows), "min_sample": min_sample, "bands": bands}


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
        s: _advice_accuracy_from_rows([r for r in rows if r.get("source", "position") == s])
        for s in sorted(set(r.get("source", "position") for r in rows))
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

    返回 {"run_date": "YYYY-MM-DD"|None, "US": [...], "HK": [...], "A": [...]}，
    run_date为None表示还没有任何历史记录（比如cron还没跑过第一次）。
    """
    init_db()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        latest = c.execute(
            "SELECT created_at FROM advice WHERE source = 'screen' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if latest is None:
            return {"run_date": None, "US": [], "HK": [], "A": []}
        run_date = latest["created_at"][:10]
        rows = c.execute(
            "SELECT * FROM advice WHERE source = 'screen' AND created_at LIKE ? ORDER BY created_at",
            (f"{run_date}%",),
        ).fetchall()
    rows = [dict(r) for r in rows]

    result = {"run_date": run_date}
    for market in ("US", "HK", "A"):
        pool = sorted(
            (r for r in rows if r.get("market") == market),
            key=lambda r: _ADVICE_ACTION_PRIORITY.get(r["action"], 9),
        )
        result[market] = pool[:limit_per_market]
    return result


def get_latest_leaderboard(limit: int = 10, source: str = "screen") -> dict:
    """2026-08-25新增：三个市场混排的综合得分排行榜，取代"每个市场固定
    前3"的老逻辑——用户明确要求"好的就上，不好不出现也没事"，不要求每个
    市场凑数量。取同一批（最近一次跑advisor.py那天）里score不为空的记录，
    按score降序排，跨市场统一取前limit条。

    score为空的记录（AI没按格式输出综合得分，或者是老数据这一列本来就是
    NULL）不参与排行榜——不能默认成0分，那等于把"没解析到分数"当成"最差
    评级"，是两回事（这条判断本身可能是好的，只是分数解析失败）。这类记录
    不会出现在榜单里，但原始判断文本还在数据库里，不是被丢弃了。

    source：2026-08-28新增参数——首页"推荐股排行榜"改用固定观察池后，
    source='watchlist'跟原来的'screen'（全市场扫描，仍然喂私人微信简报）
    是两套独立的每日批次，不能混在一起按score排——参数化而不是复制一份
    函数，两边的查询逻辑完全一样，只是source不同。

    同一支股票当天可能有不止一条记录（观察池每天固定但热度榜会变，同一支
    股票理论上不会同一次运行里出现两次，但如果cron当天意外重跑过、或者
    调试时手动多跑了几次，同一天会攒出好几条不同分数的记录）——之前这里
    没去重，直接按分数排，会导致同一支股票占了排行榜里好几个名次（用户
    截图发现"小米集团"同时出现在#1和#3）。改成先按symbol取当天最新一条
    （MAX(id)，id自增等价于按时间取最新），再排分数，跟get_position_advice
    "每个symbol取最近一条"是同一个模式。
    """
    init_db()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        latest = c.execute(
            "SELECT created_at FROM advice WHERE source = ? ORDER BY created_at DESC LIMIT 1", (source,),
        ).fetchone()
        if latest is None:
            return {"run_date": None, "leaderboard": []}
        run_date = latest["created_at"][:10]
        rows = c.execute(
            """
            SELECT * FROM advice WHERE source = ? AND created_at LIKE ? AND score IS NOT NULL
            AND id IN (
                SELECT MAX(id) FROM advice WHERE source = ? AND created_at LIKE ? AND score IS NOT NULL
                GROUP BY symbol
            )
            ORDER BY score DESC LIMIT ?
            """,
            (source, f"{run_date}%", source, f"{run_date}%", limit),
        ).fetchall()
    return {"run_date": run_date, "leaderboard": [dict(r) for r in rows]}


def get_position_advice(email: str) -> dict:
    """给持仓列表用：每支持仓股票最新的一条AI判断(source='position')，
    按symbol取最近一条——跟get_latest_advice(首页候选，全局只有一批"最近
    一次")不同，这里每个symbol各自独立找"这支股票最近一次判断"，因为持仓
    是逐支持续跟踪的，不是同一批筛选结果。返回{symbol: row}，没有判断过的
    symbol不会出现在返回的dict里，调用方用.get(symbol)处理"还没判断过"。
    """
    init_db()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            """
            SELECT * FROM advice WHERE email = ? AND source = 'position'
            AND id IN (
                SELECT MAX(id) FROM advice WHERE email = ? AND source = 'position' GROUP BY symbol
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
