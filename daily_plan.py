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

    return {
        "代码": symbol, "市场": market, "名称": rec.get("name") or symbol,
        "方向": rec.get("action"), "评分": rec.get("score"),
        "现价": round(float(last), 3),
        "止损参考": round(float(stop), 3),
        "止损幅度": round(stop_pct, 1),
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
        "关注候选": items_watch[:_MAX_ITEMS],
        "持仓处理": items_pos,
    }


def render_text(plan: dict) -> str:
    """渲染成适合微信推送的纯文本。"""
    L = [f"投研站 · {plan['日期']} 操作清单", ""]

    if not plan.get("已验证"):
        L.append("[重要] 这套打分的数学期望目前尚未验证完成——下面的内容是")
        L.append("筛选线索和执行参数参考，不是可以照着下单的结论。原因见文末。")
        L.append("")

    pos = plan.get("持仓处理") or []
    if pos:
        L.append(f"一、你的持仓（{len(pos)}支）")
        for x in pos:
            L.append(f"  {x['名称']}（{x['代码']}·{x['市场']}）{x['方向']} {x['评分']}分")
            L.append(f"    现价 {x['现价']}  止损参考 {x['止损参考']}（{x['止损幅度']}%）"
                     + (f"  日均波幅 {x['日均波幅']}%" if x.get("日均波幅") else ""))
        L.append("")

    watch = plan.get("关注候选") or []
    if watch:
        L.append(f"二、观察池候选（{len(watch)}支，评分≥{_MIN_SCORE}）")
        for x in watch:
            L.append(f"  {x['名称']}（{x['代码']}·{x['市场']}）{x['方向']} {x['评分']}分"
                     + (f"  52周分位{x['52周分位']:.0f}%" if x.get("52周分位") is not None else ""))
            L.append(f"    现价 {x['现价']}  止损参考 {x['止损参考']}（{x['止损幅度']}%）"
                     + (f"  日均波幅 {x['日均波幅']}%" if x.get("日均波幅") else ""))
        L.append("")

    if not pos and not watch:
        L.append("今天没有达到阈值的标的。空仓也是一种决定，不必为了有事做而下单。")
        L.append("")

    L.append("止损位取 20 日 ATR 的两倍——跌破它说明发生的不是日常波动。")
    L.append("不用固定百分比是因为不同标的波动率差几倍，同一个 5% 对有的票是")
    L.append("腰斩信号，对有的票开盘就会被扫到。")
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
