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

_MARKET_TRD = {"HK": ft.TrdMarket.HK, "US": ft.TrdMarket.US, "A": ft.TrdMarket.CN}


def _a_share_prefix(symbol: str) -> str:
    """A股代码转富途SH./SZ.前缀——6开头是上交所，0/3开头是深交所。8/4开头
    理论上是北交所，富途模拟盘是否支持不确定，不特殊处理，交给下单接口
    自己报错（上层会catch住记成失败，不会导致整批信号中断）。"""
    if symbol.startswith("6"):
        return "SH"
    if symbol.startswith(("0", "3")):
        return "SZ"
    return "SH"


def _to_futu_code(symbol: str, market: str) -> str:
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
    symbol = signal["symbol"]
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
