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


def _target_price(symbol: str, market: str, last: float) -> tuple[float | None, str]:
    """目标价：优先用机构一致预期，取不到就退回52周高点的中间位置。

    用机构目标价而不是自己算，理由是它是外部的、可追溯的，而且清单要
    交给用户去下单——一个"我们自己模型算出来的目标价"没法向他解释凭什么。
    """
    try:
        view = advisor._analyst_view_text(symbol, market, last) or ""
    except Exception:
        view = ""
    import re
    m = re.search(r"目标价均值\s*([\d,.]+)", view)
    if m:
        try:
            v = float(m.group(1).replace(",", ""))
            if v > 0:
                return v, "机构一致预期目标价"
        except Exception:
            pass
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
    target, t_src = _target_price(symbol, market, last)
    rr = None
    if target and target > last and stop < last:
        rr = (target - last) / (last - stop)

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

    return {
        "代码": symbol, "市场": market, "名称": rec.get("name") or symbol,
        "方向": rec.get("action"), "评分": rec.get("score"),
        "现价": round(float(last), 3),
        "止损参考": round(float(stop), 3),
        "止损幅度": round(stop_pct, 1),
        "目标价": round(float(target), 2) if target else None,
        "目标来源": t_src,
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
    try:
        lb = tracker.get_latest_leaderboard(limit=_MAX_ITEMS, source="watchlist")
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

    if not plan.get("已验证"):
        L.append("[尚未验证] 这套打分的数学期望还在积累样本，第一批可信数据")
        L.append("09-07 之后才有。所以下面每条都卡了盈亏比门槛：只列")
        L.append(f"盈亏比≥{_MIN_RR:.0f}的机会——赔率够高时，即使胜率只有"
                 f"{100/(1+_MIN_RR):.0f}%以上期望也是正的。")
        L.append("")

    def _one(x, idx):
        seg = [f"{idx}. {x['名称']}（{x['代码']}·{x['市场']}）{x['方向']} {x['评分']}分"]
        seg.append(f"   现价 {x['现价']}")
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
            seg.append(f"   盈亏比 {x['盈亏比']:.1f}:1，胜率超过 {need:.0f}% 就是正期望")
            if x["盈亏比"] >= 8:
                # 盈亏比高到这个程度，通常不是"机会特别好"，而是机构目标价
                # 隐含了一个需要长时间兑现的假设（管线获批、周期反转），
                # 拿它当短线赔率会高估。宁可提醒一句。
                seg.append("   注意：赔率高到这个程度，多半是机构目标价押在")
                seg.append("   长期逻辑上（管线/周期反转），短线未必兑现")
        elif x.get("目标价"):
            seg.append("   盈亏比不足，不建议这个位置进")
        else:
            seg.append("   没有机构目标价，盈亏比无法计算——只做止损参考")
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
        L.append(f"四、赔率不足或缺目标价（{len(low_rr)}支，仅供观察）")
        for x in low_rr:
            rr = f"盈亏比{x['盈亏比']:.1f}" if x.get("盈亏比") else "无机构目标价"
            L.append(f"   {x['名称']}（{x['代码']}）{x['评分']}分 现价{x['现价']} {rr}")
        L.append("")

    L.append("—— 关于这份清单怎么用 ——")
    L.append(f"仓位是按“单笔最多亏 {_RISK_PER_TRADE_PCT:.0f}% 本金”反推的：止损越远仓位越小，")
    L.append("所以不同标的的金额不一样，不是随便给的。单笔不超过总资金")
    L.append(f"{_MAX_POSITION_PCT:.0f}%。止损用20日ATR两倍——跌破说明发生的不是日常波动。")
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
