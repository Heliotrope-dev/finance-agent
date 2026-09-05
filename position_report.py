# -*- coding: utf-8 -*-
"""持仓盘中/收盘报告：AI 看一遍当前持仓，有要动的立刻说，收盘结算收益。

用户的流程是"照着系统 1:1 在汇丰下单"，所以持仓状态在两边必须是一致的，
而且任何该动的时刻都不能错过。这个脚本负责三件事：

  1. 逐支持仓做一次 AI 判断（复用 advisor 的六维链路，数据全量喂进去）
  2. 判断结果是卖出/减仓时，在推送里明确标出来，让用户同步操作
  3. 收盘那一轮额外结算当日收益（按成本价对最新价）

调用时机由 --session 参数区分，因为港美股的盘中时点不一样：
  hk-mid   北京 12:00  港股午盘
  hk-close 北京 16:10  港股收盘后
  us-mid   北京 00:30  美股午盘
  us-close 北京 04:10  美股收盘后

为什么不是一个任务跑全部：港股午盘的时候美股还没开，把美股持仓的"实时"
判断混进来，用的是十几个小时前的收盘价，那不是分析是噪音。按市场分开，
每次只看当时真正在交易的那部分。

推送同样走纯脚本不经模型——理由跟 plan_push 一致，报告里有成本价、市值、
盈亏金额，这些数字不能被复述一遍。
"""
import argparse
import datetime as dt
import subprocess
import sys

import advisor
import data_sources as ds
import tracker

_WECHAT_TARGET = "o9cq80_APBq3j8dLdECzrOB0opJs@im.wechat"
_WECHAT_ACCOUNT = "b329c51975ab-im-bot"
_CHANNEL = "openclaw-weixin"

_SESSIONS = {
    "hk-mid": ("HK", "港股午盘", False),
    "hk-close": ("HK", "港股收盘", True),
    "us-mid": ("US", "美股午盘", False),
    "us-close": ("US", "美股收盘", True),
}


def _send(message: str) -> bool:
    try:
        r = subprocess.run(
            ["openclaw", "message", "send", "--channel", _CHANNEL,
             "--target", _WECHAT_TARGET, "--account", _WECHAT_ACCOUNT,
             "--message", message],
            capture_output=True, text=True, timeout=120)
    except Exception as e:
        print(f"发送异常: {e}")
        return False
    if r.returncode != 0:
        print(f"发送失败(exit={r.returncode}): {(r.stderr or r.stdout or '')[:250]}")
        return False
    return True


def build(session: str) -> tuple[str, bool]:
    """返回 (推送文本, 是否有需要用户立刻操作的事)。"""
    market, label, is_close = _SESSIONS[session]
    email = advisor._EMAIL
    now = dt.datetime.now(dt.timezone.utc).astimezone()

    holdings = [p for p in tracker.get_positions(email)
                if (p.get("shares") or 0) > 0 and p.get("market") == market]

    L = [f"投研站 · {label}报告 {now.strftime('%m-%d %H:%M')}"]
    if not holdings:
        L.append("")
        L.append(f"{market} 市场当前空仓，没有需要处理的持仓。")
        if is_close:
            L.append("今日无持仓收益可结算。")
        return "\n".join(L), False

    L.append("")
    urgent = []
    total_cost = total_value = 0.0
    fx, _cur = 1.0, "CNY"
    try:
        if market in ("HK", "US"):
            rate, _ = ds.get_fx_rate("HKD" if market == "HK" else "USD", "CNY")
            fx = float(rate) if rate else 0.0
    except Exception:
        fx = 0.0

    for p in holdings:
        sym, mkt = p["symbol"], p["market"]
        shares = float(p.get("shares") or 0)
        cost_total = float(p.get("cost_total") or 0)
        try:
            q = ds.get_stock_realtime_futu(sym, mkt) or {}
        except Exception:
            q = {}
        last = q.get("最新价")
        if not last:
            L.append(f"{p.get('name') or sym}（{sym}）行情取不到，本轮跳过")
            continue

        value = shares * float(last)
        cost_avg = (cost_total / shares) if shares else 0
        pnl = value - cost_total
        pnl_pct = (pnl / cost_total * 100) if cost_total else 0
        total_cost += cost_total
        total_value += value

        # AI 判断：走跟排行榜同一条六维链路，数据全量喂进去
        verdict = {}
        try:
            verdict = advisor._judge_one(
                {"symbol": sym, "market": mkt, "name": p.get("name") or sym},
                "position") or {}
        except Exception as e:
            print(f"[report] {sym} 判断失败: {e}")

        act = verdict.get("action") or "—"
        score = verdict.get("score")
        L.append(f"{p.get('name') or sym}（{sym}·{mkt}）")
        L.append(f"  持{shares:.0f}股 成本{cost_avg:.3f} 现价{last} "
                 f"盈亏{pnl:+,.0f}（{pnl_pct:+.1f}%）")
        L.append(f"  AI判断：{act}" + (f" {score}分" if score else ""))
        reason = (verdict.get("fundamental_verdict") or "").strip()
        if reason:
            first = reason.split("\n")[0][:110]
            L.append(f"  {first}")
        if act in ("卖出", "减仓"):
            urgent.append(f"{p.get('name') or sym}（{sym}）{act}")
        L.append("")

    if total_cost > 0:
        tp = total_value - total_cost
        L.append(f"{market} 持仓合计：成本{total_cost:,.0f} 市值{total_value:,.0f} "
                 f"盈亏{tp:+,.0f}（{tp/total_cost*100:+.1f}%）")
        if fx > 0:
            L.append(f"  折合人民币约 {total_value * fx:,.0f} 元，"
                     f"浮动盈亏 {tp * fx:+,.0f} 元")

    if urgent:
        L.insert(1, "")
        L.insert(1, "【需要你操作】" + "；".join(urgent))
        L.insert(1, "")

    if is_close:
        L.append("")
        L.append("以上为收盘结算。卖出后记得在网页持仓里同步平掉，")
        L.append("否则下一轮判断还会把它当成在持。")

    return "\n".join(L), bool(urgent)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True, choices=sorted(_SESSIONS))
    ap.add_argument("--dry-run", action="store_true", help="只打印不推送")
    args = ap.parse_args()

    advisor._load_secrets_into_env()
    text, urgent = build(args.session)
    print(text)
    if args.dry_run:
        return 0
    ok = _send(text)
    print(f"\n推送{'成功' if ok else '失败'}" + ("（含待操作项）" if urgent else ""))
    return 0 if ok else 1


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    __import__("os")._exit(code)
