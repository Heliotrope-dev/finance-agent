"""组合风险体检 —— 纯计算层，不碰 Streamlit 也不自己取数。

升级路线图第4条。出发点在文档里写得很直接：普通散户最大的问题往往不是选错
了某一只股票，而是整个组合押在同一个方向上——自选里的美光、英伟达、台积电、
海力士、闪迪外加几只三星/海力士的杠杆产品，其实全是同一个押注（存储和AI
芯片）。这种风险券商App不会主动提醒。

这里只做数学，取数和渲染都在外面：
- 取数在 app.py（复用已有的 get_stock_history / get_benchmark_history 缓存）
- 渲染在 app.py
- 这个文件可以脱离 Streamlit 单独跑测试

刻意不做的事：不返回任何"建议买/卖"的结论。这一层输出的是组合的客观结构
（暴露、相关性、波动率、VaR），判断留给 AI 那一层和用户自己。把统计量和
投资建议混在一个函数里，出了问题分不清是数学错了还是判断错了。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# 交易日年化系数。用252不用365：波动率是按交易日累积的，周末不开盘不产生波动。
_TRADING_DAYS = 252

# 算相关性/波动率至少要这么多个重叠交易日。60天是个折中：再少的话相关系数
# 的置信区间宽到没有意义（20个点算出来的0.8，真值可能在0.5~0.95之间），
# 再多则港美股节假日不同、inner join 之后经常凑不齐。
_MIN_DAYS = 60

# 高相关阈值。0.8不是随便定的：路线图里举的例子"美光/英伟达/台积电三者相关
# 系数0.85，实际上相当于一只股票"就在这个量级。0.8以上意味着两只票超过64%
# (r^2) 的日内波动可以互相解释，分散效果基本为零。
_HIGH_CORR = 0.8

# 单一标的/单一行业的集中度警戒线。
_CONCENTRATION_WARN = 0.35


def _align_returns(hist_by_symbol: dict[str, pd.Series]) -> pd.DataFrame:
    """把各标的的收盘价序列对齐成一张日收益率表。

    inner join 不是偷懒。港股和美股的交易日历不一样（各自的公众假期、还有
    美股的夏令时切换），outer join 之后会有大量 NaN；用 0 填是错的（那天不
    是"没涨没跌"，是"没开盘"），用前值填会凭空造出一串 0 收益，把波动率和
    相关性一起压低。只留两边都开盘的日子，样本少一点但每个点都是真的。
    """
    frame = {}
    for sym, s in hist_by_symbol.items():
        if s is None or len(s) < 2:
            continue
        s = pd.Series(s).dropna()
        s = s[~s.index.duplicated(keep="last")].sort_index()
        if len(s) >= 2:
            frame[sym] = s
    if len(frame) < 1:
        return pd.DataFrame()
    px = pd.DataFrame(frame).dropna(how="any")
    if len(px) < 2:
        return pd.DataFrame()
    return px.pct_change().dropna(how="any")


def _exposure(holdings: list[dict], key: str) -> dict[str, float]:
    """按某个字段汇总权重（占比，0~1）。"""
    total = sum(float(h.get("value") or 0.0) for h in holdings)
    if total <= 0:
        return {}
    agg: dict[str, float] = {}
    for h in holdings:
        k = (h.get(key) or "未知") or "未知"
        agg[k] = agg.get(k, 0.0) + float(h.get("value") or 0.0)
    return {k: v / total for k, v in sorted(agg.items(), key=lambda kv: -kv[1])}


def analyze(
    holdings: list[dict],
    hist_by_symbol: dict[str, pd.Series],
    benchmark_close: pd.Series | None = None,
    benchmark_name: str = "基准",
) -> dict:
    """组合体检。

    holdings: [{symbol, name, market, currency, value}]，value 必须已经换算到
              同一个币种（外面用 to_cny 换好再传进来），否则权重是错的。
    hist_by_symbol: {symbol: 收盘价 Series，index 是日期}
    benchmark_close: 基准指数收盘价 Series，用来算 Beta 和压力测试；给 None
                     就跳过这两项，其余照算。

    返回的 dict 里每一项都可能缺席（数据不够就不放），调用方按 key 存在与否
    判断——不返回占位的 0 或 None，那会被当成"算出来就是0"。
    """
    out: dict = {}
    holdings = [h for h in holdings if float(h.get("value") or 0.0) > 0]
    if not holdings:
        return out

    total = sum(float(h["value"]) for h in holdings)
    out["total_value"] = total
    out["n_holdings"] = len(holdings)

    # ── 1. 暴露度：不需要历史数据，只要有持仓就能算 ──────────────────
    out["market_exposure"] = _exposure(holdings, "market")
    out["currency_exposure"] = _exposure(holdings, "currency")
    if any(h.get("sector") for h in holdings):
        out["sector_exposure"] = _exposure(holdings, "sector")

    # 港币和美元实际是同一个敞口：港币从1983年起就挂钩美元（联系汇率制，
    # 7.75~7.85区间由金管局兑换保证维持）。把它们分开列会让人以为"我分散在
    # 两个币种"，其实汇率风险是同一个。这里额外给一个合并口径。
    cur = out["currency_exposure"]
    usd_linked = sum(v for k, v in cur.items() if k.upper() in ("USD", "HKD"))
    if usd_linked > 0:
        out["usd_linked_pct"] = usd_linked

    # 集中度：最大单一持仓 + HHI。HHI(赫芬达尔指数)=各权重平方和，取值
    # 1/n ~ 1：等权分散时等于1/n，全压一只时等于1。它比"最大持仓占比"更能
    # 反映整体结构——三只各33%和一只34%加六只11%，最大持仓差不多，但前者
    # 集中得多。
    weights_by_sym: dict[str, float] = {}
    for h in holdings:
        weights_by_sym[h["symbol"]] = weights_by_sym.get(h["symbol"], 0.0) + float(h["value"]) / total
    top_sym, top_w = max(weights_by_sym.items(), key=lambda kv: kv[1])
    out["concentration"] = {
        "top_symbol": top_sym,
        "top_weight": top_w,
        "hhi": float(sum(w * w for w in weights_by_sym.values())),
        "effective_n": 1.0 / float(sum(w * w for w in weights_by_sym.values())),
    }

    # ── 2. 以下全部需要历史价格 ────────────────────────────────────
    rets = _align_returns({s: h for s, h in (hist_by_symbol or {}).items()
                           if s in weights_by_sym})
    if rets.empty or len(rets) < _MIN_DAYS or rets.shape[1] < 1:
        out["insufficient_history"] = True
        out["n_days"] = int(len(rets))
        return out

    out["n_days"] = int(len(rets))
    cols = list(rets.columns)
    # 权重要按"实际参与计算的标的"重新归一。有的持仓拉不到历史（新股、冷门
    # 标的、接口临时抽风），如果还用原来的权重，加起来不到1，波动率和VaR
    # 会被系统性低估。归一之后口径变成"在能算的这部分里"，并且把覆盖率报出来
    # 让用户知道这次体检代表了多少仓位。
    w_raw = np.array([weights_by_sym[c] for c in cols], dtype=float)
    covered = float(w_raw.sum())
    out["coverage"] = covered
    out["missing_history"] = [s for s in weights_by_sym if s not in rets.columns]
    if covered <= 0:
        out["insufficient_history"] = True
        return out
    w = w_raw / covered

    # 相关性矩阵 + 高相关配对
    if len(cols) >= 2:
        corr = rets.corr()
        out["corr_matrix"] = corr
        pairs = []
        for i in range(len(cols)):
            for j in range(i + 1, len(cols)):
                r = corr.iloc[i, j]
                if pd.notna(r) and r >= _HIGH_CORR:
                    pairs.append((cols[i], cols[j], float(r)))
        pairs.sort(key=lambda t: -t[2])
        out["high_corr_pairs"] = pairs

    # 组合年化波动率
    cov = rets.cov() * _TRADING_DAYS
    port_var = float(w @ cov.values @ w)
    if port_var > 0:
        out["port_vol_annual"] = float(np.sqrt(port_var))
        # 加权平均单票波动率——拿来跟组合波动率对比，差额就是分散化实际省下
        # 来的风险。只报组合波动率的话，用户没有参照系不知道这个数是高是低。
        indiv_vol = rets.std() * np.sqrt(_TRADING_DAYS)
        wavg = float((pd.Series(w, index=cols) * indiv_vol).sum())
        out["weighted_avg_vol"] = wavg
        if wavg > 0:
            out["diversification_benefit"] = 1.0 - out["port_vol_annual"] / wavg

    # 历史VaR：直接取组合日收益的5%分位，不假设正态分布。金融收益的尾部比
    # 正态厚得多，用正态公式算出来的VaR在真正暴跌的日子会系统性偏小。
    port_ret = pd.Series(rets.values @ w, index=rets.index)
    out["var95_pct"] = float(np.percentile(port_ret, 5))
    out["var95_amount"] = out["var95_pct"] * total * covered
    # 条件VaR(ES)：超过VaR那些天的平均亏损，回答"真跌穿了大概会跌多少"。
    tail = port_ret[port_ret <= out["var95_pct"]]
    if len(tail) > 0:
        out["cvar95_pct"] = float(tail.mean())
        out["cvar95_amount"] = out["cvar95_pct"] * total * covered
    out["worst_day_pct"] = float(port_ret.min())

    # ── 3. Beta 与压力测试 ────────────────────────────────────────
    if benchmark_close is not None and len(benchmark_close) >= 2:
        b = pd.Series(benchmark_close).dropna()
        b = b[~b.index.duplicated(keep="last")].sort_index().pct_change().dropna()
        joined = pd.concat([port_ret.rename("_port"), b.rename("_bench")],
                           axis=1, join="inner").dropna()
        if len(joined) >= _MIN_DAYS:
            bv = float(joined["_bench"].var())
            if bv > 0:
                port_beta = float(joined["_port"].cov(joined["_bench"]) / bv)
                out["benchmark_name"] = benchmark_name
                out["beta_days"] = int(len(joined))
                out["port_beta"] = port_beta
                # 压力测试：基准跌10%，按Beta线性外推组合的变动。
                # 线性外推在极端行情下会低估损失（真正的暴跌里相关性趋近1、
                # Beta本身会抬高），这一点在渲染层要写出来，不能让用户把它
                # 当成保证的下限。
                out["stress_bench_down10"] = port_beta * -0.10
                # 每只票各自的Beta，用来指出"谁在放大整体风险"
                per = {}
                for c in cols:
                    j2 = pd.concat([rets[c].rename("_s"), b.rename("_b")],
                                   axis=1, join="inner").dropna()
                    if len(j2) >= _MIN_DAYS:
                        v2 = float(j2["_b"].var())
                        if v2 > 0:
                            per[c] = float(j2["_s"].cov(j2["_b"]) / v2)
                if per:
                    out["beta_by_symbol"] = per
    return out


def build_warnings(res: dict, name_by_symbol: dict[str, str] | None = None) -> list[str]:
    """把体检结果翻译成人话提示。

    只说事实和它的含义，不说"建议减仓"——仓位决定牵涉到用户的风险偏好、税、
    现金需求，这里没有这些信息。措辞上也避免制造恐慌：集中不等于错，很多
    人就是故意集中押注的，提示的作用是确保这是"想清楚之后的选择"而不是
    "不知不觉变成这样"。
    """
    names = name_by_symbol or {}
    def nm(s: str) -> str:
        return names.get(s, s)

    tips: list[str] = []
    conc = res.get("concentration") or {}
    if conc.get("top_weight", 0) >= _CONCENTRATION_WARN:
        tips.append(
            f"单一标的 {nm(conc['top_symbol'])} 占了 {conc['top_weight']:.0%}，"
            f"组合的涨跌主要由它一只决定。"
        )
    eff_n = conc.get("effective_n")
    if eff_n and res.get("n_holdings", 0) >= 3 and eff_n < res["n_holdings"] * 0.6:
        tips.append(
            f"名义上持有 {res['n_holdings']} 只，但按权重算的有效分散度只相当于 "
            f"{eff_n:.1f} 只——仓位集中在少数几只上。"
        )

    for a, b, r in (res.get("high_corr_pairs") or [])[:3]:
        tips.append(
            f"{nm(a)} 和 {nm(b)} 近 {res.get('n_days', 0)} 个交易日的相关系数 "
            f"{r:.2f}，同涨同跌，分开持有几乎没有分散效果。"
        )

    usd = res.get("usd_linked_pct")
    cur = res.get("currency_exposure") or {}
    if usd and usd > 0.9 and len(cur) > 1:
        tips.append(
            f"币种看起来分散在 {'/'.join(cur)}，但港币挂钩美元，实际约 "
            f"{usd:.0%} 的仓位是同一个美元敞口。"
        )

    mkt = res.get("market_exposure") or {}
    if mkt:
        top_m, top_mv = next(iter(mkt.items()))
        if top_mv >= 0.8 and len(mkt) > 1:
            tips.append(f"{top_mv:.0%} 的仓位在{top_m}市场，跨市场分散有限。")

    if "stress_bench_down10" in res:
        tips.append(
            f"压力测试：{res.get('benchmark_name','基准')}若下跌 10%，按 Beta "
            f"{res['port_beta']:.2f} 线性外推组合约 {res['stress_bench_down10']:.1%}。"
            f"真正的暴跌里相关性会趋近 1、Beta 本身也会抬高，这个数是下限不是上限。"
        )

    if "var95_pct" in res:
        tips.append(
            f"按过去 {res['n_days']} 个交易日的实际分布，95% 的日子里单日回撤不超过 "
            f"{abs(res['var95_pct']):.2%}；剩下 5% 的坏日子平均跌 "
            f"{abs(res.get('cvar95_pct', res['var95_pct'])):.2%}。"
        )

    cov = res.get("coverage")
    if cov is not None and cov < 0.95:
        tips.append(
            f"注意：只有 {cov:.0%} 的仓位拉到了足够的历史数据，上面的波动率和 "
            f"VaR 是按这部分算的。"
        )
    return tips
