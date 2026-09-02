"""AI模拟盘下单——把advisor.py生成的组合交易信号自动同步到富途OpenAPI的
SIMULATE模拟交易环境。

2026-09-01用户明确要求"让内置AI模拟买卖"（参照富途App自己的"模拟买入"按钮），
对接的是富途账号自带的SIMULATE模拟交易账户，不涉及真实资金。实测过（VPS上
拿真实账户验证)：SIMULATE环境下单不需要unlock_trade，跟REAL环境的交易解锁
密码要求不一样——这个模块不经手、也不需要任何交易密码。这是对advisor.py.
_parse_trade_signals文档字符串里"不接真实下单"那句话的一次明确调整，范围
仅限模拟盘，真实交易(TrdEnv.REAL)依然不做，代码里也不会出现REAL这个值。

默认关闭，用户在持仓页手动打开"AI自动模拟交易"开关(tracker.get_ai_sim_trading)
后，advisor.py每天17:30生成组合交易信号时才会调用这里的execute_simulated_trades。
"""
import futu as ft

import data_sources as ds
import tracker

_HOST, _PORT = "127.0.0.1", 11111

# 2026-09-01用户明确要求"这两个绑定的汇率固定数字就行不用每次去找"——
# AI模拟盘(get_agent_snapshot/sim_agent.py)统一用固定汇率，不再每次调用
# data_sources.get_fx_rate查实时值。USD/HKD用7.80合理（港币是联系汇率
# 制度，官方盯住美元浮动区间7.75-7.85，几十年没脱离过，固定值本来就比
# "实时查询"更稳）；CNY/HKD用1.085是近期大致中枢。只服务于这个模拟盘的
# 估算/记账，不追求分毫不差，也不影响持仓页那个用get_sim_snapshot()的
# 通用视图（那边继续用ds.to_cny的实时汇率，两套快照本来就是独立的）。
USD_HKD_RATE = 7.80
CNY_HKD_RATE = 1.085

_MARKET_TRD = {"HK": ft.TrdMarket.HK, "US": ft.TrdMarket.US, "A": ft.TrdMarket.CN}


def calc_fee_hkd(market: str, shares: float, price: float, action: str, usd_hkd_rate: float) -> float:
    """一笔交易的手续费(折HKD)——按富途官方公开费率算(佣金+平台使用费+
    政府/交易所各项代收费用)，港股见 https://www.futuhk.com/hans/support/topic2_335，
    美股见 https://www.futuhk.com/hans/support/topic2_283。

    只给sim_agent.py那条自主决策的虚拟现金台账(_settle_virtual_cash)用——
    持仓页那条每日信号交易走的是富途真实SIMULATE账户余额(get_sim_snapshot)，
    富途自己的模拟盘本身就会按真实费率扣账，不用在这边重复算一遍。
    A股不在自主agent操作范围内，给0兜底，不硬编一套A股费率。
    """
    if shares <= 0 or price <= 0:
        return 0.0
    amount_native = shares * price
    if market == "HK":
        commission = max(amount_native * 0.0003, 3.0)
        platform_fee = 15.0
        settlement_fee = amount_native * 0.000042
        stamp_duty = max(amount_native * 0.001, 1.0)
        trading_fee = max(amount_native * 0.0000565, 0.01)
        sfc_levy = max(amount_native * 0.000027, 0.01)
        frc_levy = amount_native * 0.0000015
        return commission + platform_fee + settlement_fee + stamp_duty + trading_fee + sfc_levy + frc_levy
    if market == "US":
        commission_usd = max(shares * 0.0049, 0.99)
        platform_fee_usd = max(shares * 0.005, 1.0)
        combined_usd = min(commission_usd + platform_fee_usd, amount_native * 0.005)
        settlement_fee_usd = shares * 0.003
        sec_fee_usd = max(amount_native * 0.0000206, 0.01) if action == "卖出" else 0.0
        taf_usd = min(max(shares * 0.000195, 0.01), 9.79) if action == "卖出" else 0.0
        total_usd = combined_usd + settlement_fee_usd + sec_fee_usd + taf_usd
        return total_usd * usd_hkd_rate
    return 0.0


def _a_share_prefix(symbol: str) -> str:
    """A股代码转富途SH./SZ.前缀——6开头是上交所，0/3开头是深交所。8/4开头
    理论上是北交所，富途模拟盘是否支持不确定，不特殊处理，交给下单接口
    自己报错（上层会catch住记成失败，不会导致整批信号中断）。"""
    if symbol.startswith("6"):
        return "SH"
    if symbol.startswith(("0", "3")):
        return "SZ"
    return "SH"


_MARKET_PREFIXES = ("HK.", "US.", "SH.", "SZ.")


def _strip_market_prefix(symbol: str) -> str:
    """去掉AI信号里可能带的市场前缀，返回纯代码。

    2026-09-02修真实bug：之前只有_to_futu_code内部临时去前缀来拼下单代码，
    去完之后这个"干净"的symbol没有回写给调用方——_execute_one记录到
    simulated_orders表的还是signal里原始未处理的symbol。结果生产库里
    同一支票（小米集团-W/量化派）的下单历史时而是"01810"时而是"HK.01810"，
    两种格式混着出现，因为AI每次生成信号时symbol字段里带不带前缀不固定。
    这个函数抽出来给_to_futu_code和_execute_one共用同一份"干净"symbol，
    保证下单代码和落库记录用的是同一个值。
    """
    for prefix in _MARKET_PREFIXES:
        if symbol.upper().startswith(prefix):
            return symbol[len(prefix):]
    return symbol


def _to_futu_code(symbol: str, market: str) -> str:
    # 真实故障(2026-09-01)：AI偶尔会在信号里把市场前缀也写进symbol字段本身
    # (比如给出"US.META"而不是"META")，advisor._parse_trade_signals不会
    # 帮忙剥掉这个前缀——这里如果直接拼"US."+symbol会拼出"US.US.META"这种
    # 不存在的代码，下单直接报"美股找不到"失败。这里防御性地先把symbol
    # 开头已经带着的市场前缀去掉，不管AI给没给前缀，最终拼出来的都是
    # 正确的单一前缀代码。
    symbol = _strip_market_prefix(symbol)
    if market == "HK":
        return f"HK.{symbol}"
    if market == "US":
        return f"US.{symbol}"
    return f"{_a_share_prefix(symbol)}.{symbol}"


def _get_sim_acc_id(trd) -> str | None:
    """拿这个市场下的SIMULATE账户id——优先选CASH账户（现金账户，没有杠杆/
    保证金这些额外语义，最贴近"如果照抄AI建议单纯买卖股票"的模拟场景），
    这个市场只有MARGIN账户（比如美股，富途默认就只给MARGIN模拟账户）就
    退而求其次用它。"""
    ret, data = trd.get_acc_list()
    if ret != ft.RET_OK or data.empty:
        return None
    sims = data[data["trd_env"] == "SIMULATE"]
    if sims.empty:
        return None
    cash = sims[sims["acc_type"] == "CASH"]
    row = cash.iloc[0] if not cash.empty else sims.iloc[0]
    return str(row["acc_id"])


def _get_lot_size(qot, code: str) -> int:
    """A股/港股必须按整手下单，AI算出来的shares是理论数字，不一定刚好是
    整手的倍数——下单前按实际lot_size取整，取整后是0就放弃这条信号（金额
    太小连一手都买不起，不该硬凑一手冲上去，也不该四舍五入拉到一手）。
    美股不按手交易，lot_size统一按1处理，不用查（省一次网络请求）。
    """
    if code.startswith("US."):
        return 1
    market = ft.Market.HK if code.startswith("HK.") else ft.Market.CN
    ret, data = qot.get_stock_basicinfo(market, ft.SecurityType.STOCK, code_list=[code])
    if ret != ft.RET_OK or data.empty:
        return 1
    return int(data.iloc[0]["lot_size"]) or 1


def _execute_one(qot, trd, email: str, market: str, acc_id: str, signal: dict) -> dict:
    # 先在这里统一去掉symbol可能带的市场前缀，后面下单代码(code)和落库记录
    # 用的都是同一个干净symbol，不会再出现同一支票记录时而带前缀时而不带的情况。
    symbol = _strip_market_prefix(signal["symbol"])
    name = signal.get("name", "")
    action = signal["action"]
    shares_signal = signal["shares"]
    code = _to_futu_code(symbol, market)

    lot = _get_lot_size(qot, code)
    shares_ordered = int(shares_signal // lot) * lot
    if shares_ordered <= 0:
        note = f"按每手{lot}股取整后不足一手，金额太小（AI给的股数是{shares_signal:g}）"
        tracker.log_simulated_order(email, symbol, name, market, action, shares_signal, 0, "", acc_id, "跳过", note)
        return {"symbol": symbol, "status": "跳过", "note": note}

    trd_side = ft.TrdSide.BUY if action == "买入" else ft.TrdSide.SELL
    # 用市价单(MARKET)，不用限价单——目的是验证"如果照抄AI建议会怎样"，限价
    # 单可能因为价格没设对而一直不成交，没法真实反映"如果执行了"的结果。
    # 实测过：MARKET订单price参数必须传0（不是留空/None），trd_env=SIMULATE
    # 不需要提前unlock_trade。
    ret, data = trd.place_order(
        price=0, qty=shares_ordered, code=code, trd_side=trd_side,
        order_type=ft.OrderType.MARKET, trd_env=ft.TrdEnv.SIMULATE, acc_id=int(acc_id),
    )
    if ret == ft.RET_OK:
        order_id = str(data.iloc[0]["order_id"]) if not data.empty else ""
        tracker.log_simulated_order(email, symbol, name, market, action, shares_signal, shares_ordered, order_id, acc_id, "成功")
        return {"symbol": symbol, "status": "成功", "shares": shares_ordered}
    note = str(data)
    tracker.log_simulated_order(email, symbol, name, market, action, shares_signal, 0, "", acc_id, "失败", note)
    return {"symbol": symbol, "status": "失败", "note": note}


def execute_simulated_trades(email: str, signals: list[dict]) -> list[dict]:
    """遍历advisor.py解析出的交易信号，只处理"买入"/"卖出"（"不动"没有对应
    的下单动作），逐条下单到对应市场的SIMULATE账户，每条不管成功失败都落
    一笔simulated_orders记录——失败（比如取整后不足一手、账户查不到、代码
    无效）就跳过那一条，不拿假数据硬凑，也不因为一条失败中断其它信号。
    按市场分组各开一个trade连接，用完就关，不用quote那套常驻worker线程
    （这里一天只跑一次，不需要那个复杂度）。
    """
    actionable = [s for s in signals if s.get("action") in ("买入", "卖出")]
    if not actionable:
        return []

    grouped: dict[str, list[dict]] = {}
    for s in actionable:
        grouped.setdefault(s["market"], []).append(s)

    qot = ft.OpenQuoteContext(host=_HOST, port=_PORT)
    results: list[dict] = []
    try:
        for market, market_signals in grouped.items():
            trd_market = _MARKET_TRD.get(market)
            if trd_market is None:
                for s in market_signals:
                    note = f"不支持的市场：{market}"
                    tracker.log_simulated_order(email, s["symbol"], s.get("name", ""), market, s["action"], s["shares"], 0, "", "", "跳过", note)
                    results.append({"symbol": s["symbol"], "status": "跳过", "note": note})
                continue

            trd = ft.OpenSecTradeContext(filter_trdmarket=trd_market, host=_HOST, port=_PORT)
            try:
                acc_id = _get_sim_acc_id(trd)
                if not acc_id:
                    for s in market_signals:
                        note = "没查到这个市场的SIMULATE账户"
                        tracker.log_simulated_order(email, s["symbol"], s.get("name", ""), market, s["action"], s["shares"], 0, "", "", "失败", note)
                        results.append({"symbol": s["symbol"], "status": "失败", "note": note})
                    continue
                for s in market_signals:
                    results.append(_execute_one(qot, trd, email, market, acc_id, s))
            finally:
                trd.close()
    finally:
        qot.close()
    return results


_MARKET_CURRENCY = {"HK": "HKD", "US": "USD", "A": "CNY"}


def get_sim_snapshot() -> dict:
    """AI模拟盘的实时快照——直接查富途SIMULATE账户当前的资产/持仓，不经过
    我们自己的数据库（数据库只留simulated_orders这份"下过什么单"的执行
    记录，持仓/市值这些会变的数字永远以富途自己的账户状态为准，不在本地
    另外维护一份容易跟真实状态脱节的副本）。三个市场各自的SIMULATE账户是
    完全独立的资金池，不是同一笔钱，total_assets_cny是三边折算加总，仅供
    参考整体规模，不代表真的能互相调用。
    """
    total_assets_cny = 0.0
    markets_info: dict[str, dict] = {}
    positions: list[dict] = []
    skipped_markets: list[str] = []

    for market, trd_market in _MARKET_TRD.items():
        currency = _MARKET_CURRENCY[market]
        trd = ft.OpenSecTradeContext(filter_trdmarket=trd_market, host=_HOST, port=_PORT)
        try:
            acc_id = _get_sim_acc_id(trd)
            if not acc_id:
                skipped_markets.append(market)
                continue

            ret, info = trd.accinfo_query(trd_env=ft.TrdEnv.SIMULATE, acc_id=int(acc_id))
            assets_native = float(info.iloc[0]["total_assets"]) if ret == ft.RET_OK and not info.empty else None
            assets_cny = ds.to_cny(assets_native, currency)[0] if assets_native is not None else None
            markets_info[market] = {"acc_id": acc_id, "assets_native": assets_native, "assets_cny": assets_cny, "currency": currency}
            if assets_cny is not None:
                total_assets_cny += assets_cny

            ret2, pos = trd.position_list_query(trd_env=ft.TrdEnv.SIMULATE, acc_id=int(acc_id))
            if ret2 == ft.RET_OK and not pos.empty:
                for _, p in pos.iterrows():
                    if float(p["qty"] or 0) == 0:
                        continue
                    positions.append({
                        "market": market, "code": p["code"], "name": p.get("stock_name") or p["code"],
                        "qty": float(p["qty"]),
                        "cost_price": float(p["cost_price"]) if p.get("cost_price") not in (None, "N/A") else None,
                        "market_val": float(p["market_val"]) if p.get("market_val") not in (None, "N/A") else None,
                        "pl_val": float(p["pl_val"]) if p.get("pl_val") not in (None, "N/A") else None,
                        "currency": currency,
                    })
        except Exception:
            skipped_markets.append(market)
        finally:
            trd.close()

    return {"total_assets_cny": total_assets_cny, "markets": markets_info, "positions": positions, "skipped_markets": skipped_markets}


def get_agent_snapshot() -> dict:
    """sim_agent.py自主决策专用的快照——2026-09-01用户明确要求"暂时先港美股，
    A股不让AI碰，总资金按十万港币算"：跟get_sim_snapshot()的区别是只统计
    HK/US两个市场（不含A股），并且统一折算成HKD而不是CNY——用户把这个自主
    agent的记账本位币定为港币，跟"持仓页"那个面向全部三个市场、以人民币
    汇总的通用快照(get_sim_snapshot)是两回事，服务于不同的展示需求，所以
    分开两个函数，不在同一个函数里加参数分叉（分叉逻辑会让两种用途互相
    牵制，改一个怕影响另一个）。

    起始本金调整（比如改成十万港币）这件事OpenAPI没有对应接口（已确认
    OpenSecTradeContext没有"重置模拟资金"这个方法），只能用户自己在富途
    App里"交易-模拟交易-重置模拟资金"手动操作——这个函数只是如实读取账户
    当前实际余额，用户把本金调好之后这里自然就是以新本金为基准。
    """
    total_assets_hkd = 0.0
    markets_info: dict[str, dict] = {}
    positions: list[dict] = []
    skipped_markets: list[str] = []

    _rate_to_hkd = {"HKD": 1.0, "USD": USD_HKD_RATE}

    for market in ("HK", "US"):
        trd_market = _MARKET_TRD[market]
        currency = _MARKET_CURRENCY[market]
        trd = ft.OpenSecTradeContext(filter_trdmarket=trd_market, host=_HOST, port=_PORT)
        try:
            acc_id = _get_sim_acc_id(trd)
            if not acc_id:
                skipped_markets.append(market)
                continue

            ret, info = trd.accinfo_query(trd_env=ft.TrdEnv.SIMULATE, acc_id=int(acc_id))
            assets_native = float(info.iloc[0]["total_assets"]) if ret == ft.RET_OK and not info.empty else None
            rate = _rate_to_hkd.get(currency)
            assets_hkd = assets_native * rate if (assets_native is not None and rate) else None
            markets_info[market] = {"acc_id": acc_id, "assets_native": assets_native, "assets_hkd": assets_hkd, "currency": currency}
            if assets_hkd is not None:
                total_assets_hkd += assets_hkd

            ret2, pos = trd.position_list_query(trd_env=ft.TrdEnv.SIMULATE, acc_id=int(acc_id))
            if ret2 == ft.RET_OK and not pos.empty:
                rate = _rate_to_hkd.get(currency)
                for _, p in pos.iterrows():
                    if float(p["qty"] or 0) == 0:
                        continue
                    market_val = float(p["market_val"]) if p.get("market_val") not in (None, "N/A") else None
                    positions.append({
                        "market": market, "code": p["code"], "name": p.get("stock_name") or p["code"],
                        "qty": float(p["qty"]),
                        "cost_price": float(p["cost_price"]) if p.get("cost_price") not in (None, "N/A") else None,
                        "market_val": market_val,
                        "market_val_hkd": (market_val * rate) if (market_val is not None and rate) else None,
                        "pl_val": float(p["pl_val"]) if p.get("pl_val") not in (None, "N/A") else None,
                        "currency": currency,
                    })
        except Exception:
            skipped_markets.append(market)
        finally:
            trd.close()

    # 用户明确要求"实时收益要读取Futu获得最新报价结算当前收益"——真实故障：
    # 上面position_list_query返回的market_val/pl_val是富途SIMULATE账户自己
    # 记账用的估值，实测发现它不是逐笔实时重估的，同一支持仓连续几次(间隔
    # 15分钟)查出来的market_val/pl_val完全没变过，哪怕这段时间美股一直在
    # 正常交易——反映到走势图上就是"一直是平的"，不是图表渲染的问题，是
    # 上游这份持仓市值数据本身没实时更新。这里改成拿到持仓清单后，再用
    # 一次真实行情快照(get_market_snapshot)把每支持仓的市值/浮动盈亏按
    # 最新成交价重新算一遍，覆盖掉富途账户自己给的(可能滞后的)那份数字，
    # 换算逻辑不变，只是价格来源换成了真实盘口。
    codes_by_market: dict[str, list[str]] = {}
    for p in positions:
        codes_by_market.setdefault(p["market"], []).append(p["code"])
    if codes_by_market:
        qot = ft.OpenQuoteContext(host=_HOST, port=_PORT)
        try:
            for market, codes in codes_by_market.items():
                ret3, snap = qot.get_market_snapshot(codes)
                if ret3 != ft.RET_OK or snap.empty:
                    continue
                price_by_code = dict(zip(snap["code"], snap["last_price"]))
                rate = _rate_to_hkd.get(_MARKET_CURRENCY[market])
                for p in positions:
                    if p["market"] != market:
                        continue
                    last_price = price_by_code.get(p["code"])
                    if last_price is None or not rate:
                        continue
                    p["market_val"] = float(last_price) * p["qty"]
                    p["market_val_hkd"] = p["market_val"] * rate
                    if p.get("cost_price") is not None:
                        p["pl_val"] = (float(last_price) - p["cost_price"]) * p["qty"]
        finally:
            qot.close()

    # 持仓总市值(折HKD)——sim_agent.py的虚拟预算约束(用户要求"理论无上限的
    # 富途账户里，让AI自己控制在约十万港币规模")要用这个数字做硬性拦截，
    # 不能只让AI嘴上说控制预算，代码这边也要能核实"当前占用了多少额度"，
    # 缺失汇率的持仓不计入(不能当0处理，那样会低估占用、放行不该放行的
    # 买入)，如实通过holdings_value_partial标记这种情况。
    holdings_value_hkd = 0.0
    holdings_value_partial = False
    for p in positions:
        if p.get("market_val_hkd") is not None:
            holdings_value_hkd += p["market_val_hkd"]
        else:
            holdings_value_partial = True

    return {
        "total_assets_hkd": total_assets_hkd, "markets": markets_info, "positions": positions,
        "skipped_markets": skipped_markets, "holdings_value_hkd": holdings_value_hkd,
        "holdings_value_partial": holdings_value_partial,
    }
