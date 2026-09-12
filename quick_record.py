# -*- coding: utf-8 -*-
"""命令行记一笔买入/卖出，给 OpenClaw 在微信里替用户填数用。

用户的原话是"我懒得再打开填数字了我直接把截图发给open claw"。他在汇丰下完
单，发一张成交截图到微信，希望持仓就自动记上——这是合理的，手动填一遍
成交价和股数既烦又容易错。

OpenClaw 那边已经配了多模态模型（Qwen3-VL），能把截图里的股票名、方向、
股数、成交价读出来。缺的是一个能写进数据库的入口，就是这个脚本。

用法：
    python3 quick_record.py --buy  --symbol 00700 --shares 100 --price 442.8
    python3 quick_record.py --sell --symbol 00700 --shares 100 --price 455.0
    python3 quick_record.py --undo            撤销最近一笔
    python3 quick_record.py --show            看当前持仓

设计上有两条是为"AI 代填"这个场景专门定的：

一、**写完立刻回显完整记录**。AI 读截图会出错——数字看错一位、把"腾讯"
认成别的票都可能发生，而这些错误会一路污染后面的止损计算和收益结算。
所以每次写入都把结果原样打回去，让用户扫一眼就能发现不对。

二、**提供 --undo**。发现错了要能一句话撤销，而不是让用户去网页里找。
没有撤销键的自动化，用户第二次就不敢用了。

代码可以只给数字（00700），也可以给名字（腾讯）——名字会走富途搜索转成
代码，搜不到就报错退出，绝不猜。猜错代码等于把仓位记到别的股票上，
比记不上严重得多。
"""
import argparse
import json
import sys
from pathlib import Path

import advisor
import data_sources as ds
import tracker

_UNDO = Path(__file__).resolve().parent / "data" / "last_record.json"


def _cny_amount_text(amount: float, currency: str) -> str:
    """把原币金额换成可核对的人民币参考值，不改变数据库的成交原币事实。"""
    try:
        cny, note = ds.to_cny(amount, currency)
    except Exception:
        cny, note = None, "汇率获取失败"
    if cny is None:
        return f"人民币折算不可用（{note}）"
    return f"约人民币 {cny:,.2f} 元（{note}）"


def _resolve(text: str) -> tuple[str, str, str] | None:
    """把用户/AI给的代码或名字解析成 (代码, 市场, 名称)。解析不出返回 None。"""
    t = (text or "").strip()
    if not t:
        return None
    # 已经是纯数字代码：港股5位、沪深6位
    digits = "".join(ch for ch in t if ch.isdigit())
    if digits and len(digits) == len(t.replace(".", "").replace("·", "")):
        if len(digits) == 5:
            return digits, "HK", digits
        if len(digits) == 4:
            return digits.zfill(5), "HK", digits.zfill(5)
    # 纯字母：当美股代码
    if t.isascii() and t.replace("-", "").replace(".", "").isalpha():
        return t.upper(), "US", t.upper()
    # 其余走富途全市场模糊搜索，支持中英文和拼音
    try:
        rows = ds.search_quote_futu(t) or []
    except Exception:
        rows = []
    for r in rows:
        sym = str(r.get("code") or "")
        mkt = str(r.get("market") or "")
        nm = str(r.get("name") or sym)
        if sym and mkt:
            return sym, mkt, nm
    return None


def _show() -> str:
    email = advisor._EMAIL
    held = [p for p in tracker.get_positions(email) if (p.get("shares") or 0) > 0]
    if not held:
        return "当前空仓。"
    L = ["当前持仓："]
    for p in held:
        shares = float(p.get("shares") or 0)
        cost = float(p.get("cost_total") or 0)
        avg = cost / shares if shares else 0
        currency = str(p.get("currency") or {"HK": "HKD", "US": "USD"}.get(p.get("market"), "CNY"))
        L.append(f"  {p.get('name') or p['symbol']}（{p['symbol']}·{p['market']}）"
                 f"{shares:.0f}股 原币成本均价 {avg:.3f} {currency}")
        L.append(f"    原币成本 {cost:,.2f} {currency}，{_cny_amount_text(cost, currency)}")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--buy", action="store_true")
    g.add_argument("--sell", action="store_true")
    g.add_argument("--undo", action="store_true")
    g.add_argument("--show", action="store_true")
    ap.add_argument("--symbol", help="代码或名称，如 00700 / 腾讯 / NVDA")
    ap.add_argument("--shares", type=float)
    ap.add_argument("--price", type=float, help="成交价（标的本身的货币）")
    args = ap.parse_args()

    advisor._load_secrets_into_env()
    email = advisor._EMAIL

    if args.show:
        print(_show())
        return 0

    if args.undo:
        try:
            last = json.loads(_UNDO.read_text(encoding="utf-8"))
        except Exception:
            print("没有可撤销的记录。")
            return 1
        sym, mkt = last["symbol"], last["market"]
        shares, amount = last["shares"], last["amount"]
        try:
            if last["action"] == "buy":
                # 买入的撤销 = 反向减仓
                tracker.reduce_position(email, sym, shares, amount)
            else:
                tracker.upsert_position(email, sym, last.get("name") or sym, mkt,
                                        shares, amount)
        except Exception as e:
            print(f"撤销失败：{e}")
            return 1
        _UNDO.unlink(missing_ok=True)
        print(f"已撤销：{last['action']} {last.get('name') or sym} {shares:.0f}股")
        print()
        print(_show())
        return 0

    if not args.symbol or not args.shares or not args.price:
        print("缺参数：--symbol --shares --price 三个都要给")
        return 2

    got = _resolve(args.symbol)
    if not got:
        # 绝不猜代码。猜错等于把仓位记到别的股票上，后面的止损、收益结算
        # 全部会算在错的标的上，比记不上严重得多。
        print(f"认不出「{args.symbol}」是哪只股票，没有记录。"
              "请给准确的代码（港股5位数字，美股字母代码）。")
        return 1
    sym, mkt, name = got
    amount = args.shares * args.price
    currency = {"HK": "HKD", "US": "USD", "A": "CNY"}.get(mkt, "CNY")

    try:
        if args.buy:
            tracker.upsert_position(email, sym, name, mkt, args.shares, amount)
            act = "买入"
        else:
            tracker.reduce_position(email, sym, args.shares, amount)
            act = "卖出"
    except Exception as e:
        print(f"记录失败：{e}")
        return 1

    try:
        _UNDO.parent.mkdir(parents=True, exist_ok=True)
        _UNDO.write_text(json.dumps({
            "action": "buy" if args.buy else "sell", "symbol": sym, "market": mkt,
            "name": name, "shares": args.shares, "amount": amount,
        }, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

    # 回显完整结果。AI 读截图会出错——数字看错一位、认错股票都可能，
    # 而这些错误会一路污染止损计算和收益结算。打回去让用户扫一眼。
    print(f"已记录：{act} {name}（{sym}·{mkt}）{args.shares:.0f}股 "
          f"成交价 {args.price} {currency}，原币金额 {amount:,.2f} {currency}")
    print(f"人民币参考：{_cny_amount_text(amount, currency)}")
    print()
    print(_show())
    print()
    print("如果不对，回一句「撤销」即可。")
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    __import__("os")._exit(code)
