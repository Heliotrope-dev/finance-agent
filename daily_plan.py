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
import tracker

# 只有分数达到这条线的标的才进清单。65 分不是拍脑袋：低于它的判断在文本里
# 几乎都带着"数据不足/等待更明确信号"这类措辞，放进"今天该看什么"只会稀释
# 注意力。这个阈值等期望值验证出来之后应该按分段表现重新定。
_MIN_SCORE = 65

# 一份清单最多几条。超过这个数就不再是"清单"而是"又一个列表"了——人一天
# 能认真处理的决策数量有限，宁可漏掉边缘机会，也不要让真正值得看的那几条
# 被淹没。
_MAX_ITEMS = 8

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
_MIN_RR = 2.0

# 单笔最多亏总资金的百分之几。2% 是仓位管理里的常规值：连错 10 次总回撤
# 约 18%，账户还活着；用 10% 的话连错 5 次就腰斩，那时候即使策略是对的
# 也已经没有本金去等它兑现了。
_RISK_PER_TRADE_PCT = 2.0

# 单笔仓位占总资金的上限。风险预算算出来的仓位在止损很近时会非常大
# （风险200元 / 止损2% = 一万元，等于满仓），必须再加一道集中度约束。
_MAX_POSITION_PCT = 30.0

# 港股每手股数。仓位要按手取整，不然算出来的股数根本下不了单。
_HK_LOT_FALLBACK = 100


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


def _target_price(symbol: str, market: str, last: float,
                  atr: float | None) -> tuple[float | None, str]:
    """短线目标价：1-4周能摸到的位置。

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
    hi20 = hi60 = None
    try:
        import datetime as _d
        _e = _d.date.today()
        _s = _e - _d.timedelta(days=110)
        df = ds.get_stock_history(symbol, _s.isoformat(), _e.isoformat(), "d", market)
        if df is not None and not getattr(df, "empty", True):
            col = "最高" if "最高" in df.columns else "high"
            highs = df[col].tolist()
            if len(highs) >= 20:
                hi20 = max(highs[-20:])
            if len(highs) >= 60:
                hi60 = max(highs[-60:])
    except Exception:
        pass

    # 取第一个还在现价上方的阻力位——已经突破的高点不再是阻力。
    # 至少高出 0.5% 才算有空间，否则等于"目标就是现价"。
    for lvl, label in ((hi20, "20日高点"), (hi60, "60日高点")):
        if lvl and lvl > last * 1.005:
            return lvl, f"{label}·短线阻力"

    if atr:
        return last + 2 * atr, "现价+2倍ATR·已突破近期高点"
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
    # 止损：2倍ATR。拿不到ATR时退回8%——那是个明确标注为兜底的粗略值，
    # 不假装它跟ATR一样有依据。
    stop = last - 2 * atr if atr else last * 0.92
    stop_pct = (stop - last) / last * 100

    hi52, lo52 = q.get("52周最高"), q.get("52周最低")
    pos_pct = None
    if hi52 and lo52 and hi52 > lo52:
        pos_pct = (last - lo52) / (hi52 - lo52) * 100

    # ---- 目标价与盈亏比 ----
    target, t_src = _target_price(symbol, market, last, atr)
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
    lot = int(rec.get("lot_size") or 0) or (_HK_LOT_FALLBACK if market == "HK" else 1)
    if cap > 0 and fx > 0 and stop_pct < 0:
        risk_budget = cap * _RISK_PER_TRADE_PCT / 100          # 这笔最多亏多少人民币
        raw_amount = risk_budget / (abs(stop_pct) / 100)        # 反推仓位金额
        capped = min(raw_amount, cap * _MAX_POSITION_PCT / 100)  # 集中度上限
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
            cap_limit = cap * _MAX_POSITION_PCT / 100
            if one_unit_cny > cap:
                why = f"总资金不够（{unit_label} 约 {one_unit_cny:,.0f} 元 > 本金 {cap:,.0f} 元）"
            else:
                # 买得起，但一个最小单位就突破了单笔集中度上限。这跟"买不起"
                # 是两回事，说反了用户会以为自己钱不够——他钱够，是风控在拦。
                why = (f"{unit_label} 约 {one_unit_cny:,.0f} 元，超过单笔上限 "
                       f"{cap_limit:,.0f} 元（本金的{_MAX_POSITION_PCT:.0f}%）")
            rec["_不可执行原因"] = why

    # 规模检查针对机构那个12个月目标——短线目标通常只有几个点的空间，
    # 算隐含市值没有意义。
    reality = _reality_check(symbol, market, last, analyst_t)

    return {
        "代码": symbol, "市场": market, "名称": rec.get("name") or symbol,
        "规模提示": reality,
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
        "不可执行原因": rec.get("_不可执行原因"),
        "最小单位金额CNY": round(lot * last * fx, 0) if fx > 0 else None,
        "每手": lot,
        "ATR20": round(float(atr), 3) if atr else None,
        "日均波幅": round(atr / last * 100, 1) if atr else None,
        "52周分位": round(pos_pct, 0) if pos_pct is not None else None,
        "依据": (rec.get("fundamental_verdict") or "")[:400],
    }


def build_plan(email: str | None = None) -> dict:
    email = email or advisor._EMAIL
    today = dt.date.today().isoformat()

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
        lb = tracker.get_latest_leaderboard(limit=_MAX_ITEMS, source="watchlist")
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
        "评分批次": lb_date,
        "评分是否当天": (lb_date == today) if lb_date else None,
        "关注候选": items_watch[:_MAX_ITEMS],
        "持仓处理": items_pos,
    }


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
        L.append(f"[注意] 下面用的评分是 {lb_date} 那一批，不是今早刚算的。")
        L.append("今早的判断还在跑（06:30 启动，整轮约50分钟）。隔夜如果出了")
        L.append("重大消息，这批分数未必反映得进来——开盘前留意一下新闻。")
        L.append("")

    if not plan.get("已验证"):
        L.append("[尚未验证] 这套打分的数学期望还在积累样本，第一批可信数据")
        L.append("09-07 之后才有。所以下面每条都卡了盈亏比门槛：只列")
        L.append(f"盈亏比≥{_MIN_RR:.0f}的机会——赔率够高时，即使胜率只有"
                 f"{100/(1+_MIN_RR):.0f}%以上期望也是正的。")
        L.append("")

    def _one(x, idx):
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
                       f"{_MIN_RR:.0f}:1，赔率不够")
        if x.get("建议股数"):
            seg.append(f"   买入 {x['建议股数']} 股（约 {x['建议金额CNY']:,.0f} 元）"
                       + (f"，每手{x['每手']}股" if x.get("每手", 1) > 1 else ""))
        else:
            seg.append("   仓位：算不出（缺资金规模或汇率），先不下单")
        seg.append(f"   止损 {x['止损参考']}（{x['止损幅度']}%）")
        if x.get("目标价"):
            up = (x["目标价"] - x["现价"]) / x["现价"] * 100
            seg.append(f"   目标 {x['目标价']}（+{up:.1f}%，{x['目标来源']}）")
        if x.get("盈亏比"):
            need = 100 / (1 + x["盈亏比"])
            seg.append(f"   短线盈亏比 {x['盈亏比']:.1f}:1，胜率超过 {need:.0f}% 就是正期望")
        elif x.get("目标价"):
            seg.append("   算不出盈亏比（目标或止损缺一个）")
        else:
            seg.append("   上方没有明确阻力位，只做止损参考")
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

    pos = plan.get("持仓处理") or []
    if pos:
        L.append(f"一、持仓处理（{len(pos)}支）")
        for i2, x in enumerate(pos, 1):
            L += _one(x, i2)
            L.append("")
    else:
        L.append("一、持仓处理：当前空仓")
        L.append("")

    watch = plan.get("关注候选") or []
    tradable, unaffordable, low_rr = [], [], []
    for x in watch:
        rr_ok = (x.get("盈亏比") or 0) >= _MIN_RR
        if x.get("建议股数") and rr_ok:
            tradable.append(x)
        elif rr_ok and x.get("不可执行原因"):
            # 赔率够但本金不够。这类要单独列——它是"资金规模的约束"，
            # 不是"系统认为不该买"，用户加钱之后它们就能进第一档。
            unaffordable.append(x)
        else:
            low_rr.append(x)

    if tradable:
        L.append(f"二、可执行候选（{len(tradable)}支，盈亏比≥{_MIN_RR:.0f}）")
        for i2, x in enumerate(tradable, 1):
            L += _one(x, i2)
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
    L.append(f"的距离越大，赔率越差。上限就是盈亏比正好跌到 {_MIN_RR:.0f}:1 的那个价，")
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


if __name__ == "__main__":
    code = main()
    # 富途SDK线程不是daemon线程，不强制退出会挂住；os._exit 跳过stdout
    # 缓冲刷新，管道输出会丢，所以必须先flush（项目老坑）。
    sys.stdout.flush()
    __import__("os")._exit(code)
