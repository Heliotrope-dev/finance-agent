"""追踪层 —— 记录每次分析发生时的价格，方便日后回看走势对照。

这里存了AI输出里解析出的"方向倾向"（偏多/偏空/中性）跟事后价格实际涨跌方向做
比对，算一个"方向一致率"。用户明确要这个功能，知道风险（可能被解读成"AI荐股
胜率"）后仍然选择要，所以做了——但界面上要清楚标注这不是投资建议、不保证未来
表现，只是历史记录的客观统计，避免误导。
"""

import json
import re
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

        # 老库升级：2026-08-29——Fable 5复核指出，四个维度打分（"维度打分：
        # 基本面X/40 · 价格位置X/30 · 技术面X/15 · 数据确定性X/15"）只活在
        # fundamental_verdict这段自由文本里，从没被结构化提取过，导致
        # get_score_band_backtest永远只能回测"综合得分"这一个黑箱数字，
        # 没法回答"到底是哪个维度真的有预测力"。补上四个独立列，log_advice
        # 写入时会自动从fundamental_verdict里正则提取（extract_score_
        # breakdown），解析不出来就存NULL（不强行凑数字）。这里只补列，
        # 历史数据的回填是独立跑的一次性脚本，不在这个migration里做
        # （init_db每次进程启动都会跑一遍，回填这种一次性操作不该跟常规
        # 迁移逻辑绑在一起，容易在未来忘记这里还留着一次性代码）。
        if "score_fundamental" not in _advice_cols:
            c.execute("ALTER TABLE advice ADD COLUMN score_fundamental INTEGER")
        if "score_price_position" not in _advice_cols:
            c.execute("ALTER TABLE advice ADD COLUMN score_price_position INTEGER")
        if "score_technical" not in _advice_cols:
            c.execute("ALTER TABLE advice ADD COLUMN score_technical INTEGER")
        if "score_data_certainty" not in _advice_cols:
            c.execute("ALTER TABLE advice ADD COLUMN score_data_certainty INTEGER")
        # 2026-09-05打分从四维扩到六维，补两列。加在这里而不是新写一个
        # migration函数，是为了让"六个维度"在代码里始终是并排的六行——
        # 下次再加维度时不会漏掉某一处。
        if "score_chips" not in _advice_cols:
            c.execute("ALTER TABLE advice ADD COLUMN score_chips INTEGER")
        if "score_analyst" not in _advice_cols:
            c.execute("ALTER TABLE advice ADD COLUMN score_analyst INTEGER")

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
        # ai_sim_trading：2026-09-01用户明确要求"让内置AI模拟买卖"——开关默认
        # 关闭(0)，用户自己在持仓页打开后，advisor.py每天17:30生成组合交易
        # 信号时才会真的去调用sim_trader.execute_simulated_trades往富途的
        # SIMULATE模拟盘下单。这是对_parse_trade_signals文档字符串里那句
        # "不接真实下单"的一次明确的、用户主动要求的方向调整——只对接富途
        # 自己的模拟盘（不涉及真实资金/不需要交易解锁密码，实测过SIMULATE
        # 环境下单不需要unlock_trade），不是接真实交易。
        us_cols = [r[1] for r in c.execute("PRAGMA table_info(user_settings)").fetchall()]
        if "ai_sim_trading" not in us_cols:
            c.execute("ALTER TABLE user_settings ADD COLUMN ai_sim_trading INTEGER NOT NULL DEFAULT 0")

        # sim_virtual_cash_hkd：AI模拟盘自主决策(sim_agent.py)的虚拟现金
        # 余额——2026-09-01用户要求"总资金十万港币"，但富途模拟账户本金
        # 没法改（客服说"理论无上限"），只能在这个agent自己的逻辑里另开
        # 一本虚拟账（不是走真实的富途现金余额）。NULL表示还没初始化，
        # 第一次运行时设成_VIRTUAL_BUDGET_HKD；之后每次买卖成功，
        # sim_agent.py按估算金额扣减/增加这个字段，跟持仓当前市值加总
        # 就是"虚拟净值"，用来算真正反映"如果只给AI十万港币"的收益率
        # ——不能直接用富途账户真实总资产，那里面绝大部分是AI碰不到的
        # 闲置资金，混进去会让收益率完全失真。
        if "sim_virtual_cash_hkd" not in us_cols:
            c.execute("ALTER TABLE user_settings ADD COLUMN sim_virtual_cash_hkd REAL")

        # sim_agent_enabled：AI模拟炒股页面"AI自主模拟交易"开关——2026-09-01
        # 真实故障纠偏：这个开关之前跟ai_sim_trading(上面那个，控制的是
        # advisor.py每天17:30那条完全不同的信号)混用同一个字段，导致用户
        # 在"AI模拟炒股"页关掉开关其实什么都没关掉——sim_agent.py的15分钟
        # 自主决策循环根本不看这个字段，只要openclaw那个cron是enabled就会
        # 一直跑，而ai_sim_trading当时还是True，advisor.py那条legacy流程
        # 还在悄悄往同一个富途SIMULATE账户下单，两套系统互不知情地共用
        # 同一个账户，这也是重置时总冒出"来源不明的遗留持仓"的原因之一。
        # 新开一个独立字段，sim_agent.py的run_cycle会真的检查它；默认1
        # (开启)，跟"目前一直在跑"的既成事实保持一致，不会因为这次修复
        # 突然把正在运行的agent停掉。
        if "sim_agent_enabled" not in us_cols:
            c.execute("ALTER TABLE user_settings ADD COLUMN sim_agent_enabled INTEGER NOT NULL DEFAULT 1")
        # closure_notice_date：休市公告最后一次弹出的日期（2026-09-05新增）。
        # 这个标记必须落库，不能只放 session_state：项目里的卡片跳转全是
        # <a href="?open_symbol=..."> 的整页导航，每跳一次就是一个新的
        # Streamlit 会话，session_state 跟着清空，弹窗于是每点一只股票就重来
        # 一遍——比不提醒还烦人。
        if "closure_notice_date" not in us_cols:
            c.execute("ALTER TABLE user_settings ADD COLUMN closure_notice_date TEXT NOT NULL DEFAULT ''")

        # simulated_orders：AI模拟盘每次自动下单的执行记录——富途自己的订单
        # 历史会被清理/查询接口只能看近期，这里单独留一份，让"回看"页能展示
        # 完整的历史执行记录，不依赖富途那边保留多久。
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS simulated_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                market TEXT NOT NULL,
                action TEXT NOT NULL,
                shares_signal REAL NOT NULL,
                shares_ordered REAL NOT NULL DEFAULT 0,
                order_id TEXT NOT NULL DEFAULT '',
                acc_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )

        # notified_at：这笔成交有没有推送到微信（2026-09-04新增）。原来的
        # "实时买卖提醒"是读 data/last_sim_agent_run.log 判断要不要发微信，
        # 而那个文件每轮都被覆盖：提醒任务只要错过一轮（网关重启、AI调用
        # 失败——千问额度耗尽那几天就连续失败了25轮），那一轮的成交通知就
        # 永久丢了，没有任何补发机制。用户明确要求"每笔买入卖出都要通知我"，
        # 靠一个会被覆盖的临时文件做不到。改成在这张持久表上打标记：没打上
        # 标记的成功成交，下一轮会继续被捞出来重发，漏一轮只是晚到，不会丢。
        _so_cols = [r[1] for r in c.execute("PRAGMA table_info(simulated_orders)").fetchall()]
        if "notified_at" not in _so_cols:
            c.execute("ALTER TABLE simulated_orders ADD COLUMN notified_at TEXT NOT NULL DEFAULT ''")

        # sim_agent_runs：AI模拟盘"自主决策"每次运行的完整记录——2026-09-01
        # 用户要求"全自动、自己学习试错"，这是每15分钟一次的独立决策循环
        # (sim_agent.py，只在港股/美股开盘时跑，A股不参与)，跟每天17:30那次
        # 组合分析(advise_portfolio)不是同一回事，各自留自己的执行记录：
        # 这张表记的是"这次AI看到了什么、想了什么、决定怎么做"的完整上下文，
        # 不只是最终下了哪些单（simulated_orders已经记了下单结果，这张表
        # 补的是决策过程本身，让"学习试错"这件事有据可查，不是黑箱）。
        # assets_hkd_before用于在下一轮决策时喂给AI"你上次操作后资产变化"
        # 这个反馈，是"学习"这个说法在LLM agent场景下能落地的方式——不是
        # 训练模型权重，是把历史战绩摘要写进prompt当参考。用港币而不是人民币
        # 记账，因为用户把这个自主agent的起始本金定为"十万港币"（港股/美股
        # 两个独立SIMULATE账户，折算成港币统一核算）。
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS sim_agent_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                run_at TEXT NOT NULL,
                open_markets TEXT NOT NULL DEFAULT '',
                assets_hkd_before REAL,
                reasoning_text TEXT NOT NULL DEFAULT '',
                signals_json TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # 老库升级：这张表刚上线时字段名是assets_cny_before(人民币计价)，
        # 后来用户改主意要港币计价，字段名同步改成assets_hkd_before——这张
        # 表还没积累真实要保留的数据，直接RENAME COLUMN，不用像其它老表那样
        # 走"新增列+回填"的兼容路径。
        sar_cols = [r[1] for r in c.execute("PRAGMA table_info(sim_agent_runs)").fetchall()]
        if "assets_cny_before" in sar_cols and "assets_hkd_before" not in sar_cols:
            c.execute("ALTER TABLE sim_agent_runs RENAME COLUMN assets_cny_before TO assets_hkd_before")

        # sim_equity_snapshots：2026-09-01用户反馈"15分钟一格太稀疏，走势图
        # 看着像死了"——sim_agent_runs是跟着AI决策节奏走的(15分钟一次，且
        # 只在决策时才记)，遇到工商银行/中国神华这种低波动防御型持仓，连续
        # 几次数字精确不变，图表就会显得很平。这张表职责单一：不涉及AI
        # 决策，只是每隔几分钟单纯查一次市值+虚拟现金写一条快照，专门喂给
        # 走势图用，跟"AI每次决策记录"那个列表（继续用sim_agent_runs，
        # 15分钟一次）解耦——决策历史和资产曲线是两回事，不用绑在同一个
        # 采样频率上。
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS sim_equity_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                snapshot_at TEXT NOT NULL,
                holdings_value_hkd REAL NOT NULL,
                virtual_cash_hkd REAL NOT NULL,
                net_value_hkd REAL NOT NULL
            )
            """
        )

        # sim_agent_lessons：AI模拟盘的长期经验教训——2026-09-02用户明确要求
        # "他也要学习，想让他变强大"。sim_agent.py每次决策时能看到的历史只有
        # 最近_HISTORY_CONTEXT_SIZE(5)次的短期战绩，跨天/跨周的规律性教训看
        # 不到。这张表是每天一条、由sim_agent_report.py用真实数据机械算出来
        # 的日度复盘(胜率/盈亏/当天最典型的一笔对/错决策)，不是AI临时编的，
        # sim_agent.py后续会把最近几条也塞进prompt，让"学习"这件事有一个
        # 比短期战绩更长的记忆窗口。
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS sim_agent_lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                created_at TEXT NOT NULL,
                lesson_text TEXT NOT NULL
            )
            """
        )

        # macro_briefs：宏观重大议题（美联储议息、CPI、非农这类）的AI解读。
        # 2026-09-04新增。做成"离线生成、页面只读"是刻意的：这类分析要把
        # 多条资讯汇总起来让AI通读一遍，一次要几十秒，如果放在页面渲染时现算，
        # 每个访客每次打开首页都要等一次、烧一次AI调用，而这份内容一天之内
        # 对所有人都是同一份。跟 advisor.py 那条"cron跑完写库、页面只读最近
        # 一次结果"的链路是同一个模式。
        # topic 是议题标识（如"fed"/"cpi"/"payrolls"），同一个议题保留多条
        # 历史，页面只取最新一条。
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS macro_briefs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL,
                title TEXT NOT NULL,
                brief_text TEXT NOT NULL,
                sources_json TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_macro_topic ON macro_briefs (topic, created_at DESC)")

        # ipo_briefs：港股新股的申购参考（2026-09-05新增）。跟 macro_briefs
        # 同一个模式——预先算好落库，首页只读，不在渲染路径里跑AI。
        # 每只新股每次重算写一条新记录，读的时候按 symbol 取最新那条，保留
        # 历史是为了事后能回看"当时是怎么判断的"，跟实际上市表现对照。
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS ipo_briefs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                list_date TEXT NOT NULL DEFAULT '',
                apply_end TEXT NOT NULL DEFAULT '',
                brief_text TEXT NOT NULL,
                facts_json TEXT NOT NULL DEFAULT '',
                sources_json TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_ipo_symbol ON ipo_briefs (symbol, created_at DESC)")

        # ipo_performance：近期已上市新股的首日表现统计（2026-09-05新增）。
        # 算一次要为每只新股各发一次历史K线请求，几十次，富途历史K线有每日
        # 额度限制，不能放在页面渲染路径里。定时算好存这里，页面只读最新一行。
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS ipo_performance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        # chart_json：这条议题配套的结构化数据，页面拿它画图（比如美联储那条
        # 存的是CME FedWatch的利率概率表）。存下来而不是渲染时现查，是因为
        # 首页每个访客都要画一次，现查等于每次打开都打一次Futu接口。
        # 老库升级：这一列是建表之后才加的，用同样的"查一下有没有再ALTER"
        # 的写法，跟这个文件里其它几处老库升级保持一致。
        mb_cols = {r[1] for r in c.execute("PRAGMA table_info(macro_briefs)")}
        if "chart_json" not in mb_cols:
            c.execute("ALTER TABLE macro_briefs ADD COLUMN chart_json TEXT NOT NULL DEFAULT ''")

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


def set_ai_sim_trading(email: str, enabled: bool):
    init_db()
    with closing(_conn()) as c:
        c.execute(
            "INSERT INTO user_settings (email, ai_sim_trading, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET ai_sim_trading = excluded.ai_sim_trading, updated_at = excluded.updated_at",
            (email, int(enabled), datetime.now(timezone.utc).isoformat()),
        )
        c.commit()


def get_ai_sim_trading(email: str) -> bool:
    init_db()
    with closing(_conn()) as c:
        row = c.execute("SELECT ai_sim_trading FROM user_settings WHERE email = ?", (email,)).fetchone()
        return bool(row[0]) if row else False


def set_sim_agent_enabled(email: str, enabled: bool):
    """AI模拟炒股页"AI自主模拟交易"开关的真正落地——跟上面ai_sim_trading
    是两个独立字段，见user_settings建表那段注释，不要合并成一个。"""
    init_db()
    with closing(_conn()) as c:
        c.execute(
            "INSERT INTO user_settings (email, sim_agent_enabled, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET sim_agent_enabled = excluded.sim_agent_enabled, updated_at = excluded.updated_at",
            (email, int(enabled), datetime.now(timezone.utc).isoformat()),
        )
        c.commit()


def get_sim_agent_enabled(email: str) -> bool:
    init_db()
    with closing(_conn()) as c:
        row = c.execute("SELECT sim_agent_enabled FROM user_settings WHERE email = ?", (email,)).fetchone()
        return bool(row[0]) if row else True


def get_sim_virtual_cash(email: str) -> float | None:
    """None表示还没初始化过（第一次跑sim_agent.py之前）。"""
    init_db()
    with closing(_conn()) as c:
        row = c.execute("SELECT sim_virtual_cash_hkd FROM user_settings WHERE email = ?", (email,)).fetchone()
        return row[0] if row else None


def set_sim_virtual_cash(email: str, amount: float):
    init_db()
    with closing(_conn()) as c:
        c.execute(
            "INSERT INTO user_settings (email, sim_virtual_cash_hkd, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET sim_virtual_cash_hkd = excluded.sim_virtual_cash_hkd, updated_at = excluded.updated_at",
            (email, amount, datetime.now(timezone.utc).isoformat()),
        )
        c.commit()


def log_simulated_order(
    email: str, symbol: str, name: str, market: str, action: str,
    shares_signal: float, shares_ordered: float, order_id: str, acc_id: str,
    status: str, note: str = "",
) -> None:
    init_db()
    with closing(_conn()) as c:
        c.execute(
            "INSERT INTO simulated_orders "
            "(email, symbol, name, market, action, shares_signal, shares_ordered, order_id, acc_id, status, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (email, symbol, name, market, action, shares_signal, shares_ordered, order_id, acc_id, status, note,
             datetime.now(timezone.utc).isoformat()),
        )
        # 用户明确要求"完整下单记录"最多留50笔，过了自动删除——不能让这张表
        # 无限长下去（agent每15分钟可能下好几单，攒得很快），每次插入后顺手
        # 清掉这个email超出50条的旧记录。
        c.execute(
            "DELETE FROM simulated_orders WHERE email = ? AND id NOT IN "
            "(SELECT id FROM simulated_orders WHERE email = ? ORDER BY created_at DESC LIMIT 50)",
            (email, email),
        )
        c.commit()


def get_simulated_orders(email: str, limit: int = 50) -> list[dict]:
    init_db()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM simulated_orders WHERE email = ? ORDER BY created_at DESC LIMIT ?", (email, limit),
        ).fetchall()
        return [dict(r) for r in rows]



def get_closure_notice_count(email: str, day: str) -> int:
    """今天已经弹过几次休市公告。

    存的格式是 "YYYY-MM-DD:次数"。用户要求"每天跳三次就行，不需要每次点进去
    就跳"——所以不是布尔的"弹过没有"，要记次数；日期不匹配（换天了）就归零。
    塞在同一个字段里而不是再加一列：这是个纯提示性的计数，为它单独加列和迁移
    不值当。
    """
    init_db()
    with closing(_conn()) as c:
        row = c.execute(
            "SELECT closure_notice_date FROM user_settings WHERE email = ?", (email,)
        ).fetchone()
    raw = (row[0] if row and row[0] else "")
    if not raw.startswith(day):
        return 0
    try:
        return int(raw.split(":", 1)[1])
    except Exception:
        # 兼容旧格式（只存了日期、没有次数）：当成已经弹过一次
        return 1


def bump_closure_notice_count(email: str, day: str) -> None:
    n = get_closure_notice_count(email, day) + 1
    init_db()
    with closing(_conn()) as c:
        c.execute(
            "INSERT INTO user_settings (email, closure_notice_date) VALUES (?, ?) "
            "ON CONFLICT(email) DO UPDATE SET closure_notice_date = excluded.closure_notice_date",
            (email, f"{day}:{n}"),
        )
        c.commit()




def log_ipo_performance(payload_json: str) -> None:
    init_db()
    with closing(_conn()) as c:
        c.execute("INSERT INTO ipo_performance (payload_json, created_at) VALUES (?, ?)",
                  (payload_json, datetime.now(timezone.utc).isoformat()))
        # 只留最近5次，这是个可重算的派生统计，没必要无限堆历史
        c.execute("DELETE FROM ipo_performance WHERE id NOT IN "
                  "(SELECT id FROM ipo_performance ORDER BY id DESC LIMIT 5)")
        c.commit()


def get_latest_ipo_performance() -> dict:
    init_db()
    with closing(_conn()) as c:
        row = c.execute("SELECT payload_json, created_at FROM ipo_performance "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return {}
    try:
        d = json.loads(row[0])
        d["created_at"] = row[1]
        return d
    except Exception:
        return {}


def log_ipo_brief(symbol: str, name: str, list_date: str, apply_end: str,
                  brief_text: str, facts_json: str = "", sources_json: str = "") -> None:
    init_db()
    with closing(_conn()) as c:
        c.execute(
            "INSERT INTO ipo_briefs (symbol, name, list_date, apply_end, brief_text, "
            "facts_json, sources_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (symbol, name, list_date, apply_end, brief_text, facts_json, sources_json,
             datetime.now(timezone.utc).isoformat()),
        )
        c.commit()


def get_latest_ipo_briefs(limit: int = 6) -> list[dict]:
    """每只新股最新的一条，按上市日期正序——先上市的排前面，因为申购截止
    也更早，用户要先处理那一只。已经上市的不再显示（打新窗口已经过了）。"""
    init_db()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM ipo_briefs WHERE id IN "
            "(SELECT MAX(id) FROM ipo_briefs GROUP BY symbol) "
            "ORDER BY list_date ASC LIMIT ?", (limit * 3,),
        ).fetchall()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = [dict(r) for r in rows if (r["list_date"] or "9999") >= today]
    return out[:limit]


def get_unnotified_orders(email: str, limit: int = 20) -> list[dict]:
    """捞出还没推送到微信的成功成交。

    只认 status='成功'——跳过/失败的信号没有真实动作，不值得打扰用户。
    按时间正序返回，让推送顺序跟成交顺序一致。

    这张表滚动只保留50条，理论上连续漏推超过50笔会丢最早的；实际每轮
    最多下几单、提醒任务几分钟一次，够不到这个边界。
    """
    init_db()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM simulated_orders WHERE email = ? AND status = '成功' "
            "AND (notified_at IS NULL OR notified_at = '') ORDER BY created_at ASC LIMIT ?",
            (email, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_orders_notified(order_ids: list[int]) -> int:
    """把这些成交标记成已推送。必须在微信发送成功之后才调用。

    发送成功但标记失败会导致重复推送一次——这个方向的错误可以接受；
    反过来（标记了却没发出去）才是用户明确不想要的"漏掉"。
    """
    ids = [int(i) for i in order_ids if str(i).strip()]
    if not ids:
        return 0
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    with closing(_conn()) as c:
        cur = c.execute(
            "UPDATE simulated_orders SET notified_at = ? WHERE id IN (%s) "
            "AND (notified_at IS NULL OR notified_at = '')" % ",".join("?" * len(ids)),
            [now] + ids,
        )
        c.commit()
        return cur.rowcount


def log_sim_agent_run(
    email: str, open_markets: list[str], assets_hkd_before: float | None,
    reasoning_text: str, signals_json: str, status: str, note: str = "",
) -> int:
    init_db()
    with closing(_conn()) as c:
        cur = c.execute(
            "INSERT INTO sim_agent_runs "
            "(email, run_at, open_markets, assets_hkd_before, reasoning_text, signals_json, status, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (email, datetime.now(timezone.utc).isoformat(), json.dumps(open_markets, ensure_ascii=False),
             assets_hkd_before, reasoning_text, signals_json, status, note),
        )
        # 同样的道理——"AI每次决策记录"最多留30条，过了自动删除。这张表比
        # simulated_orders涨得还快（开盘每15分钟必有一条，不管有没有真的下单，
        # 含"跳过：当前没有市场开盘"这种），不清理会一直膨胀下去。
        c.execute(
            "DELETE FROM sim_agent_runs WHERE email = ? AND id NOT IN "
            "(SELECT id FROM sim_agent_runs WHERE email = ? ORDER BY run_at DESC LIMIT 30)",
            (email, email),
        )
        c.commit()
        return cur.lastrowid


def log_equity_snapshot(email: str, holdings_value_hkd: float, virtual_cash_hkd: float) -> int:
    init_db()
    with closing(_conn()) as c:
        cur = c.execute(
            "INSERT INTO sim_equity_snapshots (email, snapshot_at, holdings_value_hkd, virtual_cash_hkd, net_value_hkd) "
            "VALUES (?, ?, ?, ?, ?)",
            (email, datetime.now(timezone.utc).isoformat(), holdings_value_hkd, virtual_cash_hkd,
             holdings_value_hkd + virtual_cash_hkd),
        )
        c.commit()
        return cur.lastrowid


def get_equity_snapshots(email: str, limit: int = 500) -> list[dict]:
    init_db()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM sim_equity_snapshots WHERE email = ? ORDER BY snapshot_at DESC LIMIT ?", (email, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def log_sim_agent_lesson(email: str, lesson_text: str) -> int:
    init_db()
    with closing(_conn()) as c:
        cur = c.execute(
            "INSERT INTO sim_agent_lessons (email, created_at, lesson_text) VALUES (?, ?, ?)",
            (email, datetime.now(timezone.utc).isoformat(), lesson_text),
        )
        # 只留最近30条——日度复盘攒太久意义不大(市场状态会变，半年前的
        # "教训"未必还适用)，超过的旧记录自动清掉，跟其它sim_agent相关表
        # 同一个道理。
        c.execute(
            "DELETE FROM sim_agent_lessons WHERE email = ? AND id NOT IN "
            "(SELECT id FROM sim_agent_lessons WHERE email = ? ORDER BY created_at DESC LIMIT 30)",
            (email, email),
        )
        c.commit()
        return cur.lastrowid


def get_sim_agent_lessons(email: str, limit: int = 7) -> list[dict]:
    init_db()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM sim_agent_lessons WHERE email = ? ORDER BY created_at DESC LIMIT ?", (email, limit),
        ).fetchall()
        return [dict(r) for r in rows]


_CN_TZ = timezone(timedelta(hours=8))


def get_period_pnl(email: str, current_net_value: float) -> dict:
    """本日/昨日/本月收益——用户明确要求"要有本日收益昨日收益本月收益三个
    模块，还有一个收益百分比，负收益就是负数"。基于sim_equity_snapshots
    (每5分钟一次的净值快照)按北京时间的自然日/自然月分组，跟当前实时净值
    (current_net_value，调用方传入——快照表最新一条可能有几分钟延迟，"现在"
    这一端要用真正实时查到的数字，不能拿旧快照冒充)做差值。

    某个时间窗口内一条快照都没有(比如AI今天还没运行过、或者是本月第一天
    还没有上个月末的数据可比)时，对应字段返回None，界面上如实显示"暂无
    数据"，不拿0或者硬凑一个数字出来充当"没有变化"。
    """
    init_db()
    with closing(_conn()) as c:
        rows = c.execute(
            "SELECT snapshot_at, net_value_hkd FROM sim_equity_snapshots WHERE email = ? ORDER BY snapshot_at ASC",
            (email,),
        ).fetchall()

    parsed: list[tuple[datetime, float]] = []
    for snapshot_at, net_value in rows:
        try:
            dt = datetime.fromisoformat(snapshot_at)
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        parsed.append((dt.astimezone(_CN_TZ), net_value))

    def _pct_block(base: float | None, end: float) -> dict | None:
        if base is None or base == 0:
            return None
        change = end - base
        return {"change": change, "pct": change / base * 100}

    if not parsed:
        return {"today": None, "yesterday": None, "month": None}

    today = datetime.now(_CN_TZ).date()
    yesterday = today - timedelta(days=1)
    month_start = today.replace(day=1)

    # 每个自然日/月窗口的"期初"：这个窗口开始后第一条快照；"期末"（仅
    # 昨日收益需要，本日/本月的期末统一用current_net_value）：这个窗口
    # 结束前最后一条快照。
    today_start = next((v for dt, v in parsed if dt.date() >= today), None)
    month_start_val = next((v for dt, v in parsed if dt.date() >= month_start), None)

    yesterday_start = next((v for dt, v in parsed if dt.date() >= yesterday), None)
    yesterday_end = None
    for dt, v in parsed:
        if dt.date() < today:
            yesterday_end = v
        else:
            break

    return {
        "today": _pct_block(today_start, current_net_value),
        "yesterday": _pct_block(yesterday_start, yesterday_end) if yesterday_end is not None else None,
        "month": _pct_block(month_start_val, current_net_value),
    }


def get_sim_agent_runs(email: str, limit: int = 30) -> list[dict]:
    init_db()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM sim_agent_runs WHERE email = ? ORDER BY run_at DESC LIMIT ?", (email, limit),
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


def extract_score_breakdown(text: str) -> dict:
    """从AI判断原文里解析"维度打分：基本面X/40 · 价格位置X/30 · 技术面X/15 ·
    数据确定性X/15"这一行，拆成四个独立数字——advisor.py的_JUDGE_SYSTEM
    prompt里要求AI必须输出这一行，但之前从没被结构化提取过，只是混在
    fundamental_verdict这段自由文本里，导致get_score_band_backtest永远
    只能回测"综合得分"这一个黑箱数字，没法回答"到底哪个维度真的有预测力"
    （2026-08-29 Fable 5复核指出的缺口）。

    每个子分数独立解析、独立返回None（不是整行解析失败就全部放弃）——
    某天AI输出格式稍微跑偏、少写了一项，不该因为这一项连累另外三项也
    解析不出来。允许"X/40"里的"X"是"—"或空（AI偶尔会用占位符表示这项
    没法打分），这种情况该子项返回None，不当0分处理，跟这个项目一贯
    "解析不出来不代表0分"的原则一致。
    """
    result = {"fundamental": None, "price_position": None, "technical": None,
              "chips": None, "analyst": None, "data_certainty": None}
    m = re.search(r"维度打分[：:]([^\n]+)", text)
    if not m:
        return result
    line = m.group(1)
    # 分母写成 \d+ 而不是写死具体数字。2026-09-05真实故障：09-05把打分从
    # 四维扩到六维、并按短线重新配权（基本面40->22、价格位置30->20、
    # 技术面15->20、数据确定性15->10），但这里的正则还写死着旧分母，
    # 于是四个维度全部匹配失败、整列落库成NULL——总分照常写进去了，
    # 所以从页面上完全看不出问题，直到审计时查数据库才发现。
    #
    # 把分母参数化掉，以后再调权重这里就不会跟着失效。分母本身对解析没有
    # 意义（我们要的是分子），当初写死纯粹是为了让正则更"精确"，结果精确
    # 变成了脆弱。
    patterns = {
        "fundamental": r"基本面\s*(\d+)\s*/\s*\d+",
        "price_position": r"价格位置\s*(\d+)\s*/\s*\d+",
        "technical": r"技术面\s*(\d+)\s*/\s*\d+",
        "chips": r"筹码面\s*(\d+)\s*/\s*\d+",
        "analyst": r"分析师预期\s*(\d+)\s*/\s*\d+",
        "data_certainty": r"数据确定性\s*(\d+)\s*/\s*\d+",
    }
    for key, pat in patterns.items():
        pm = re.search(pat, line)
        if pm:
            result[key] = int(pm.group(1))
    return result


def log_advice(
    email: str, symbol: str, price_at_advice: float, fundamental_verdict: str,
    technical_signal: str, action: str = "观望", market: str = "A", name: str = "",
    source: str = "position", score: int | None = None,
) -> int:
    init_db()
    breakdown = extract_score_breakdown(fundamental_verdict)
    with closing(_conn()) as c:
        cur = c.execute(
            "INSERT INTO advice (email, symbol, market, name, created_at, price_at_advice, "
            "fundamental_verdict, technical_signal, action, source, score, "
            "score_fundamental, score_price_position, score_technical, score_data_certainty, "
            "score_chips, score_analyst) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (email, symbol, market, name, datetime.now(timezone.utc).isoformat(), price_at_advice,
             fundamental_verdict, technical_signal, action, source, score,
             breakdown["fundamental"], breakdown["price_position"],
             breakdown["technical"], breakdown["data_certainty"],
             breakdown["chips"], breakdown["analyst"]),
        )
        c.commit()
        return cur.lastrowid


def get_due_for_advice_review(
    email: str, min_age_days: float = 7, limit: int = 20, source: str | None = None,
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

    2026-08-29新增"按市场"拆分（返回字典多一个"按市场"键）：Fable 5复核
    时指出，A股观察池选股口径是涨停股池（当天已经涨停10%/20%的股票），
    跟港美股"人气榜/知名蓝筹"完全不是同一类总体——A股涨停股次日走势更多
    是"情绪面剩余动能能不能延续"，跟基本面质量的相关性天然弱，如果三个
    市场混在同一批分数区间里回测，A股这类跟基本面无关的噪音会污染"这套
    打分体系到底有没有预测力"这个问题的检验结果。总体的bands还保留（有
    些场景就是想看整体），但同时也按市场各自独立算一遍，方便对照。
    """
    def _compute_bands(rows: list[dict]) -> list[dict]:
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
        return bands

    init_db()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT score, market, price_at_advice, review_price FROM advice WHERE source = ? "
            "AND score IS NOT NULL AND review_price IS NOT NULL "
            "AND price_at_advice IS NOT NULL AND price_at_advice > 0",
            (source,),
        ).fetchall()
    rows = [dict(r) for r in rows]

    by_market = {
        m: {"total_reviewed": len(mrows), "bands": _compute_bands(mrows)}
        for m in sorted(set(r.get("market", "A") for r in rows))
        for mrows in [[r for r in rows if r.get("market", "A") == m]]
    }
    return {
        "total_reviewed": len(rows), "min_sample": min_sample,
        "bands": _compute_bands(rows), "按市场": by_market,
    }


_DIMENSION_COLUMNS = {
    "基本面质量": "score_fundamental",
    "价格位置安全边际": "score_price_position",
    "技术面确认": "score_technical",
    "筹码面": "score_chips",
    "分析师预期": "score_analyst",
    "数据确定性": "score_data_certainty",
}


def get_dimension_predictive_value(source: str = "watchlist", min_sample: int = 6) -> dict:
    """回答Fable 5复核提出的核心问题："综合得分里四个维度，到底是哪个真的
    对预测有贡献，哪个只是看起来严谨？"——get_score_band_backtest只能回测
    "综合得分"这一个加总后的数字，没法拆开看，这个函数用extract_score_
    breakdown回填出来的四个独立列，把每个维度单独拿出来验证。

    做法：每个维度分别按"这个维度这次打分是不是在该维度中位数以上"把样本
    分成高分组/低分组，对比两组的事后平均涨跌幅——不用更复杂的相关系数/
    回归（样本量现在还很小，算出来的相关系数本身就不可靠，容易给人"看起来
    精确"但其实是噪音的错觉），高低分组对比这种最朴素的办法，在小样本下
    更不容易被过度解读，跟这个项目一贯"宁可少给结论也不编"的原则一致。
    单个维度里某次判断没解析出这个维度的分数（extract_score_breakdown
    返回None的情况）就不计入这个维度的统计，不当0分处理。
    """
    init_db()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            f"SELECT {', '.join(_DIMENSION_COLUMNS.values())}, price_at_advice, review_price "
            "FROM advice WHERE source = ? AND review_price IS NOT NULL "
            "AND price_at_advice IS NOT NULL AND price_at_advice > 0",
            (source,),
        ).fetchall()
    rows = [dict(r) for r in rows]

    result = {}
    for label, col in _DIMENSION_COLUMNS.items():
        valid = [r for r in rows if r.get(col) is not None]
        n = len(valid)
        if n < min_sample:
            result[label] = {"count": n, "min_sample": min_sample, "note": "样本不够，暂不计算"}
            continue
        valid.sort(key=lambda r: r[col])
        mid = n // 2
        low_group, high_group = valid[:mid], valid[mid:]

        def _avg_return(group):
            returns = [(r["review_price"] - r["price_at_advice"]) / r["price_at_advice"] * 100 for r in group]
            return round(sum(returns) / len(returns), 2)

        result[label] = {
            "count": n,
            "低分组": {"count": len(low_group), "avg_return_pct": _avg_return(low_group)},
            "高分组": {"count": len(high_group), "avg_return_pct": _avg_return(high_group)},
        }
    return result


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


def get_recent_advice_map(within_hours: int = 36) -> dict:
    """把最近一段时间内 advisor.py 出过结论的标的整理成 {(symbol, market): {...}}。

    2026-09-04新增，用途是把"投研顾问"和"AI模拟盘"这两条一直各跑各的链路接上。
    在此之前它们是完全不通气的：advisor.py 每天17:30跑一次深度分析，看财务
    摘要、新闻、研报、52周位置，一次几十个AI调用；sim_agent.py 每5分钟跑一次
    自主决策，但手里只有"现价/涨跌幅/量比/换手率/PE/PB"这种一眼行情。也就是
    说系统里已经算出来的那份更贵、更厚的判断，高频决策那条链路一次都没用上。

    这里只取最精简的部分（结论+综合得分），不取 fundamental_verdict 那段长文本
    ——候选池一次有几十支，把长文本全塞进 prompt 会让本来就要 75~110 秒的那次
    AI调用更慢（这个超时问题刚在2026-09-04量过），而"顾问怎么看这支"这件事
    用"买入/持有/观望/卖出 + 分数"表达已经足够，长篇理由对高频动量决策的边际
    价值很低。

    within_hours 默认36小时：advisor 是工作日每天17:30跑，36小时能覆盖到"昨天
    收盘后那一批"，周末/节假日不至于因为跨了一天就整个断供；再长就容易把过时
    的结论当成今天的判断喂进去了。
    """
    init_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=within_hours)).isoformat()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT symbol, market, action, score, created_at FROM advice "
            "WHERE created_at >= ? ORDER BY created_at",
            (cutoff,),
        ).fetchall()
    # 按时间正序遍历，同一个标的后面的记录自然覆盖前面的，留下的是最新那条。
    out: dict[tuple[str, str], dict] = {}
    for r in rows:
        sym = (r["symbol"] or "").strip()
        mkt = (r["market"] or "").strip()
        if not sym or not mkt:
            continue
        out[(sym, mkt)] = {
            "action": r["action"], "score": r["score"], "created_at": r["created_at"],
        }
    return out


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
        _cutoff = _latest_run_cutoff(c, source) or (run_date + "T00:00:00")
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


def _apply_market_quota(rows: list[dict], limit: int, quota: dict[str, int]) -> list[dict]:
    """2026-09-02新增：rows已经按score降序排好——quota给每个市场留的最多
    席位数，按score从高到低分别往对应市场的桶里放，桶满了就不再收这个
    市场的（多出来的进others，只有配额没收满时才拿来补位）。用户明确
    反馈过"推荐股排行榜五个全是港股"，这天恰好港股候选普遍打分更高，
    纯score排序会把美股全挤出前5——不是bug，是分数确实那样，但用户要的
    是"港美都有、美股占大头"这个固定诉求，不是"分数说了算"，所以需要
    显式配额而不是像advisor.py._leaderboard那样只设一个"每个市场最多
    几席"的上限（那种只保证不被单一市场包圆，不保证哪个市场占大头）。
    """
    buckets: dict[str, list[dict]] = {m: [] for m in quota}
    others: list[dict] = []
    for r in rows:
        m = r.get("market")
        if m in buckets and len(buckets[m]) < quota[m]:
            buckets[m].append(r)
        else:
            others.append(r)
    result = [r for m in quota for r in buckets[m]]
    for r in others:
        if len(result) >= limit:
            break
        result.append(r)
    result.sort(key=lambda r: r["score"], reverse=True)
    return result[:limit]


def _latest_run_cutoff(c, source: str) -> str:
    """最近一轮 advisor 跑批的起始时间戳，用来把"这一轮"跟之前几轮切开。

    2026-09-05新增。原来两处调用方都是拿最新一条记录所在的**自然日**当一批，
    当时的注释写着"就算同一天手动多跑了几次，也只是同一天的记录被合并当一批，
    不影响正确性"——那个结论只在打分口径不变时成立。

    这天打分从四维改成六维，同一个UTC日里先后跑了旧口径和新口径两轮，混在
    一起按分数排序，旧口径那轮打出的90-92分（旧规则满分更好拿）直接把新口径
    的85分压下去，页面上看到的还是改动前的榜单，看起来像"改了没生效"。

    改成从最新一条往回走、遇到超过30分钟空档就断开：一轮跑下来相邻两条判断
    之间只有几十秒，两轮之间隔着几小时，用间隔切分能干净地只取最近那一轮，
    跟自然日无关，也不用给表加批次列。

    时间戳解析不了就退回"当天零点"，即老行为，至少不比改之前更差。
    """
    stamps = [r[0] for r in c.execute(
        "SELECT created_at FROM advice WHERE source = ? AND score IS NOT NULL "
        "ORDER BY created_at DESC LIMIT 800", (source,),
    ).fetchall()]
    if not stamps:
        return ""
    cutoff = stamps[0]
    try:
        prev = datetime.fromisoformat(stamps[0])
        for ts in stamps[1:]:
            cur = datetime.fromisoformat(ts)
            if (prev - cur).total_seconds() > 30 * 60:
                break
            cutoff, prev = ts, cur
    except Exception:
        return stamps[0][:10] + "T00:00:00"
    return cutoff


def get_latest_leaderboard(limit: int = 10, source: str = "screen", market_quota: dict[str, int] | None = None) -> dict:
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

    market_quota（2026-09-02新增，默认None不生效）：{市场: 最多几席}——用户
    明确反馈"首页推荐股排行榜五个全是港股"，要求"港美都有、美股占大头"，
    这是一个固定诉求，不是"分数排出来是什么就是什么"。传了这个参数就不再
    是单纯score DESC LIMIT，而是先按_apply_market_quota分配。app.py/
    assistant.py两处首页排行榜的source="watchlist"调用都传这个，source=
    "screen"（私人微信简报的综合得分Top10）不传，维持"好的自然上榜"不变。
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
        _cutoff = _latest_run_cutoff(c, source) or (run_date + "T00:00:00")
        # market_quota要在全量候选里挑，不能先用SQL LIMIT截断到limit条
        # （截断早了美股候选可能压根没进这批行，配额也补不回来）。这批
        # 候选本来就是一天的观察池（约120支封顶），全取出来在Python里
        # 排序/分配的开销可以忽略。
        sql_limit = 100000 if market_quota else limit
        rows = c.execute(
            """
            SELECT * FROM advice WHERE source = ? AND created_at >= ? AND score IS NOT NULL
            AND id IN (
                SELECT MAX(id) FROM advice WHERE source = ? AND created_at >= ? AND score IS NOT NULL
                GROUP BY symbol
            )
            ORDER BY score DESC LIMIT ?
            """,
            (source, _cutoff, source, _cutoff, sql_limit),
        ).fetchall()
    board = [dict(r) for r in rows]
    if market_quota:
        board = _apply_market_quota(board, limit, market_quota)
    return {"run_date": run_date, "leaderboard": board}


def get_watchlist_verdict_for_symbol(symbol: str, source: str = "watchlist") -> dict:
    """2026-08-30新增：给AI咨询窗答"为什么XX没上首页推荐榜"这类问题用。

    这类问题最常见的错误答法是让AI凭自己知识临时分析一遍基本面/估值，
    答案本身可能没错，但没答到点子上——用户真正想知道的往往是"它今天
    有没有进观察池"，这跟"进了池子但分数不够"是两件完全不同的事：观察池
    每天只挑约120支当天最热门的股票（见advisor.py的_build_watchlist），
    大量业绩优秀的大盘股（比如贵州茅台、五粮液这类）大部分交易日热度
    排不进这120支，根本没被打分，不是"打分低被淘汰"。这个函数直接查
    这支股票今天在不在池子里、如果在的话真实得分/排名是多少，让AI用
    网站自己的真实判断结果回答，不用另起炉灶分析一遍。
    """
    init_db()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        latest = c.execute(
            "SELECT created_at FROM advice WHERE source = ? ORDER BY created_at DESC LIMIT 1", (source,),
        ).fetchone()
        if latest is None:
            return {"in_pool": False, "run_date": None, "pool_size": 0}
        run_date = latest["created_at"][:10]
        _cutoff = _latest_run_cutoff(c, source) or (run_date + "T00:00:00")
        # 当天完整打分池（去重取每支symbol当天最新一条，score不为空），
        # 用来算这支股票的真实排名和池子总大小。
        rows = c.execute(
            """
            SELECT * FROM advice WHERE source = ? AND created_at >= ? AND score IS NOT NULL
            AND id IN (
                SELECT MAX(id) FROM advice WHERE source = ? AND created_at >= ? AND score IS NOT NULL
                GROUP BY symbol
            )
            ORDER BY score DESC
            """,
            (source, _cutoff, source, _cutoff),
        ).fetchall()
        pool = [dict(r) for r in rows]
    for rank, row in enumerate(pool, start=1):
        if row["symbol"] == symbol:
            return {
                "in_pool": True, "run_date": run_date, "pool_size": len(pool), "rank": rank,
                "score": row["score"], "action": row["action"],
                "verdict_excerpt": (row.get("fundamental_verdict") or "")[:300],
            }
    return {"in_pool": False, "run_date": run_date, "pool_size": len(pool)}


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


def get_score_evidence_text(source: str = "watchlist") -> str:
    """把打分体系的事后回测结论整理成一段可以直接塞进prompt的实证文字。

    2026-09-04第一版把结论写成"7天窗口、分数越高表现越差、单调递减"，两处都
    不准确，已更正——错误的前提喂给AI比不喂更糟，这里把更正过程一并记下来，
    免得以后又照着直觉重写一遍：

    一、窗口不是7天，是约1天。watchlist 这条线的回填走的是"次日核对"
        （get_due_for_advice_review 传 min_age_days=1）。实测786条已回填样本
        的 created_at 到 review_at 间隔中位数0.99天、均值1.20天。

    二、"分数越高表现越差"是合并统计造成的假象（辛普森悖论）。按市场拆开：
        美股(n=477) 70-89档-0.65%/上涨41.6%，30-49档+0.34%/上涨55.9%
                    ——高分没有优势，但谈不上单调反向；
        港股(n=281) 70-89档-1.42%，50-69档-1.07%，30-49档-1.85%
                    ——根本不单调，最低档反而最差，各档全负；
        A股(n=28)   样本太小，不作结论。
        港股整体基线就差（各档全负），而它在高分档里占比又比美股高得多
        （89/281=32% vs 77/477=16%），于是把合并后的高分档整体拖了下去。

    所以真正站得住的结论只有一条：这套综合得分对"次日涨跌"没有可证实的预测力。
    而它本来也不该有——40分基本面质量+30分价格位置安全边际，占了70%的权重，
    衡量的是公司质地和估值位置，那是按季度起作用的东西，拿来预测明天从设计上
    就不成立。这是衡量口径错配（用一个慢信号去打一个快窗口的分），不是模型算错。

    据此仍然不改打分权重：真要改，正确的做法是把回看窗口拉长到跟信号的时间
    尺度匹配，而不是把一个为季度设计的评分硬掰成日内择时指标。改权重还会让
    "这套打分到底有没有用"失去干净的对照。这里只把事实和它的边界如实交给AI。

    样本不足时返回空字符串——没有证据就不要编一段"经验"出来误导它。
    """
    try:
        bt = get_score_band_backtest(source=source, min_sample=5)
    except Exception:
        return ""
    total = bt.get("total_reviewed") or 0
    if total < 100:
        return ""

    # 真实回看间隔从数据里实算，不写死。2026-09-04把 watchlist 的校验窗口从
    # 0.9天改成6.9天之后，库里会有一段时间同时存在"1天口径"和"7天口径"两批
    # 已回填数据；任何写死的窗口描述在过渡期都会是错的，而错误的前提喂给AI
    # 比不喂更糟（这一版就是因为上一版写死"7天"而实际是1天才重写的）。
    lags = []
    try:
        with closing(_conn()) as c:
            for ca, ra in c.execute(
                "SELECT created_at, review_at FROM advice "
                "WHERE source = ? AND review_price IS NOT NULL AND review_at IS NOT NULL",
                (source,),
            ):
                try:
                    lags.append(
                        (datetime.fromisoformat(ra) - datetime.fromisoformat(ca)).total_seconds() / 86400
                    )
                except Exception:
                    continue
    except Exception:
        lags = []
    if lags:
        lags.sort()
        _med = lags[len(lags) // 2]
        _lo, _hi = lags[0], lags[-1]
        window_txt = (
            f"核对窗口按实际回填时间算，中位数{_med:.1f}天"
            + (f"（区间{_lo:.1f}~{_hi:.1f}天）" if _hi - _lo > 0.5 else "")
        )
    else:
        window_txt = "核对窗口未知"

    by_market = bt.get("按市场") or {}
    _label = {"US": "美股", "HK": "港股", "A": "A股"}
    mkt_lines = []
    for mk in ("US", "HK", "A"):
        sub = by_market.get(mk)
        if not sub:
            continue
        bands = sub.get("bands") if isinstance(sub, dict) else sub
        bits = [
            f"{b['band']}分档{b['count']}条平均{b['avg_return_pct']:+.2f}%、上涨{b['win_rate_pct']:.0f}%"
            for b in (bands or [])
            if b.get("count") and b.get("avg_return_pct") is not None
        ]
        if bits:
            mkt_lines.append(f"    {_label.get(mk, mk)}：" + "；".join(bits))
    if not mkt_lines:
        return ""

    return (
        f"本系统打分体系的事后实证（{total}条已回填样本，{window_txt}，"
        f"是客观统计不是理论）：\n"
        + "\n".join(mkt_lines) + "\n"
        "  必须按市场分开看：三个市场的候选池口径完全不同（A股是当天涨停股池，"
        "港美股是人气榜/蓝筹），混在一起统计会出现辛普森悖论——港股整体基线更差、"
        "又在高分档占比更高，会把合并后的高分档拖低，看上去像\"分数越高越差\"，"
        "拆开后并不成立。\n"
        "  站得住的结论只有一条：在上面这个窗口上，这套综合得分没有可证实的预测力。"
        "需要说明的是，之前绝大多数样本是按\"次日核对\"回填的，而综合得分里"
        "基本面质量40分+价格位置30分占了七成权重，衡量的是公司质地和估值位置，"
        "那是按季度起作用的东西——拿它去打一天的分，从设计上就不可能成立，"
        "这是衡量口径错配，不代表这套评分本身没有价值。校验窗口已于2026-09-04"
        "从次日改成一周，新样本会在更接近它该被检验的尺度上重新积累，在那之前"
        "不要拿旧结论下死判断。\n"
        "  怎么用：把综合得分当作\"这家公司的质地和估值位置如何\"的描述，不要当作"
        "\"明天会不会涨\"的理由。短周期的买卖时点要靠技术面确认（真实放量、趋势"
        "成立）和当下的盘面证据，不能靠高分背书。尤其\"52周低位所以安全\"这句话，"
        "在短周期里没有数据支持——低位往往意味着还在下跌趋势里。"
    )


def log_macro_brief(topic: str, title: str, brief_text: str, sources_json: str = "",
                    chart_json: str = "") -> int:
    """写一条宏观议题解读，并只保留这个议题最近5条。

    保留几条而不是只留一条：偶尔会想对照"上次美联储会议后是怎么说的、这次
    变了什么"，但也没必要无限存下去——这类内容时效性强，一周前的解读已经
    没有参考价值了。
    """
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    with closing(_conn()) as c:
        cur = c.execute(
            "INSERT INTO macro_briefs (topic, title, brief_text, sources_json, chart_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (topic, title, brief_text, sources_json, chart_json, now),
        )
        c.execute(
            "DELETE FROM macro_briefs WHERE topic = ? AND id NOT IN "
            "(SELECT id FROM macro_briefs WHERE topic = ? ORDER BY created_at DESC LIMIT 5)",
            (topic, topic),
        )
        c.commit()
        return cur.lastrowid


def get_latest_macro_briefs() -> list[dict]:
    """每个议题最新的一条，按生成时间倒序。页面只读这个，不触发任何AI调用。"""
    init_db()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM macro_briefs WHERE id IN "
            "(SELECT MAX(id) FROM macro_briefs GROUP BY topic) "
            "ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_user_overview(email: str) -> dict:
    """"我的"页要的那一份个人概览，一次连接里全取完。

    2026-09-04新增。原来这些数字散落在各处（自选/持仓数在页面里现算、分析
    次数没人统计过、搜索历史只在搜索框里用），"我的"这个分区要把它们放到一起，
    与其在 app.py 里连着开五六次连接、写五六段 SQL，不如在这里一次取完——
    这类概览查询本来就该是一次往返，页面那边只管画。

    返回的都是原始计数和分组，不做任何百分比/文案，展示逻辑留给前端。
    """
    init_db()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        one = lambda sql, *a: c.execute(sql, a).fetchone()[0]

        watch_count = one(
            "SELECT count(*) FROM positions WHERE email = ? AND (shares IS NULL OR shares <= 0)", email)
        hold_count = one("SELECT count(*) FROM positions WHERE email = ? AND shares > 0", email)
        analysis_count = one("SELECT count(*) FROM analyses WHERE email = ?", email)
        search_count = one("SELECT count(*) FROM search_history WHERE email = ?", email)

        # 自选/持仓按市场分布——比一个总数更能说明"这个人在看哪个市场"
        by_market = {
            r["market"] or "A": r["n"]
            for r in c.execute(
                "SELECT market, count(*) AS n FROM positions WHERE email = ? GROUP BY market", (email,))
        }

        # "从什么时候开始用的"——取各张表里最早的一条时间，谁早算谁。用户表本身
        # 没存注册时间（auth那边只存了凭证），只能从活动记录反推，够用了。
        firsts = []
        for sql in (
            "SELECT min(added_at) FROM positions WHERE email = ?",
            "SELECT min(searched_at) FROM search_history WHERE email = ?",
            "SELECT min(created_at) FROM analyses WHERE email = ?",
        ):
            v = c.execute(sql, (email,)).fetchone()[0]
            if v:
                firsts.append(v)
        first_seen = min(firsts) if firsts else None

        row = c.execute(
            "SELECT max_capital_cny, ai_sim_trading, sim_agent_enabled FROM user_settings WHERE email = ?",
            (email,),
        ).fetchone()
        settings = dict(row) if row else {}

    return {
        "watch_count": watch_count, "hold_count": hold_count,
        "analysis_count": analysis_count, "search_count": search_count,
        "by_market": by_market, "first_seen": first_seen,
        "max_capital_cny": settings.get("max_capital_cny"),
        "ai_sim_trading": bool(settings.get("ai_sim_trading")),
        "sim_agent_enabled": bool(settings.get("sim_agent_enabled")),
    }


def get_search_history(email: str, limit: int = 10) -> list[dict]:
    init_db()
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM search_history WHERE email = ? ORDER BY searched_at DESC LIMIT ?",
            (email, limit),
        ).fetchall()
        return [dict(r) for r in rows]
