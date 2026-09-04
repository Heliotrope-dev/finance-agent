# -*- coding: utf-8 -*-
"""AI模拟盘成交推送的取数与销账工具，给OpenClaw那个"实时买卖提醒"任务调用。

用户明确要求"每笔买入卖出都要在微信上通知我"。原来的做法是让agent去读
data/last_sim_agent_run.log 判断这轮有没有成交——那个文件每轮都被整个
覆盖，提醒任务只要错过一轮（网关重启、模型调用失败，千问额度耗尽那几天
就连续失败了25轮），那一轮的成交通知就永久消失，没有任何补发机制。

改成由数据库里的持久记录驱动：simulated_orders 表上有 notified_at 标记，
没打标记的成功成交会一直被捞出来，直到真的推送成功为止。漏一轮只是晚
几分钟到，不会丢。

用法：
    python3 trade_alerts.py --list          列出待推送的成交（无则输出 NO_PENDING）
    python3 trade_alerts.py --mark 12,13    推送成功后销账

刻意分成两步而不是"取出即销账"：销账必须发生在微信真的发出去之后。
如果发送失败了却已经销账，这笔就永远不会再被推送——那正是要避免的情况。
反过来"发出去了但销账失败"只会导致重复推一次，可以接受。
"""
import argparse
import os
import sys

import advisor
import tracker

_ACTION_TEXT = {"买入": "买入", "卖出": "卖出"}


def _fmt(order: dict) -> str:
    """一行一笔，字段固定顺序，方便agent照抄进微信消息不用自己组织格式。"""
    name = (order.get("name") or "").strip()
    # name字段历史上混进过"买入 快手-W"这种带动作前缀的值，这里剥掉，
    # 免得推送出来变成"买入 买入 快手-W"。
    for act in ("买入", "卖出"):
        if name.startswith(act):
            name = name[len(act):].strip()
    shares = order.get("shares_ordered") or 0
    shares_text = f"{shares:.0f}" if float(shares).is_integer() else f"{shares}"
    return (
        f"[{order['id']}] {_ACTION_TEXT.get(order.get('action'), order.get('action'))} "
        f"{name} {order.get('symbol')} ({order.get('market')}) {shares_text}股"
        f" | 时间 {(order.get('created_at') or '')[:19]}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="列出待推送的成交")
    ap.add_argument("--mark", default="", help="推送成功后销账，逗号分隔的id")
    args = ap.parse_args()

    email = advisor._EMAIL

    if args.mark:
        ids = [x.strip() for x in args.mark.replace("，", ",").split(",") if x.strip()]
        n = tracker.mark_orders_notified(ids)
        print(f"已销账 {n} 笔")
        return 0

    orders = tracker.get_unnotified_orders(email)
    if not orders:
        print("NO_PENDING")
        return 0

    print(f"待推送成交 {len(orders)} 笔：")
    for o in orders:
        print(_fmt(o))
    print("ID列表：" + ",".join(str(o["id"]) for o in orders))
    return 0


if __name__ == "__main__":
    code = main()
    # advisor会间接把富途相关模块带进来，那些线程不是daemon线程（项目里
    # 反复踩过的老坑），不强制退出进程会挂住不返回。os._exit会跳过stdout
    # 的缓冲区刷新，管道输出时内容会整个丢掉，所以必须先flush再退出。
    sys.stdout.flush()
    os._exit(code)
