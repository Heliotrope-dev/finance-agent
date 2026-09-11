"""把 Futu SIMULATE 账户里"不是 AI 下单买的"持仓清掉，一次性脚本。

为什么需要它：这个 SIMULATE 账户不是 AI 独占的沙盒。2026-09-11 前端审计
发现账户里长期躺着两笔从未出现在 simulated_orders 里的持仓（新奥能源
HK.02688 200股、滨化股份 HK.06745 2000股，合计市值约 HK$15,798，都是浮亏），
来源不明，大概率是这套记账体系上线前的手动测试遗留。

它们造成的后果已经在 sim_trader.get_ledger_reconciled_holdings 那一层堵住了
（净值/收益率/预算只算 AI 自己买过的仓位），所以现在页面上的数字是对的。
这个脚本做的是最后一步物理清理：把这些仓位真的卖掉，让账户状态和账本状态
完全一致，页面上也不再需要挂"账户内另有非AI仓位"那条说明。

判断标准跟 get_ledger_reconciled_holdings 完全一致：按 (market, symbol) 在
simulated_orders 里核算净买入股数，>0 的是 AI 的，其余都算"外来仓位"。

安全设计：
- 只卖，不买。
- 只动"外来仓位"，AI 自己买的一股都不碰。
- 跑完写一个 marker 文件，再次执行直接退出——挂在 cron 上重复触发也不会
  反复交易（比如用户后来自己手动建了仓，不该被这个脚本顺手平掉）。
- 市场没开盘就直接退出，不下单、不写 marker，等下一次触发。

用法：
    python3 cleanup_foreign_sim_positions.py          # 预演，只打印不下单
    python3 cleanup_foreign_sim_positions.py --write  # 真的下卖单
"""

import os
import sys
from datetime import datetime, timezone

import futu as ft

import sim_trader
import tracker

_EMAIL = os.environ.get("ADVISOR_EMAIL", "")
_MARKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", ".foreign_sim_positions_cleaned")


def _market_open(qot, code: str) -> bool:
    ret, ms = qot.get_market_state([code])
    if ret != ft.RET_OK or ms.empty:
        return False
    state = str(ms.iloc[0].get("market_state") or "")
    # 盘中才卖。竞价/盘前盘后这些阶段模拟盘的成交行为不稳定，宁可等下一轮。
    return "TRADING" in state.upper()


def main(write: bool = False) -> int:
    if os.path.exists(_MARKER):
        print(f"已经清理过（marker: {_MARKER}），直接退出。")
        return 0

    email = _EMAIL
    if not email:
        print("没有配置 ADVISOR_EMAIL，退出。")
        return 1

    reconciled = sim_trader.get_ledger_reconciled_holdings(email)
    foreign = reconciled["foreign_positions"]
    if not foreign:
        print("账户里没有外来仓位，无需清理。")
        if write:
            open(_MARKER, "w").write(datetime.now(timezone.utc).isoformat())
        return 0

    print(f"发现 {len(foreign)} 笔外来仓位（合计市值 HK${reconciled['foreign_value_hkd']:,.0f}）：")
    for p in foreign:
        print(f"  {p['market']} {p['code']} {p.get('name')} qty={p['qty']:g} mv={p.get('market_val')}")

    if not write:
        print("（预演，没有下单。加 --write 真正执行）")
        return 0

    qot = ft.OpenQuoteContext(host=sim_trader._HOST, port=sim_trader._PORT)
    done, skipped = 0, 0
    try:
        by_market: dict[str, list[dict]] = {}
        for p in foreign:
            by_market.setdefault(p["market"], []).append(p)

        for market, positions in by_market.items():
            trd_market = sim_trader._MARKET_TRD.get(market)
            if trd_market is None:
                print(f"  跳过不支持的市场 {market}")
                skipped += len(positions)
                continue
            if not _market_open(qot, positions[0]["code"]):
                print(f"  {market} 当前不在盘中交易时段，本轮跳过，等下次触发。")
                skipped += len(positions)
                continue

            trd = ft.OpenSecTradeContext(filter_trdmarket=trd_market,
                                         host=sim_trader._HOST, port=sim_trader._PORT)
            try:
                acc_id = sim_trader._get_sim_acc_id(trd)
                if not acc_id:
                    print(f"  没查到 {market} 的 SIMULATE 账户，跳过。")
                    skipped += len(positions)
                    continue
                for p in positions:
                    ret, data = trd.place_order(
                        price=0, qty=float(p["qty"]), code=p["code"],
                        trd_side=ft.TrdSide.SELL, order_type=ft.OrderType.MARKET,
                        trd_env=ft.TrdEnv.SIMULATE, acc_id=int(acc_id),
                    )
                    if ret == ft.RET_OK:
                        print(f"  已卖出 {p['code']} {p['qty']:g}股")
                        done += 1
                    else:
                        print(f"  卖出 {p['code']} 失败：{data}")
                        skipped += 1
            finally:
                trd.close()
    finally:
        qot.close()

    if done and not skipped:
        open(_MARKER, "w").write(datetime.now(timezone.utc).isoformat())
        print("清理完成，已写 marker，后续触发会直接退出。")
    else:
        print(f"本轮成交 {done} 笔、跳过 {skipped} 笔；没写 marker，下次触发会继续尝试。")
    return 0


if __name__ == "__main__":
    sys.exit(main(write="--write" in sys.argv))
