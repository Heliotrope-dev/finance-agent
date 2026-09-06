# -*- coding: utf-8 -*-
"""命令行搜索入口，给 OpenClaw 用。走项目自己的 Serper 额度。

2026-09-06 用户要求"把最新API也给 OpenClaw 装上"。OpenClaw 原生只支持
Exa（还能用，但那是另一份额度），不认 Serper。与其在它的插件体系里折腾，
不如给它一个命令行入口——它有 exec 权限，跑脚本比装插件简单可靠。

好处不只是共用额度：走同一个 web_research 意味着限流处理、缓存、结果
过滤这些逻辑只有一份，不会出现"网页端答得对、微信里答得不对"这种因为
两条链路行为不一致导致的问题。

用法：
    python3 search_cli.py "泡泡玛特 2026 中期业绩"
    python3 search_cli.py --read "https://..."      直接读一个网页
    python3 search_cli.py --full "查询词"            搜索+抓正文
"""
import argparse
import sys

import advisor
import web_research


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", default="", help="搜索词")
    ap.add_argument("--read", default="", help="直接读取这个URL的正文")
    ap.add_argument("--full", action="store_true", help="搜索并抓正文（慢，但更完整）")
    ap.add_argument("--limit", type=int, default=6)
    args = ap.parse_args()

    advisor._load_secrets_into_env()

    if args.read:
        t = web_research.read_url(args.read, max_chars=4000)
        print(t or "读不到这个页面（可能是JS渲染或被反爬挡了）")
        return 0 if t else 1

    if not args.query:
        print("要给搜索词，或者用 --read <url>")
        return 2

    if args.full:
        print(web_research.research(args.query, read_top=2, limit=args.limit,
                                    max_chars=2500) or "没搜到")
        return 0

    hits = web_research.search(args.query, limit=args.limit)
    if not hits:
        print("没搜到（如果日志里有限流提示，说明是被挡了，不是真的没有）")
        return 1
    for h in hits:
        line = f"- {h['title']}（{h.get('domain','')}"
        if h.get("date"):
            line += f"，{h['date']}"
        print(line + "）")
        if h.get("snippet"):
            print(f"  {h['snippet']}")
        print(f"  {h['url']}")
    return 0


if __name__ == "__main__":
    code = main()
    # 项目老坑：富途相关模块的线程不是 daemon 线程，不强制退出会挂住；
    # os._exit 跳过 stdout 缓冲刷新，所以必须先 flush。
    sys.stdout.flush()
    __import__("os")._exit(code)
