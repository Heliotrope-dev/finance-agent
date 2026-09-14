# -*- coding: utf-8 -*-
"""首页"资产配置行动清单"（advisor.advise_portfolio）的午间/晚间微信推送。

定时任务直接跑这个脚本，不经过任何模型——踩过跟trade_alert_push.py同一个坑：
"没有就不用推送"这种条件判断如果交给agentTurn，announce投递会把模型没调用
message工具时的那句"结束语"当成正文原样发出去（试过让模型在跳过时回一句
"本次无更新，不推送"，cron的announce fallback照样把这句话当成消息发给了
用户，属于fallback投递机制本身的设计，不是提示词能绕开的）。这类"查询/
生成→有就发、没有就不动"全是确定性流程，没有一步真的需要模型判断"要不要
发"，交给模型只会多两种故障模式（不按预期跳过、把中间过程当结果发出去），
不如直接写成脚本。

用户原话："这个资产配置行动计划每天下午十二点半和晚上十点执行，没有就
直接不用动，有的话AI分析open claw发微信和项目同步推送"——AI分析这一步
本来就在advise_portfolio()内部完成（真实查行情/新闻+推理），这个脚本只
负责"有没有→要不要推、推什么"这个确定性判断，不重新做AI判断。"项目同步
推送"不需要额外代码：advise_portfolio()写进的是网站首页/持仓页共用的
portfolio_advice表，这里推送的和网站上看到的是同一份结果。
"""
import os
import sys

import advisor
import wechat_delivery


def _build_message(result: dict) -> str:
    lines = ["资产配置行动清单更新", ""]
    lines.append(result["analysis_text"].strip())
    action_signals = [s for s in result["signals"] if s["action"] != "不动"]
    if action_signals:
        lines.append("")
        lines.append("交易信号（需要操作的）：")
        for s in action_signals:
            lines.append(f"{s['name']}：{s['action']} {s['shares']:g}股 · 约¥{s['amount_cny']:,.0f}")
    return "\n".join(lines)


def main() -> int:
    advisor._load_secrets_into_env()
    advisor._check_gemini_available()

    result = advisor.advise_portfolio(advisor._EMAIL)
    if not result:
        # 持仓不足2支，或分析没能生成——如实什么都不做，不发一句"本次无
        # 更新"去打扰用户，这正是"没有就直接不用动"这句话字面的意思。
        print("NO_PENDING")
        return 0

    message = _build_message(result)
    if not wechat_delivery.send_text(message):
        return 1
    print("已推送资产配置行动清单")
    return 0


if __name__ == "__main__":
    code = main()
    # advisor 间接带进 Futu 相关模块，起的线程不是 daemon 线程（项目老坑，
    # 见 trade_alert_push.py 同一处说明），不强制退出会挂住；os._exit 会
    # 跳过 stdout 缓冲区刷新，管道输出可能整段丢失，必须先 flush。
    sys.stdout.flush()
    os._exit(code)
