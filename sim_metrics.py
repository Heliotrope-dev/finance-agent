"""AI模拟盘的专业指标 —— 纯计算层，不碰 Streamlit 也不自己取数。

升级路线图第7条"AI模拟盘升级为AI基金经理"里的指标部分：年化收益、最大回撤、
夏普比率、换手率、胜率、平均持仓天数。

这个文件最重要的一条设计原则是**数据不够就不给数字**。

理由很具体：这个模拟盘 2026-09-12 刚重置成1万美金，写这段代码时数据库里只有
1个交易日的净值快照、4笔订单而且全是买入。这种情况下——
  - 年化收益：把1天的涨跌按252个交易日外推，+0.5%的一天会变成"年化+250%"；
  - 夏普比率：2个点算不出有意义的标准差；
  - 胜率：一笔平仓都没有。
照着公式硬算全都能算出数来，而且看起来很专业。但那是编的。这个项目的定位是
"亏的也放在里面，没有挑过"，在指标上编数字比不显示指标伤害大得多。

所以每个指标都带一个最小样本量门槛，不够就返回 None，由渲染层显示"还需要多少
个交易日"。
"""

from __future__ import annotations

import math
from collections import deque

import pandas as pd

_TRADING_DAYS = 252

# 年化收益和夏普至少要这么多个交易日。20≈一个自然月。
#
# 这个门槛不是统计上的"足够"（夏普比率要稳定下来通常需要一两年），而是
# "低于这个数连数量级都是错的"的下限。20天的年化仍然会放大约12.6倍，所以
# 渲染层还要额外标明"样本很短"，不能只靠这个门槛兜底。
_MIN_DAYS_ANNUAL = 20

# 最大回撤不需要那么多天——它描述的是"这段时间里实际经历过的最大一次下跌"，
# 哪怕只有一天的日内数据，"今天盘中最多回撤过多少"也是真实发生过的事实，
# 不涉及任何外推。只要有足够的点能看出起伏就行。
_MIN_POINTS_DRAWDOWN = 5


def _to_daily(points: list[dict], value_key: str = "assets_hkd",
              time_key: str = "run_at") -> pd.Series:
    """把盘中快照压成"每个交易日一个收盘净值"。

    这一步不能省。sim_snapshot 每5分钟落一个点，如果直接拿这些点算日收益率再
    乘 sqrt(252) 年化，等于把5分钟的波动当成一天的波动——夏普比率会被放大约
    sqrt(78) ≈ 8.8 倍（美股一天约78个5分钟）。这类错误算出来的数字很好看，
    而且不会报错，属于最难被发现的一类。

    取每天最后一个点作为当日收盘净值：快照只在开盘时段产生，当天最后一个点
    就是最接近收盘的那个。
    """
    if not points:
        return pd.Series(dtype=float)
    df = pd.DataFrame(points)
    if value_key not in df.columns or time_key not in df.columns:
        return pd.Series(dtype=float)
    df = df[[time_key, value_key]].dropna()
    if df.empty:
        return pd.Series(dtype=float)
    df[time_key] = pd.to_datetime(df[time_key], utc=True, errors="coerce")
    df = df.dropna(subset=[time_key]).sort_values(time_key)
    if df.empty:
        return pd.Series(dtype=float)
    s = pd.Series(df[value_key].astype(float).values,
                  index=pd.DatetimeIndex(df[time_key]))
    # 按北京时间分日：这个盘同时交易港股和美股，美股盘对应的是北京时间的深夜
    # 到凌晨。用 UTC 分日会把同一个美股交易日劈成两半（美东白天=UTC当天下午，
    # 但港股那天=UTC当天上午），而用北京时间至少能让港股这条线的"一天"是完整的。
    s.index = s.index.tz_convert("Asia/Shanghai")
    return s.groupby(s.index.date).last()


def compute(points: list[dict], start_capital: float,
            orders: list[dict] | None = None) -> dict:
    """算出一整套指标。points 就是页面上画净值曲线用的那份快照列表。

    返回的 dict 里，算不出来的指标不会出现（不是放 None 占位）——调用方用
    `"sharpe" in res` 判断，跟 portfolio_risk.analyze 是同一个约定。
    额外总是返回 `n_days`（有多少个交易日）和 `enough_for_annual`（够不够
    算年化类指标），让渲染层能说清楚"为什么这里没有数字"。
    """
    out: dict = {}
    daily = _to_daily(points)
    n_days = int(len(daily))
    out["n_days"] = n_days
    out["n_points"] = len(points or [])
    out["enough_for_annual"] = n_days >= _MIN_DAYS_ANNUAL
    out["min_days_for_annual"] = _MIN_DAYS_ANNUAL
    if not points:
        return out

    # ── 区间收益：任何时候都能算，也不涉及外推 ──────────────────────
    raw = pd.DataFrame(points)
    if "assets_hkd" in raw.columns and len(raw) >= 1:
        last_val = float(raw["assets_hkd"].iloc[-1])
        out["current_value"] = last_val
        if start_capital > 0:
            out["total_return"] = last_val / start_capital - 1.0

    # ── 最大回撤：用全部盘中点，不用日线 ────────────────────────────
    # 这里故意跟年化类指标相反地取更细的粒度：回撤问的是"最难受的时候有多
    # 难受"，那是盘中真实经历过的，压成日线会把日内的深坑抹平。
    if len(raw) >= _MIN_POINTS_DRAWDOWN and "assets_hkd" in raw.columns:
        nav = pd.Series(raw["assets_hkd"].astype(float).values)
        running_max = nav.cummax()
        dd = nav / running_max - 1.0
        out["max_drawdown"] = float(dd.min())

    # ── 年化 / 夏普 / 波动率：必须够天数 ────────────────────────────
    if n_days >= _MIN_DAYS_ANNUAL:
        rets = daily.pct_change().dropna()
        # 标准差要真的大于一个下限，不能只判 >0。净值曲线几乎不动的时候
        # （比如一整段时间满仓不动、快照之间只差几分钱），std 会趋近于0，
        # 夏普 = 均值/标准差 直接炸到天文数字——测试里用"每天恰好+1%"的
        # 合成数据跑出过 1.4e15。1e-6 相当于日波动万分之一，低于这个数
        # 说明这段净值本质上是条直线，夏普没有意义。
        if len(rets) >= 2 and float(rets.std()) > 1e-6:
            # 年化收益用几何口径（按实际经过的交易日数折算），不是简单
            # 把区间收益乘以 252/n——后者在收益为负时会算出小于-100%的数。
            growth = float(daily.iloc[-1] / daily.iloc[0])
            if growth > 0:
                out["annual_return"] = growth ** (_TRADING_DAYS / n_days) - 1.0
            out["annual_vol"] = float(rets.std()) * math.sqrt(_TRADING_DAYS)
            # 夏普假设无风险利率为0，并在渲染层写明。现在美债10年期接近5%，
            # 把它当0会系统性高估夏普；但要用真实无风险利率就得逐日对齐利率
            # 序列，对一个模拟盘来说不值当，写清楚假设比偷偷改口径好。
            out["sharpe"] = float(rets.mean()) / float(rets.std()) * math.sqrt(_TRADING_DAYS)
            out["risk_free_assumed"] = 0.0

    # ── 交易类指标 ──────────────────────────────────────────────────
    if orders:
        out.update(_trade_stats(orders, out.get("current_value") or start_capital, n_days))
    return out


def _trade_stats(orders: list[dict], avg_equity: float, n_days: int) -> dict:
    """成交笔数、换手率、平仓胜率、平均持仓天数。

    只认 status=="成功" 的记录：跳过和失败的不是交易，算进换手率会虚高。

    胜率和持仓天数需要"一买一卖配成一个回合"。这里用先进先出(FIFO)配对，
    跟券商算已实现盈亏是同一套规则。

    成交价来自 simulated_orders.fill_price，由 sim_trader.backfill_fill_prices()
    事后从富途回填（下市价单的那一刻还没成交，拿不到价）。没回填上价格的订单
    不参与盈亏统计但仍然计入笔数——宁可少一个指标，也不用"卖出价未知就当赚了"
    这种假设去凑数。
    """
    out: dict = {}
    done = [o for o in orders if str(o.get("status") or "") == "成功"]
    out["n_trades"] = len(done)
    if not done:
        return out

    buys = sum(1 for o in done if o.get("action") == "买入")
    out["n_buys"] = buys
    out["n_sells"] = len(done) - buys
    out["n_priced"] = sum(1 for o in done if (o.get("fill_price") or 0) > 0)

    # FIFO 配对出平仓回合
    lots: dict[str, deque] = {}
    holding_days: list[float] = []
    wins = 0
    scored = 0
    realized = 0.0
    traded_value = 0.0
    for o in sorted(done, key=lambda x: str(x.get("created_at") or "")):
        sym = str(o.get("symbol") or "")
        qty = float(o.get("shares_ordered") or 0)
        px = float(o.get("fill_price") or 0)
        ts = pd.to_datetime(o.get("created_at"), utc=True, errors="coerce")
        if qty <= 0 or pd.isna(ts):
            continue
        if px > 0:
            traded_value += qty * px
        if o.get("action") == "买入":
            lots.setdefault(sym, deque()).append([ts, qty, px])
        else:
            q = qty
            dq = lots.get(sym) or deque()
            while q > 0 and dq:
                open_ts, open_qty, open_px = dq[0]
                used = min(q, open_qty)
                holding_days.append((ts - open_ts).total_seconds() / 86400.0)
                # 两边都有成交价才算这一段的盈亏，否则只计持仓天数
                if px > 0 and open_px > 0:
                    pnl = (px - open_px) * used
                    realized += pnl
                    scored += 1
                    if pnl > 0:
                        wins += 1
                open_qty -= used
                q -= used
                if open_qty <= 0:
                    dq.popleft()
                else:
                    dq[0][1] = open_qty
    out["round_trips"] = len(holding_days)
    if holding_days:
        out["avg_holding_days"] = sum(holding_days) / len(holding_days)
    if scored:
        out["win_rate"] = wins / scored
        out["scored_round_trips"] = scored
        out["realized_pnl"] = realized

    # 换手率给"区间换手"（累计成交金额 / 平均净值），不年化。年化换手在样本
    # 只有几天时会得出"一年换手三百次"这种荒唐数字，跟年化收益是同一个坑。
    #
    # 注意成交金额是各自市场的本币（美股按美元、港股按港币），这个盘目前只做
    # 美股和港股，而展示口径统一是美元——港币金额直接加进来会把港股那部分放大
    # 约7.8倍。所以只在全部成交都是同一个市场时才给这个数，混市场就不给，
    # 等真的需要了再把汇率接进来。
    markets = {str(o.get("market") or "") for o in done if (o.get("fill_price") or 0) > 0}
    if traded_value > 0 and avg_equity and avg_equity > 0 and len(markets) <= 1:
        out["turnover"] = traded_value / avg_equity
        out["turnover_market"] = next(iter(markets), "")
    return out
