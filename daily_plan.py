# -*- coding: utf-8 -*-
"""每日操作清单：把散落在各处的信号收敛成一份"今天该看什么"。

用户的原话是"我根据他的指示在股票系统里操作赚钱"——他要的不是又一个数据
看板，是一份能拿着去汇丰下单的清单。项目此前的产出是分散的：排行榜给分数
但不给价位，持仓判断给动作但不给仓位，模拟盘自己在跑但仓位跟用户的账户
没有关系。三样东西都需要用户自己在脑子里合成，而这一步恰恰最容易掉链子。

这个模块负责合成。每条给出：标的、方向、现价、参考区间、止损位、依据、
置信度，以及最要紧的一项——**这套信号目前有没有被验证过**。

关于最后一项，有必要把话说死。2026-09-06 审计查过数据库：那时全部已复核
记录的判断到复核间隔是 0.97~1.43 天，而打分七成权重压在按季度起作用的
基本面和估值上。用一天的波动检验它，量到的是噪音不是信号，所以那批"分数
越高表现越差"的结论既不能证明系统有效，也不能证明它无效——它只说明我们
还没测过。09-04 起窗口已改成 6.9 天，第一批可信数据 09-07 之后才有。

因此这份清单的定位是**筛选结果 + 执行参数的参考**，不是"照着买就能赚"的
指令。expectancy.py 的验证状态会原样印在清单顶部，验证通过之前那行会一直
写着"尚未验证"。这不是免责话术，是对"高数学期望"这个目标本身负责：一个
没测过期望的策略，重复执行它跟掷硬币没有区别。

止损位取 20 日 ATR 的 2 倍，这是个有依据的默认值：ATR 衡量的是这只票平时
一天能走多远，2 倍 ATR 意味着"跌破这里，说明发生的不是日常波动"。不用固定
百分比是因为不同标的的波动率差好几倍——对 MSTR（日均 7%）用 5% 止损，
等于开盘就被扫掉。
"""
import datetime as dt
import json
import sys

import advisor
import data_sources as ds
import expectancy
import risk_policy
import tracker

# 只有分数达到这条线的标的才进清单。65 分不是拍脑袋：低于它的判断在文本里
# 几乎都带着"数据不足/等待更明确信号"这类措辞，放进"今天该看什么"只会稀释
# 注意力。这个阈值等期望值验证出来之后应该按分段表现重新定。
_MIN_SCORE = 65

# 一份清单最多几条。超过这个数就不再是"清单"而是"又一个列表"了——人一天
# 能认真处理的决策数量有限，宁可漏掉边缘机会，也不要让真正值得看的那几条
# 被淹没。
_MAX_ITEMS = 8
# 进闸门的候选池大小。跟 _MAX_ITEMS（最终展示几条）分开：池子要够大，
# 才轮得到那些"分数中上但赔率好"的票——它们才是期望值真正的来源。
_POOL_SIZE = 45

# ---- 仓位与期望值 ----
#
# 用户要"数学期望最高"。期望 = 胜率×平均盈利 - 败率×平均亏损。麻烦在于
# 这套打分的胜率目前还没验证出来（见 expectancy.py 里那段），所以不能用
# 历史胜率去算仓位。
#
# 但有一条路是不依赖胜率的：**盯住盈亏比**。如果目标价距离是止损距离的
# 3 倍，那么只要胜率超过 25% 期望就是正的；盈亏比 2 倍时门槛是 33%。
# 换句话说，与其去猜"这次能不能对"，不如只做那些"对一次能补三次错"的机会。
# 这是在胜率未知时唯一数学上站得住的做法，所以下面用 _MIN_RR 卡门槛，
# 达不到的标的会被标成"盈亏比不足"而不是给一个仓位。
# 盈亏比门槛。2.0 -> 2.5（2026-09-06）。
#
# 用户："门槛可以设置的高一点，就算达标的只有一只两只也可以，我们要找最优解"。
# 这个要求有个独立于偏好的正当理由：胜率目前是"尚未验证"状态，而盈亏比门槛
# 换算过去就是保本胜率——2:1 要求胜率>33%，2.5:1 只要>28.6%，3:1 只要>25%。
# 越不知道自己的胜率，就越该要求更高的赔率来兜底。等期望值那套攒够样本、
# 胜率被真实验证之后，这个数可以降回 2.0。
#
# 配合 _rank_candidates 按盈亏比降序排，达标的不设数量上限：宁可某天只有
# 一两支，也不要为了凑满清单放进赔率不够的票。
_MIN_RR = 2.5

# 单笔最多亏总资金的百分之几。2% 是仓位管理里的常规值：连错 10 次总回撤
# 约 18%，账户还活着；用 10% 的话连错 5 次就腰斩，那时候即使策略是对的
# 也已经没有本金去等它兑现了。
_RISK_PER_TRADE_PCT = 2.0

# 单笔仓位占总资金的上限。风险预算算出来的仓位在止损很近时会非常大
# （风险200元 / 止损2% = 一万元，等于满仓），必须再加一道集中度约束。
_MAX_POSITION_PCT = 30.0

# 港股每手股数。仓位要按手取整，不然算出来的股数根本下不了单。
_HK_LOT_FALLBACK = 100

# ---- 两档持有周期 ----
#
# 2026-09-06 用户把周期从"1-4周"缩到"5-10天，甚至再加1-2天的超短期"。
# 周期一变，止损和目标位都必须跟着变，否则会算出系统性偏差的赔率：
#
#   止损距离要跟持有时间匹配。2倍ATR 是给四周持有用的——那个窗口里价格
#   有足够时间在噪音里来回，需要宽止损才不会被扫。压到5-10天之后同样的
#   宽度就成了纯粹的浪费：既承担了四周的下行，又只吃一周的上行，赔率
#   天然算不出来。
#
#   目标位同理，从"20日高点"缩到"5日/10日高点"。
#
# 每档一组 (标签, 止损ATR倍数, 找阻力时优先看几日高点)。
_HORIZONS = (
    ("超短线1-2天", 1.0, (5,)),
    # 2026-09-06 二次收缩：用户"我现在不需要五到十天了，我现在想要的是
    # 五个交易日内，超短线"。主档从5-10天压到3-5天，止损倍数跟着从1.5降到
    # 1.25——持有期短一半，能容忍的回撤也该更小，否则止损宽度相对于目标
    # 距离过大，赔率会被结构性压低。阻力窗口从(10,20)收到(5,10)：20日高点
    # 是四周才可能摸到的位置，拿它给五天的交易当目标就是老错误的重演。
    ("短线3-5天", 1.25, (5, 10)),
)


def _atr(symbol: str, market: str, days: int = 20) -> float | None:
    """20日平均真实波幅。止损距离的客观标尺。"""
    try:
        end = dt.date.today()
        start = end - dt.timedelta(days=days * 2 + 20)
        # 注意参数顺序：get_stock_history(symbol, start, end, frequency, market)，
        # market 是第五个参数不是第二个。按第二个传的话 start_date 会收到
        # "HK" 这种字符串，接口直接空手而归——而且是静默的。
        df = ds.get_stock_history(symbol, start.isoformat(), end.isoformat(),
                                  "d", market)
    except Exception:
        return None
    if df is None or getattr(df, "empty", True) or len(df) < 5:
        return None
    try:
        hi = df["最高"].tolist() if "最高" in df.columns else df["high"].tolist()
        lo = df["最低"].tolist() if "最低" in df.columns else df["low"].tolist()
        cl = df["收盘"].tolist() if "收盘" in df.columns else df["close"].tolist()
    except Exception:
        return None
    trs = []
    for i in range(1, len(cl)):
        trs.append(max(hi[i] - lo[i], abs(hi[i] - cl[i - 1]), abs(lo[i] - cl[i - 1])))
    if not trs:
        return None
    tail = trs[-days:]
    return sum(tail) / len(tail)


def _capital_cny() -> float:
    """用户设定的最大资金投入量。取不到时返回0——0会让下游明确显示
    "未设置资金规模，无法给出仓位"，而不是拿一个假设的数字去算。"""
    try:
        import sqlite3
        from pathlib import Path
        db = Path(__file__).resolve().parent / "data" / "track_record.db"
        with sqlite3.connect(db) as c:
            row = c.execute("SELECT max_capital_cny FROM user_settings WHERE email=?",
                            (advisor._EMAIL,)).fetchone()
        return float(row[0]) if row and row[0] else 0.0
    except Exception:
        return 0.0


def _fx_to_cny(market: str) -> tuple[float, str]:
    """市场货币兑人民币。A股本身就是CNY。"""
    cur = {"HK": "HKD", "US": "USD"}.get(market)
    if not cur:
        return 1.0, "CNY"
    try:
        rate, _note = ds.get_fx_rate(cur, "CNY")
        return (float(rate) if rate else 0.0), cur
    except Exception:
        return 0.0, cur


def _reality_check(symbol: str, market: str, last: float,
                   target: float | None) -> str | None:
    """目标价隐含的市值是多少，这个涨幅到底有多难。

    2026-09-06 用户指出的问题：英伟达目标价 325.23、盈亏比 7.0:1，清单上
    看着极有吸引力——但他一眼看出现市值已经 5.55 万亿，涨到目标价意味着
    市值要去 7.84 万亿，也就是**再凭空长出 2.3 万亿**，约等于再造一个
    台积电加一个特斯拉。

    百分比会隐藏规模。+41% 对一只两百亿市值的公司是平常事，对一只五万亿
    市值的公司是另一回事——后者需要的绝对增量，可能超过全球能腾出来的
    增量资金。而盈亏比这个指标只看价格距离，完全不管这段距离背后要发生
    什么，所以"盈亏比 7:1、胜率超过 12% 就够"这句话对大市值股是有误导性的。

    这里不下"能不能到"的结论——那需要判断 AI 资本开支能不能持续，不是
    一个脚本该做的事。只把绝对量摆出来，让用户自己掂量。这也正是用户
    要的"不能坑我"：数字本身不会骗人，但只给百分比就是在选择性呈现。
    """
    if not target or not last or target <= last:
        return None
    # 市值要从富途原始快照取。get_stock_realtime_futu 返回的中文键里没有
    # 总市值这一项（它只挑了价格相关的几个字段），第一版从那里取，拿到的
    # 永远是 0，整段规模提示静默消失——没报错，只是不显示。
    mv = 0.0
    try:
        import futu as ft
        r = ds._futu_call(
            lambda c: c.get_market_snapshot([ds._futu_code(symbol, market)]),
            timeout=20, default=None)
        df = ds._unwrap_futu(r)
        if df is not None and not df.empty:
            mv = float(df.iloc[0].get("total_market_val") or 0)
    except Exception:
        mv = 0.0
    if mv <= 0:
        return None

    implied = mv * (target / last)
    delta = implied - mv
    cur_t = mv / 1e12
    imp_t = implied / 1e12
    dl_t = delta / 1e12

    # 只在绝对增量大到值得停一下的时候才说。小盘股涨40%不需要这段提醒，
    # 说了反而是噪音。门槛定在增量5000亿：那已经是一家大公司的全部体量。
    if delta < 5e11:
        return None

    unit = "万亿" if cur_t >= 1 else "千亿"
    if cur_t >= 1:
        base = f"现市值 {cur_t:.2f} 万亿，到目标价对应 {imp_t:.2f} 万亿"
        add = f"需要再增加 {dl_t:.2f} 万亿"
    else:
        base = f"现市值 {mv/1e8:,.0f} 亿，到目标价对应 {implied/1e8:,.0f} 亿"
        add = f"需要再增加 {delta/1e8:,.0f} 亿"
    return f"{base}——{add}。百分比看着不大，绝对量是这个规模，自己掂量。"


def _analyst_target(symbol: str, market: str, last: float) -> float | None:
    """机构一致预期目标价。注意这是 12 个月目标，不是短线目标。"""
    try:
        view = advisor._analyst_view_text(symbol, market, last) or ""
    except Exception:
        return None
    import re
    m = re.search(r"目标价均值\s*([\d,.]+)", view)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
        return v if v > 0 else None
    except Exception:
        return None


def _ma20_falling(symbol: str, market: str) -> bool | None:
    """20日均线是不是在往下走。拿不到数据返回 None（不当成"在跌"）。

    方向用"今天的MA20 vs 5个交易日前的MA20"，跟 advisor._recent_structure_text
    同一套口径，两边对同一支票的结论必须一致。
    """
    try:
        import datetime as _d
        _e = _d.date.today()
        _s = _e - _d.timedelta(days=90)
        df = ds.get_stock_history(symbol, _s.isoformat(), _e.isoformat(), "d", market)
        if df is None or getattr(df, "empty", True):
            return None
        col = "收盘" if "收盘" in df.columns else "close"
        closes = df[col].astype(float).tolist()
        if len(closes) < 25:
            return None
        return (sum(closes[-20:]) / 20) < (sum(closes[-25:-5]) / 20)
    except Exception:
        return None


def _target_price(symbol: str, market: str, last: float,
                  atr: float | None,
                  windows: tuple[int, ...] | None = None) -> tuple[float | None, str]:
    """短线目标价：5个交易日内能摸到的位置。

    2026-09-06 用户指出的口径错配：之前直接用机构一致预期当目标价，但那
    普遍是 12 个月目标，而他做的是 1-4 周短线。用一年期目标算盈亏比，
    等于把一年的空间记在四周的账上，赔率被系统性高估。

    英伟达那条是典型：目标价 325.23 隐含市值要从 5.55 万亿涨到 7.84 万亿，
    也就是四周内凭空多出 2.3 万亿——约等于再造一个台积电加一个特斯拉。
    这显然不是四周能发生的事，但清单上"盈亏比 7.0:1、胜率超过 12% 就够"
    让它看起来像个短线好机会。用户一眼看出了这个问题。

    改用技术阻力位，因为短线价格走到哪里由供需决定，不由估值决定：

      优先  近期高点（20日/60日里第一个还在现价上方的）。那是真实成交过
            的密集区，也是最可能出现抛压的位置。
      兜底  现价 + 2倍ATR。已经突破所有近期高点时用——这时没有历史阻力
            可参考，只能按"这只票平常两天能走多远"外推。

    机构目标价不丢弃，另外单独显示并标注 12 个月口径，让用户知道长期空间
    在哪，但不拿它算短线赔率。
    """
    hi5 = hi10 = hi20 = lo20 = None
    try:
        import datetime as _d
        _e = _d.date.today()
        _s = _e - _d.timedelta(days=60)
        df = ds.get_stock_history(symbol, _s.isoformat(), _e.isoformat(), "d", market)
        if df is not None and not getattr(df, "empty", True):
            col = "最高" if "最高" in df.columns else "high"
            highs = df[col].tolist()
            if len(highs) >= 5:
                hi5 = max(highs[-5:])
            if len(highs) >= 10:
                hi10 = max(highs[-10:])
            if len(highs) >= 20:
                hi20 = max(highs[-20:])
            lowcol = "最低" if "最低" in df.columns else "low"
            if lowcol in df.columns and len(df) >= 20:
                lo20 = min(df[lowcol].tolist()[-20:])
    except Exception:
        pass

    # 2026-09-06 用户把持有周期从"1-4周"缩到"5-10天，甚至1-2天超短期"。
    # 阻力位的选择要跟着缩：20日高点是四周才可能摸到的位置，用它给一周的
    # 交易算目标，等于把四周的空间记在一周的账上——跟之前拿12个月机构目标
    # 算短线赔率是同一类错误，只是尺度小一些。
    #
    # 改成从近到远逐级找第一个还在现价上方的阻力：5日高点（超短线能到的）、
    # 10日高点（一到两周）、20日高点（兜底）。至少高出 0.5% 才算有空间，
    # 否则等于"目标就是现价"。
    # 阻力位要"够远"才算数：至少高出1倍ATR。原来的门槛是 +0.5%，但典型
    # ATR是2-4%——一个离现价只有0.6%的高点，一天之内就会被穿过去，它不是
    # 5-10天的天花板，是日内噪音。当天实测汇丰控股目标只有 +0.6%、花旗
    # +0.6%、SK海力士 +0.8%，全都是这么来的，算出来的盈亏比 0.1~0.3 毫无
    # 意义。近处的阻力穿不过去时确实会变成压力，但那属于"进场后跌破止损"
    # 该处理的事，不该在算目标时就把天花板压到噪音水平。
    _near = (atr / last) if (atr and last) else 0.005
    pool = {5: (hi5, "5日高点"), 10: (hi10, "10日高点"), 20: (hi20, "20日高点")}
    for d in (windows or (5, 10, 20)):
        lvl, label = pool.get(d, (None, ""))
        if lvl and lvl > last * (1 + max(_near, 0.005)):
            return lvl, f"{label}·阻力"

    # 上方没有有效阻力（已突破近期高点）时，用量度幅度：前期整理区间的
    # 高度，从突破点往上投影。
    #
    # 2026-09-06 系统性审计的完整过程记在这里，因为中间那版是错的：
    #
    # 原来这里是"现价 + 1.5倍ATR"。而止损也是 1.5倍ATR，于是盈亏比恒等于
    # 1.0，永远过不了 2:1 的闸门——当天对分数最高的45支实测，盈亏比中位数
    # 0.74，只有1支达标，清单天天输出"今天没有机会"。
    #
    # 第一版改法是把目标改成 ATR×√持有天数（波动率按时间开方缩放）。方向
    # 对，结果错：止损和目标都成了 ATR 的倍数，盈亏比 = √N / 1.5 仍然是个
    # 常数，只是从 1.0 变成了 3.0——12支候选算出来的盈亏比一模一样。闸门
    # 从"全部拒绝"变成"全部放行"，同样没有鉴别力，而且那个数字是假的。
    #
    # 根子在于：ATR目标 ÷ ATR止损，横截面上不含任何信息。要让盈亏比能区分
    # 标的，目标必须来自这支票自己的价格结构。
    #
    # 量度幅度满足这个要求：突破前那段整理区间有多高，突破后就按这个高度
    # 往上投影。区间窄的票目标近、区间宽的票目标远，逐票不同。这也是技术
    # 分析里对突破最标准的目标推法，不是为了凑闸门临时发明的。
    #
    # 两道约束：区间高度不足1倍ATR时（几乎没整理过就直接拉上来）退回
    # ATR×√N，否则目标会贴在现价上；上限压在 4倍ATR 以内，防止某只票
    # 恰好有一段巨大的区间把目标推到不合理的位置。
    if atr and hi20 and lo20 is not None:
        span = hi20 - lo20
        if span >= atr:
            # 上限跟着持有期走，不再写死4倍。随机游走下N天的合理行程约
            # ATR×√N：5天≈2.2倍，2天≈1.4倍。持有期缩到5天以内之后还留着
            # 4倍上限，等于允许目标定在十几天才够得着的位置。
            _days = max(windows) if windows else 5
            _cap = max(_days ** 0.5, 1.4) * atr
            tgt = last + min(span, _cap)
            return tgt, f"量度幅度·20日区间高度{span / last * 100:.1f}%投影"
    if atr:
        days = max(windows) if windows else 10
        mult = round(days ** 0.5, 1)
        return last + mult * atr, f"现价+{mult}倍ATR·{days}天波动行程"
    return None, ""


def _build_item(rec: dict) -> dict | None:
    """把一条 AI 判断补全成可执行的一条。"""
    symbol, market = rec.get("symbol"), rec.get("market")
    if not symbol or not market:
        return None
    try:
        q = ds.get_stock_realtime_futu(symbol, market) or {}
    except Exception:
        q = {}
    last = q.get("最新价")
    if not last:
        return None

    atr = _atr(symbol, market)

    # 两档周期各算一组止损/目标/盈亏比/仓位。同一支票在不同持有周期下
    # 结论可以完全不同——刚冲高的票超短线该等回踩，但一周维度上趋势没坏，
    # 给一个模糊的中间答案不如把两档都摆出来让用户按自己的时间安排选。
    plans = []
    for label, atr_mult, wins in _HORIZONS:
        h_stop = last - atr_mult * atr if atr else last * 0.95
        h_stop_pct = (h_stop - last) / last * 100
        h_tgt, h_src = _target_price(symbol, market, last, atr, windows=wins)
        h_rr = ((h_tgt - last) / (last - h_stop)) if (h_tgt and h_stop < last) else None
        plans.append({
            "周期": label, "止损": round(float(h_stop), 3),
            "止损幅度": round(h_stop_pct, 1),
            "目标": round(float(h_tgt), 3) if h_tgt else None,
            "目标来源": h_src,
            "盈亏比": round(h_rr, 2) if h_rr else None,
        })

    # 主口径仍用短线档（5-10天），清单的仓位和买入区间按它算——那是用户
    # 的默认周期。超短线档作为附加信息展示。
    main = plans[-1]
    stop = main["止损"]
    stop_pct = main["止损幅度"]

    hi52, lo52 = q.get("52周最高"), q.get("52周最低")
    pos_pct = None
    if hi52 and lo52 and hi52 > lo52:
        pos_pct = (last - lo52) / (hi52 - lo52) * 100

    # ---- 目标价与盈亏比 ----
    target, t_src = main["目标"], main["目标来源"]
    analyst_t = _analyst_target(symbol, market, last)
    rr = None
    if target and target > last and stop < last:
        rr = (target - last) / (last - stop)

    # ---- 买入价位区间 ----
    #
    # 用户的问题很实际："开盘有的大升大降，我也不知道该在什么价位买"。
    # 清单里的现价是昨天收盘的，开盘跳空之后照着它买就错了。
    #
    # 上限有个干净的算法：盈亏比是买入价的函数——买得越高，到目标的空间
    # 越小、到止损的距离越大，盈亏比越差。设买入价 P、目标 T、止损 S，
    # 要求 (T-P)/(P-S) >= _MIN_RR，解出 P <= (T + _MIN_RR*S)/(1+_MIN_RR)。
    # 这就是"买到这个价以上，这笔交易的赔率就不值得做了"的分界线，
    # 不是拍脑袋定的心理价位。
    #
    # 下限用 20 日均线，但不低于止损位上方一点：太贴近止损的位置看着
    # 便宜，实际是买在"再跌一点就该认输"的地方，一个正常回踩就把你扫掉。
    buy_hi = buy_lo = None
    if target and stop < last:
        buy_hi = (target + _MIN_RR * stop) / (1 + _MIN_RR)
        ma20 = None
        try:
            import datetime as _d
            _e = _d.date.today()
            _s = _e - _d.timedelta(days=45)
            _df = ds.get_stock_history(symbol, _s.isoformat(), _e.isoformat(), "d", market)
            if _df is not None and not getattr(_df, "empty", True):
                col = "收盘" if "收盘" in _df.columns else "close"
                cl = _df[col].tolist()
                if len(cl) >= 20:
                    ma20 = sum(cl[-20:]) / 20
        except Exception:
            ma20 = None
        floor = stop * 1.02          # 止损上方 2%，留出被扫的余量
        buy_lo = max(ma20, floor) if ma20 else floor
        if buy_lo >= buy_hi:
            # 均线已经高过赔率上限，说明这个位置本来就不便宜了
            buy_lo = None

    # ---- 仓位：按风险预算反推，再受集中度和整手约束 ----
    cap = _capital_cny()
    fx, cur = _fx_to_cny(market)
    shares = amount_cny = None
    execution_reason = None
    is_new_buy = rec.get("action") == "买入" and not (rec.get("shares") or 0)
    decision = risk_policy.validate_new_position(
        market=market, entry=last, stop=stop, target=target, reward_risk=rr,
    ) if is_new_buy else None
    if decision and not decision.allowed:
        execution_reason = "；".join(decision.reasons)
    # 每手股数先查真实值，查不到才退回默认。
    # 2026-09-07：原来直接用 _HK_LOT_FALLBACK(100)，而港股每手从1到10000都有，
    # 实测六支里四支是错的（MINIMAX 20股、携程 50股、小米和泡泡玛特 200股）。
    # 错的后果不是差一点：当天清单让用户"买入小米500股"，而小米每手200股，
    # 500不是整数倍，这个单在券商那边根本下不出去。
    lot = int(rec.get("lot_size") or 0)
    if not lot and market == "HK":
        try:
            lot = ds.get_hk_lot_size(symbol) or 0
        except Exception:
            lot = 0
    if not lot:
        lot = _HK_LOT_FALLBACK if market == "HK" else 1
    if cap > 0 and fx > 0 and stop_pct < 0 and (not decision or decision.allowed):
        # A new-buy size uses only an explicit risk profile.  Existing holding
        # reports retain their historical sizing display, but are not orders.
        risk_pct = decision.max_risk_per_trade_pct if decision else _RISK_PER_TRADE_PCT
        max_position_pct = decision.max_position_pct if decision else _MAX_POSITION_PCT
        risk_budget = cap * risk_pct / 100                     # 这笔最多亏多少人民币
        raw_amount = risk_budget / (abs(stop_pct) / 100)        # 反推仓位金额
        capped = min(raw_amount, cap * max_position_pct / 100)  # 集中度上限
        local_amount = capped / fx                              # 换成标的货币
        n = int(local_amount / last)
        if lot > 1:
            n = (n // lot) * lot                                # 港股按手取整
        if n > 0:
            shares = n
            amount_cny = round(n * last * fx, 0)
        else:
            # 算出0股不等于"不值得买"，而是"这个本金买不起最小单位"。
            # 港股一手100股、单价154港元就是1.4万人民币，已经超过全部本金。
            # 这两种情况对用户的意义完全不同：前者是"别买"，后者是"想买
            # 但买不起"，混在一起说会让他以为系统在拒绝一个好机会。
            one_unit_cny = lot * last * fx
            unit_label = f"每手{lot}股" if lot > 1 else "1股"
            cap_limit = cap * max_position_pct / 100
            if one_unit_cny > cap:
                why = f"总资金不够（{unit_label} 约 {one_unit_cny:,.0f} 元 > 本金 {cap:,.0f} 元）"
            else:
                # 买得起，但一个最小单位就突破了单笔集中度上限。这跟"买不起"
                # 是两回事，说反了用户会以为自己钱不够——他钱够，是风控在拦。
                why = (f"{unit_label} 约 {one_unit_cny:,.0f} 元，超过单笔上限 "
                       f"{cap_limit:,.0f} 元（本金的{max_position_pct:.0f}%）")
            execution_reason = why

    # 趋势闸门：20日均线向下就不做多头进场。
    #
    # 2026-09-06 用户问"为什么携程是跨过门槛的独苗"，查出来的答案很难看：
    # 携程之所以唯一达标，正因为它跌得最惨——20日均线向下、现价低于均线
    # 7.2%、处在20日区间1%分位、近5个交易日 -9.4%。而目标价取的是上方最近
    # 的高点，一支票只有先跌下来、头顶才会留下一个"很远的高点"当目标，于是
    # 算出 2.90:1 的漂亮赔率。同期贴着高点走的小米，10日高点只在 +2.5% 处，
    # 赔率 0.43。
    #
    # 也就是说这道"盈亏比≥2.5"的闸门，实质上是一台"谁刚崩过"的筛选器——
    # 穿着风控的外衣干抄底的活，而且跟打分那边的方向正好相反：打分刚改成
    # 惩罚下跌趋势（均线向下最高10分），携程只拿70分，闸门却把它推成第一。
    # 系统的两半在选相反的东西。
    #
    # 换成量度幅度目标也挡不住：携程区间宽、ATR大，算出来照样 2.67。所以
    # 这件事不能靠调目标算法解决，得是一条独立的规则——5-10天的多头窗口里
    # 不接下落的刀。想抄底要等止跌证据（放量企稳、跌破后快速收回），那是
    # 另一套判断，不该混在"赔率够不够"里。
    #
    # 不是直接删掉，而是标成不可执行并说明原因：用户仍然能在观察档看到它，
    # 知道系统看见了这支票、也知道为什么没推。
    _falling = _ma20_falling(symbol, market)
    _trigger = None
    if _falling:
        # 不禁掉，单独分档并给出触发条件。
        #
        # 第一版我直接标成"不可执行、赔率再好也不接刀"，用户反问"抄底不是
        # 挺好的吗，你在担心什么"——他是对的，抄底是正当策略，我不该单方面
        # 把它整个关掉。担心的应该是三件具体的事，不是这个想法本身：
        #
        # 1) 那个赔率是循环论证出来的。分子来自"回到10日高点366"，而这个
        #    高点是崩盘之前留下的——跌得越狠，头顶的高点越远，赔率越漂亮。
        #    赔率是由下跌本身制造的，不是由任何"会涨回去"的证据支撑的。
        # 2) 时间窗口对不上。均值回归可能发生，但没有理由恰好在5-10天内
        #    发生，而系统对"什么时候"没有任何判断。
        # 3) 真正的问题是进场时点：清单会让用户当下按现价买，而抄底的正确
        #    做法是等止跌证据，不是在下落途中接。
        #
        # 所以给触发价：收盘重新站上5日高点，才算跌势暂停。在那之前这支票
        # 留在单独一档里，用户看得见、也知道等什么。触发价用5日高点而不是
        # MA20：MA20在下跌趋势里往往还在很上方（携程的MA20比现价高7.2%），
        # 等到那里赔率早就变了；5日高点是"最近这几天的卖压被吃掉了"这个
        # 事实的最低门槛。
        try:
            import datetime as _d
            _e2 = _d.date.today()
            df2 = ds.get_stock_history(symbol, (_e2 - _d.timedelta(days=30)).isoformat(),
                                       _e2.isoformat(), "d", market)
            if df2 is not None and not getattr(df2, "empty", True):
                hcol = "最高" if "最高" in df2.columns else "high"
                _trigger = round(float(max(df2[hcol].tolist()[-5:])), 2)
        except Exception:
            _trigger = None

    # 规模检查针对机构那个12个月目标——短线目标通常只有几个点的空间，
    # 算隐含市值没有意义。
    reality = _reality_check(symbol, market, last, analyst_t)

    item = {
        "代码": symbol, "市场": market, "名称": rec.get("name") or symbol,
        "规模提示": reality,
        "两档": plans,
        "方向": rec.get("action"), "评分": rec.get("score"),
        "现价": round(float(last), 3),
        "止损参考": round(float(stop), 3),
        "止损幅度": round(stop_pct, 1),
        "目标价": round(float(target), 2) if target else None,
        "买入上限": round(float(buy_hi), 3) if buy_hi else None,
        "买入下沿": round(float(buy_lo), 3) if buy_lo else None,
        "目标来源": t_src,
        "机构12月目标": round(float(analyst_t), 2) if analyst_t else None,
        "盈亏比": round(rr, 2) if rr else None,
        "建议股数": shares,
        "建议金额CNY": amount_cny,
        "不可执行原因": execution_reason,
        "新开仓状态": "仅观察" if decision and not decision.allowed else "可执行",
        "下跌趋势": bool(_falling),
        "止跌触发价": _trigger,
        "最小单位金额CNY": round(lot * last * fx, 0) if fx > 0 else None,
        "每手": lot,
        "ATR20": round(float(atr), 3) if atr else None,
        "日均波幅": round(atr / last * 100, 1) if atr else None,
        "52周分位": round(pos_pct, 0) if pos_pct is not None else None,
        "依据": (rec.get("fundamental_verdict") or "")[:400],
    }
    return item


def _rank_candidates(items: list[dict]) -> list[dict]:
    """达标的全部保留、按盈亏比降序；不达标的只留分数最高的几条作观察。"""
    ok, weak = [], []
    for x in items:
        rr = x.get("盈亏比")
        (ok if (rr is not None and rr >= _MIN_RR) else weak).append(x)
    ok.sort(key=lambda x: -(x.get("盈亏比") or 0))
    weak.sort(key=lambda x: -(x.get("评分") or 0))
    return ok + weak[:_MAX_ITEMS]


def build_plan(email: str | None = None) -> dict:
    email = email or advisor._EMAIL
    today = dt.date.today().isoformat()

    # AI 可用性探测。放在最前面是因为它决定清单该怎么被读——评分是旧的
    # 时候，用户需要知道是"还在跑"还是"跑不了"，这两种情况的应对完全不同。
    # 探测只花一次极小的调用，比让用户对着旧评分下单便宜得多。
    ai_status = "正常"
    try:
        advisor.chat_with_failover(
            [{"role": "user", "content": "ok"}],
            max_tokens=4, temperature=0, timeout=25, tag="plan-probe")
    except Exception as e:
        msg = str(e)
        if "quota" in msg.lower() or "balance" in msg.lower() or "余额" in msg or "配额" in msg:
            ai_status = "AI 供应商额度用尽，今天的判断跑不出来（需要充值）"
        else:
            ai_status = f"AI 调用失败：{type(e).__name__}"

    # 一、验证状态。放在最前面构造，因为它决定这份清单该怎么被读。
    verify = expectancy.summary_text()
    band = expectancy.score_band_expectancy()
    verified = band.get("状态") == "已验证"

    # 二、候选来源。持仓单独拿出来是因为它的动作性质不同——持仓给的是
    # "要不要减/清"，观察池给的是"要不要进"，混在一起用户没法分辨哪条
    # 是在说他已经有的仓位。
    items_watch, items_pos = [], []
    lb_date = None
    try:
        # 取 _POOL_SIZE 支进来，不是 _MAX_ITEMS 支。
        #
        # 2026-09-06 系统性审计查出的致命缺陷：这里原来传的是 _MAX_ITEMS(8)，
        # 也就是"先按分数砍到前8，再过盈亏比闸门"。而打分是奖励上升趋势中
        # 的强势股的，强势股必然贴近近期高点、上方空间小——所以分数最高的
        # 那8支，恰恰是盈亏比最差的8支。
        #
        # 当天实测：对分数最高的45支算盈亏比，中位数 0.74，只有1支达到2:1
        # ——而那1支是携程(2.90:1)，排在第39位，永远进不了前8。清单于是
        # 天天输出"今天没有盈亏比达标的机会"，用户拿不到任何可执行的东西。
        #
        # 这跟用户要的数学期望是直接冲突的：期望 = 胜率×平均盈利 + 败率×
        # 平均亏损，分数只是胜率的代理，盈亏比是赔率。只按分数排序等于把
        # 期望公式砍掉了赔率那一半，只留胜率。正确做法是让整个合格池都过
        # 一遍闸门，再在能做的票里挑分数高的。
        lb = tracker.get_latest_leaderboard(limit=_POOL_SIZE, source="watchlist")
        # 评分的批次日期。清单要在开盘前推，而产出评分的 advisor 是同一个
        # 早晨才开始跑的（06:30 起，实测整轮要 50 分钟以上）——如果那时
        # watchlist 那一段还没跑完，这里读到的就是昨天的分数。
        #
        # 读到旧分数本身不算错，市场没开盘时昨天收盘后的判断依然有效。
        # 真正危险的是用户不知道它是旧的：他会以为这是今早刚算出来的，
        # 从而对一个可能已经被隔夜消息推翻的判断下单。所以把批次日期
        # 原样印在清单上，隔天了就明确说出来。
        lb_date = (lb or {}).get("run_date")
        for r in (lb or {}).get("leaderboard", []):
            if (r.get("score") or 0) >= _MIN_SCORE:
                it = _build_item(r)
                if it:
                    items_watch.append(it)
    except Exception as e:
        print(f"[plan] 观察池读取失败: {e}")

    try:
        # get_position_advice 一次返回 {symbol: 最近一条判断}，不是逐支查。
        advs = tracker.get_position_advice(email)
        for p in tracker.get_positions(email):
            if (p.get("shares") or 0) <= 0:
                continue
            adv = advs.get(p["symbol"])
            if not adv:
                continue
            it = _build_item({**p, **adv})
            if it:
                it["持仓中"] = True
                items_pos.append(it)
    except Exception as e:
        print(f"[plan] 持仓判断读取失败: {e}")

    return {
        "日期": today,
        "生成时间": dt.datetime.now(dt.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M"),
        "验证状态": verify,
        "已验证": verified,
        "资金规模": _capital_cny(),
        "AI状态": ai_status,
        "评分批次": lb_date,
        "评分是否当天": (lb_date == today) if lb_date else None,
        # 排序和截断：达标的全留，不达标的按赔率排序后只留 _MAX_ITEMS 条观察。
        #
        # 原来是 items_watch[:_MAX_ITEMS]，而 items_watch 是按分数降序的，
        # 于是"分数前8"这道砍在闸门之后又砍了一遍——就算池子扩大了，达标
        # 的票只要分数不在前8，照样会在这里被切掉。
        #
        # 达标的不设上限：用户明确说"门槛可以设高一点，就算达标的只有一两只
        # 也可以，我们要找最优解"。达标的票本来就稀少（当天45支里只有1支），
        # 稀少的东西没有截断的必要，反倒是漏掉一支就少一个机会。
        #
        # 达标组内按盈亏比降序，不按分数：在胜率还没被验证之前（期望值那套
        # 现在的状态是"尚未验证"），盈亏比是唯一一个跟期望值有确定关系的量
        # ——赔率2:1时胜率超过33%就是正期望，3:1时25%就够。分数只是胜率的
        # 代理，而这个代理的有效性恰恰是还没被证明的那件事。拿没验证的代理
        # 去给已验证的量排序，顺序就反了。
        "关注候选": _rank_candidates(items_watch),
        "持仓处理": items_pos,
    }


def build_market_plan(market: str, email: str | None = None, top_n: int = 3) -> dict:
    """单市场版的build_plan，给09:00/21:00盘前Top3推荐用——港股/美股分开出
    报告，不是从三市场混排的清单里各挑几支凑数。

    2026-09-08新增。不改build_plan本身，跟它是平行入口：读的leaderboard
    source是advisor.judge_market_watchlist()写的source=f"watchlist_
    {market.lower()}"，跟老的source="watchlist"（三市场混排）是独立批次。

    _MIN_RR门槛、_rank_candidates的排序/分组逻辑原样复用，不为了凑够
    top_n支就降低盈亏比标准——达标不足top_n支时，Top3就只列实际达标的
    数量，不能把不达标的标的伪装成正式推荐。
    """
    email = email or advisor._EMAIL
    today = dt.date.today().isoformat()

    ai_status = "正常"
    try:
        advisor.chat_with_failover(
            [{"role": "user", "content": "ok"}],
            max_tokens=4, temperature=0, timeout=25, tag="plan-probe")
    except Exception as e:
        msg = str(e)
        if "quota" in msg.lower() or "balance" in msg.lower() or "余额" in msg or "配额" in msg:
            ai_status = "AI 供应商额度用尽，今天的判断跑不出来（需要充值）"
        else:
            ai_status = f"AI 调用失败：{type(e).__name__}"

    items_watch, items_pos = [], []
    lb_date = None
    try:
        lb = tracker.get_latest_leaderboard(limit=_POOL_SIZE, source=f"watchlist_{market.lower()}")
        lb_date = (lb or {}).get("run_date")
        for r in (lb or {}).get("leaderboard", []):
            if (r.get("score") or 0) >= _MIN_SCORE:
                it = _build_item(r)
                if it:
                    items_watch.append(it)
    except Exception as e:
        print(f"[plan/{market}] 候选池读取失败: {e}")

    try:
        advs = tracker.get_position_advice(email)
        for p in tracker.get_positions(email):
            if p.get("market") != market or (p.get("shares") or 0) <= 0:
                continue
            adv = advs.get(p["symbol"])
            if not adv:
                continue
            it = _build_item({**p, **adv})
            if it:
                it["持仓中"] = True
                items_pos.append(it)
    except Exception as e:
        print(f"[plan/{market}] 持仓判断读取失败: {e}")

    ranked = _rank_candidates(items_watch)
    # ranked已经是"达标组(按盈亏比降序)+不达标观察组(按分数降序,最多_MAX_ITEMS条)"
    # 拼在一起的列表；达标组数量可能是0、可能远超top_n。Top3只从达标组里切，
    # 不达标的即使排在ranked前面也不算——但ranked本身就是达标组在前，所以
    # 直接切片，同时保留是不是"凑够了"这个信息给渲染层用。
    qualifying = [x for x in ranked if (x.get("盈亏比") or 0) >= _MIN_RR
                  and x.get("新开仓状态") == "可执行"]
    top3 = qualifying[:top_n]

    return {
        "市场": market,
        "日期": today,
        "生成时间": dt.datetime.now(dt.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M"),
        "资金规模": _capital_cny(),
        "AI状态": ai_status,
        "评分批次": lb_date,
        "评分是否当天": (lb_date == today) if lb_date else None,
        "候选池规模": len(items_watch),
        "达标数量": len(qualifying),
        f"{market}Top{top_n}": top3,
        "关注候选": ranked,
        "持仓处理": items_pos,
        # 完整候选池明细（不像"关注候选"那样把不达标的截到_MAX_ITEMS条），
        # 给mentor_scan.py盘中扫描用——它要盯的是整个50支候选池有没有
        # 谁重新回到买入区间/放量异动，不是只盯render_market_text展示的
        # 那几条，被截掉的候选一样可能在盘中冒出机会。
        "候选池明细": items_watch,
    }


def render_market_text(plan: dict, top_n: int = 3) -> str:
    """render_text的单市场简化版，专给09:00/21:00盘前Top3推送用——只讲
    这个市场的达标Top3+持仓，不重复render_text里三市场混排清单的完整
    分档说明（那份留给daily_plan.py原有的推送场景用）。
    """
    market = plan.get("市场", "")
    L = []
    L.append(f"{market}盘前推荐 · {plan.get('日期')}")
    L.append(f"资金规模 {plan.get('资金规模')} 元")
    L.append("")

    if plan.get("AI状态") != "正常":
        L.append(f"[注意] {plan['AI状态']}")
        L.append("")
    elif plan.get("评分是否当天") is False:
        L.append(f"[注意] 评分来自 {plan.get('评分批次')} 那一批，不是今早算的。")
        L.append("")

    pos = plan.get("持仓处理") or []
    if pos:
        L.append(f"持仓处理（{len(pos)}支）：")
        for i2, it in enumerate(pos, 1):
            L += _render_item_lines(it, i2)
            L.append("")
    else:
        L.append("持仓处理：当前空仓")
        L.append("")

    top_key = f"{market}Top{top_n}"
    top3 = plan.get(top_key) or []
    达标数 = plan.get("达标数量", 0)
    候选池 = plan.get("候选池规模", 0)
    if top3:
        L.append(f"{market}Top{len(top3)}（候选池{候选池}支，达标{达标数}支，盈亏比≥{_MIN_RR:g}）：")
        for i, it in enumerate(top3, 1):
            L += _render_item_lines(it, i)
            L.append("")
    else:
        blocked = [x for x in (plan.get("关注候选") or []) if x.get("新开仓状态") != "可执行"]
        if blocked:
            L.append(f"今天{market}不输出新开仓指令：风险档案尚未完整配置。候选仍在观察池，"
                     "但系统不会替你假定单笔风险或仓位上限。")
        else:
            L.append(f"今天{market}没有盈亏比达标的机会（候选池{候选池}支，达标0支）——"
                     f"不是没有票，是没有票同时满足盈亏比≥{_MIN_RR:g}这个门槛，宁可没有也不硬凑。")
    L.append("")
    L.append("仅供参考，不构成投资建议，请自行判断。")
    return "\n".join(L)


def _render_item_lines(x: dict, idx: int) -> list[str]:
    """单个候选/持仓条目的完整多行呈现——买入区间/仓位/两档止损目标/机构
    目标/规模提示/日均波幅，"能直接照着下单"那套格式。

    2026-09-08从render_text内部的_one闭包提取成模块级函数，好让
    render_market_text（市场专属Top3推送）复用同一套格式，不用维护
    两份重复的呈现逻辑。提取前后行为完全一致，只是不再是闭包。
    """
    seg = [f"{idx}. {x['名称']}（{x['代码']}·{x['市场']}）{x['方向']} {x['评分']}分"]
    seg.append(f"   昨收 {x['现价']}")
    # 买入区间放在最前面。用户开盘时最先要回答的问题是"现在这个价能不能
    # 下手"，不是"这票多少分"——分数已经在标题行了。
    if x.get("买入上限"):
        lo = x.get("买入下沿")
        if lo:
            seg.append(f"   买入区间 {lo} ~ {x['买入上限']}")
            seg.append(f"     低于 {lo} 更好，但那已经贴近止损，跌下去要想想是不是逻辑变了")
        else:
            seg.append(f"   买入上限 {x['买入上限']}（不设下沿：均线已高于赔率分界，"
                       f"这个位置本来就不便宜）")
        seg.append(f"     高于 {x['买入上限']} 就别追了——那个价位盈亏比会跌破"
                   f"{_MIN_RR:g}:1，赔率不够")
    if x.get("建议股数"):
        seg.append(f"   买入 {x['建议股数']} 股（约 {x['建议金额CNY']:,.0f} 元）"
                   + (f"，每手{x['每手']}股" if x.get("每手", 1) > 1 else ""))
    elif x.get("不可执行原因"):
        seg.append(f"   新开仓：仅观察。{x['不可执行原因']}")
    else:
        seg.append("   仓位：算不出（缺资金规模或汇率），先不下单")

    # 两档周期并排。同一支票超短线和短线的赔率经常差很多——刚冲高的
    # 票超短线上方没空间，但一周维度上还有一段，分开看才知道该不该等。
    for h in (x.get("两档") or []):
        if h.get("盈亏比"):
            need = 100 / (1 + h["盈亏比"])
            seg.append(f"   [{h['周期']}] 止损 {h['止损']}（{h['止损幅度']}%）"
                       f" 目标 {h['目标']}（{h['目标来源']}）"
                       f" 盈亏比 {h['盈亏比']:.1f}:1，胜率>{need:.0f}%即正期望")
        elif h.get("目标"):
            seg.append(f"   [{h['周期']}] 止损 {h['止损']}（{h['止损幅度']}%）"
                       f" 目标 {h['目标']} — 赔率不足")
        else:
            seg.append(f"   [{h['周期']}] 上方无明确阻力，只做止损参考")
    # 机构目标单独一行并标明 12 个月口径。它跟上面的短线目标是两个
    # 时间尺度，并排放又不标注的话，人会默认它们说的是同一段时间。
    if x.get("机构12月目标"):
        up2 = (x["机构12月目标"] - x["现价"]) / x["现价"] * 100
        seg.append(f"   机构12个月目标 {x['机构12月目标']}（{up2:+.0f}%）"
                   "——长期空间，不是短线目标")
    if x.get("规模提示"):
        seg.append(f"   [规模] {x['规模提示']}")
    if x.get("日均波幅"):
        seg.append(f"   日均波幅 {x['日均波幅']}%"
                   + (f" · 52周分位 {x['52周分位']:.0f}%" if x.get("52周分位") is not None else ""))
    return seg


def render_text(plan: dict) -> str:
    """渲染成适合微信推送的纯文本。

    每条都写成"能直接照着下单"的形态：买多少股、多少钱、什么价位止损、
    什么价位是目标、盈亏比多少。用户要拿着它在汇丰操作，任何需要他自己
    再算一步的地方都是掉链子的机会。
    """
    cap = plan.get("资金规模") or 0
    L = [f"投研站 · {plan['日期']} 操作清单"]
    if cap:
        L.append(f"资金规模 {cap:,.0f} 元 · 单笔风险上限 {cap * _RISK_PER_TRADE_PCT / 100:,.0f} 元"
                 f"（{_RISK_PER_TRADE_PCT:.0f}%）")
    L.append("")

    lb_date = plan.get("评分批次")
    if lb_date and plan.get("评分是否当天") is False:
        L.append(f"[注意] 评分来自 {lb_date} 那一批，不是今早算的。")
        if plan.get("AI状态") and plan["AI状态"] != "正常":
            # 区分"还在跑"和"跑不了"。前者等一会儿就有，后者要充值才行，
            # 用户的应对完全不同。2026-09-06 四家供应商同时欠费时踩到：
            # 清单只说"评分不是当天的"，用户会以为再等等就好。
            L.append(f"原因：{plan['AI状态']}")
            L.append("")
            L.append("下面的价格、止损、目标位、盈亏比、仓位全部是今天现算的，")
            L.append("不经过 AI，照常可用。只有「评分」和「判断理由」是旧的。")
        else:
            L.append("今早的判断还在跑（06:30 启动，整轮约50分钟）。隔夜如果出了")
            L.append("重大消息，这批分数未必反映得进来——开盘前留意一下新闻。")
        L.append("")

    if not plan.get("已验证"):
        L.append("[尚未验证] 这套打分的数学期望还在积累样本，第一批可信数据")
        L.append("09-07 之后才有。所以下面每条都卡了盈亏比门槛：只列")
        L.append(f"盈亏比≥{_MIN_RR:g}的机会——赔率够高时，即使胜率只有"
                 f"{100/(1+_MIN_RR):.0f}%以上期望也是正的。")
        L.append("")

    pos = plan.get("持仓处理") or []
    if pos:
        L.append(f"一、持仓处理（{len(pos)}支）")
        for i2, x in enumerate(pos, 1):
            L += _render_item_lines(x, i2)
            L.append("")
    else:
        L.append("一、持仓处理：当前空仓")
        L.append("")

    watch = plan.get("关注候选") or []
    tradable, unaffordable, low_rr, dip = [], [], [], []
    for x in watch:
        rr_ok = (x.get("盈亏比") or 0) >= _MIN_RR
        if rr_ok and x.get("下跌趋势"):
            # 赔率够但处在下跌趋势——单独一档，等止跌信号，不混进可直接下单的
            dip.append(x)
        elif x.get("建议股数") and rr_ok:
            tradable.append(x)
        elif rr_ok and x.get("不可执行原因"):
            # 赔率够但本金不够。这类要单独列——它是"资金规模的约束"，
            # 不是"系统认为不该买"，用户加钱之后它们就能进第一档。
            unaffordable.append(x)
        else:
            low_rr.append(x)

    if tradable:
        L.append(f"二、可执行候选（{len(tradable)}支，盈亏比≥{_MIN_RR:g}）")
        for i2, x in enumerate(tradable, 1):
            L += _render_item_lines(x, i2)
            L.append("")
    else:
        L.append("二、可执行候选：今天没有盈亏比达标的机会")
        L.append("   不是没票可买，是没有赔率够高的位置。空仓也是一种决定。")
        L.append("")

    if unaffordable:
        L.append(f"三、赔率够但仓位约束进不去（{len(unaffordable)}支）")
        for x in unaffordable:
            L.append(f"   {x['名称']}（{x['代码']}）{x['评分']}分 现价{x['现价']} "
                     f"盈亏比{x['盈亏比']:.1f}")
            L.append(f"      {x['不可执行原因']}")
        L.append("   这几支不是不该买，是被本金规模或集中度上限挡住了。")
        L.append("   资金加上去之后它们会自动进第二档。")
        L.append("")

    if dip:
        L.append(f"三点五、抄底候选（{len(dip)}支，赔率够但还在下跌趋势里）")
        for x in dip:
            L.append(f"   {x['名称']}（{x['代码']}）{x['评分']}分 现价{x['现价']} "
                     f"盈亏比{x.get('盈亏比') or 0:.1f}")
            if x.get("止跌触发价"):
                up = (x["止跌触发价"] - x["现价"]) / x["现价"] * 100
                L.append(f"      触发价 {x['止跌触发价']}（+{up:.1f}%）：收盘站上5日高点"
                         f"才算跌势暂停，在那之前不进")
            else:
                L.append("      20日均线向下，等站稳再说")
        L.append("   这一档的赔率是跌出来的——跌得越狠，头顶那个高点越远，")
        L.append("   算出来的赔率越漂亮，但那不是\"会涨回去\"的证据。抄底可以做，")
        L.append("   要等止跌信号再进，别在下落途中接。")
        L.append("")

    if low_rr:
        L.append(f"四、短线赔率不足（{len(low_rr)}支，仅供观察）")
        for x in low_rr:
            # 把上方空间和止损距离都摆出来，而不是只说"赔率不够"。
            # 用户要能自己判断是"位置不好"还是"止损设得太宽"——前者该等，
            # 后者可以调参数。只给一个结论他没法分辨。
            if x.get("目标价") and x.get("止损幅度"):
                up = (x["目标价"] - x["现价"]) / x["现价"] * 100
                L.append(f"   {x['名称']}（{x['代码']}）{x['评分']}分 现价{x['现价']}")
                L.append(f"      上方空间 +{up:.1f}%（到{x['目标来源']} {x['目标价']}）"
                         f" vs 止损 {x['止损幅度']}% → 盈亏比 {x.get('盈亏比') or 0:.1f}:1")
            else:
                L.append(f"   {x['名称']}（{x['代码']}）{x['评分']}分 现价{x['现价']} "
                         "上方无明确阻力位")
        L.append("")
        L.append("   这一档不是票不好，是现在这个位置上方空间不够覆盖止损距离。")
        L.append("   多数是因为股价已经贴近近期高点——评分高恰恰是因为它一路涨上来。")
        L.append("   等回调到买入区间下沿，同样的票赔率就够了。")
        L.append("")

    L.append("—— 关于这份清单怎么用 ——")
    L.append(f"买入上限是算出来的不是估的：买得越高，到目标的空间越小、到止损")
    L.append(f"的距离越大，赔率越差。上限就是盈亏比正好跌到 {_MIN_RR:g}:1 的那个价，")
    L.append("超过它这笔就不值得做。开盘跳空高开时尤其要看这条线。")
    L.append("")
    L.append(f"仓位是按“单笔最多亏 {_RISK_PER_TRADE_PCT:.0f}% 本金”反推的：止损越远仓位越小，")
    L.append("所以不同标的的金额不一样，不是随便给的。单笔不超过总资金")
    L.append(f"{_MAX_POSITION_PCT:.0f}%。止损用20日ATR两倍——跌破说明发生的不是日常波动。")
    L.append("")
    L.append("注意：上面的价格是昨收，开盘可能跳空。别挂昨收价，看实际开盘价")
    L.append("落在区间哪个位置再决定。")
    L.append("")
    L.append(plan.get("验证状态", ""))
    return "\n".join(L)


def main() -> int:
    advisor._load_secrets_into_env()
    plan = build_plan()
    text = render_text(plan)
    print(text)
    out = __import__("pathlib").Path(__file__).resolve().parent / "data" / "daily_plan.json"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        print(f"[plan] 落盘失败: {e}")
    return 0


def main_market(market: str) -> int:
    """09:00/21:00盘前Top3推荐的CLI入口。跟main()是平行关系，不改main()
    本身——落盘到独立文件（data/daily_plan_hk.json / daily_plan_us.json），
    给mentor_scan.py盘中扫描读，不跟三市场混排的data/daily_plan.json混用，
    两份快照各自独立更新、互不覆盖。
    """
    advisor._load_secrets_into_env()
    plan = build_market_plan(market)
    text = render_market_text(plan)
    print(text)
    out = (__import__("pathlib").Path(__file__).resolve().parent / "data"
           / f"daily_plan_{market.lower()}.json")
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        print(f"[plan/{market}] 落盘失败: {e}")
    return 0


if __name__ == "__main__":
    if "--market" in sys.argv:
        _mi = sys.argv.index("--market")
        _market = sys.argv[_mi + 1].upper() if _mi + 1 < len(sys.argv) else ""
        if _market not in ("HK", "US"):
            print(f"[plan] --market 只支持 HK/US，收到: {_market!r}")
            code = 1
        else:
            code = main_market(_market)
    else:
        code = main()
    # 富途SDK线程不是daemon线程，不强制退出会挂住；os._exit 跳过stdout
    # 缓冲刷新，管道输出会丢，所以必须先flush（项目老坑）。
    sys.stdout.flush()
    __import__("os")._exit(code)
