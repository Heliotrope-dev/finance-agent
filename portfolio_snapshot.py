# -*- coding: utf-8 -*-
"""真实持仓市值的定时快照——2026-09-22（前端审计P0-2）新增。

在这之前，portfolio_equity_snapshots只在用户自己打开"持仓"页时才会被写入
（见app.py _render_positions_today_pnl），说明文字却写着"每约5分钟记录一个
真实快照"，暗示背后有个一直在跑的定时任务——实际没有，没人看这个页面的
几个小时里，趋势图上就是空的。这个脚本就是把这件事补成真的：跟
intraday_watch.cron同一批时间窗口（盘中每5分钟），独立取价、独立写快照，
不依赖任何人正好在看网页。

完整性要求跟app.py那边改过的逻辑完全一致：任何一支持仓取价/汇率折算失败，
这一轮就整体不写——宁可这一格空着，也不能把"少算的"当"真实的"存进去
（09-22真实故障：广发QDII取价失败，07788+ORCX凑出的¥15,057被当成真实
市值写库，画出一条假暴跌）。
"""
import sys

import advisor  # 加载secrets进env，压掉streamlit日志噪声，见其模块docstring
import data_sources as ds
import tracker


def run() -> None:
    email = advisor._EMAIL
    if not email:
        print("[portfolio_snapshot] 未配置 ADVISOR_EMAIL，跳过。", flush=True)
        return

    positions = tracker.get_positions(email)
    holding_items = [p for p in positions if (p.get("shares") or 0) > 0]
    if not holding_items:
        print("[portfolio_snapshot] 没有真实持仓，跳过。", flush=True)
        return

    hk_us_items = [
        (it["symbol"], it.get("market", "A")) for it in holding_items if it.get("market", "A") in ("HK", "US")
    ]
    try:
        hk_us_quotes = ds.get_stock_realtime_futu_batch(hk_us_items) if hk_us_items else {}
    except Exception:
        hk_us_quotes = {}

    total_value = 0.0
    skipped = 0
    for item in holding_items:
        symbol, market = item["symbol"], item.get("market", "A")
        if market in ("HK", "US"):
            spot = hk_us_quotes.get((symbol, market)) or {}
        else:
            try:
                spot = ds.get_stock_realtime(symbol, market=market)
            except Exception:
                spot = {}
        price = spot.get("最新价")
        if not price:
            skipped += 1
            continue
        value_cny, _note = ds.to_cny(price * item["shares"], item.get("currency", "CNY"))
        if value_cny is None:
            skipped += 1
            continue
        total_value += value_cny

    if skipped:
        print(f"[portfolio_snapshot] {skipped} 支持仓取价/折算失败，本轮不写快照（避免残缺数据）。", flush=True)
        return
    if total_value <= 0:
        print("[portfolio_snapshot] 市值算出来是0，本轮不写快照。", flush=True)
        return

    wrote = tracker.log_portfolio_equity_snapshot(email, total_value)
    print(f"[portfolio_snapshot] 市值¥{total_value:,.0f}，{'已写入' if wrote else '距上一条快照不足5分钟，跳过'}。", flush=True)


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print(f"[portfolio_snapshot] 失败：{type(e).__name__}: {e}", file=sys.stderr, flush=True)
        sys.exit(1)
