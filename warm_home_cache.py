"""首页世界地图缓存预热——独立脚本，通过系统crontab每分钟跑一次（不走
OpenClaw的agentTurn cron，那套是LLM代理执行，这里只是纯机械的"查数据+
写文件"，用AI代理跑纯属浪费）。

背景：_render_home_map()原来直接在用户请求路径上并发查A/HK/US三个市场的
指数快照（8秒截止），2026-08-30验证过这个改动把首页从20+秒压到3秒左右，
但"3秒"仍然是"运气不好撞上慢查询"的用户才要付的成本。这个脚本把"查数据"
这件事挪到用户请求路径之外——固定每分钟主动查一次（用不带超时压力的
get_multi_index_snapshot，反正没有用户在等），写进一个JSON文件；
_render_home_map()改成优先读这个文件，读到新鲜数据就完全不用等网络，
只有文件缺失/过旧时才退回原来那套并发+8秒截止的兜底逻辑。

代价（如实记录，不是免费午餐）：这个脚本不管有没有真人在看网站，都按
固定频率去查三个市场的免费接口，比原来"只在有人访问时才查"多用了一些
这几个免费数据源的调用配额。写这个文件的格式/位置跟data/fx_rate_cache.json
是同一个"独立小文件+读写函数"套路，不是新发明一套。
"""

import os
import sys

import data_sources as ds


def main():
    snaps = {}
    for mkt in ("A", "HK", "US"):
        try:
            snaps[mkt] = ds.get_multi_index_snapshot(mkt)
        except Exception:
            snaps[mkt] = []
    try:
        global_idx = ds.get_global_indices()
    except Exception:
        global_idx = {}

    # 2026-09-13 多预热两样：宏观仪表盘和首页头条。
    # 判据跟指数快照完全一样——全站共享、跟访客是谁无关、分钟级才变一次。
    # 实测这两样在渲染路径上分别要 0.66 秒和 0.97 秒，加起来 1.6 秒是**每个
    # 访客每次打开首页都要付**的。挪到这里之后对访客变成读一次本地文件。
    # 每样各自 try：任何一样挂了都不该连累其余，也不该让整个预热轮次白跑。
    try:
        macro = ds.get_macro_dashboard()
    except Exception:
        macro = {}
    # 资讯要跟页面走**同一套两级逻辑**，不能只预热第一级：
    # get_hot_market_news 是拿当天真实异动股名当关键词去富途搜，周末/休市日
    # 没有异动就没有关键词，自然返回 0 条（实测周日就是 0），这时页面会退到
    # get_market_news（财新）——也就是说真正显示在页面上的经常是第二级。
    # 只预热第一级等于预热了一个空结果，页面照样要自己去查第二级，白忙。
    news = []
    for _fn in (ds.get_hot_market_news, ds.get_market_news):
        try:
            _df = _fn()
        except Exception:
            continue
        if _df is not None and not _df.empty:
            news = _df.to_dict("records")
            break

    ds.save_home_map_cache(snaps, global_idx, macro=macro, news=news)
    print(f"预热完成：{sum(len(v) for v in snaps.values())}条市场指数 + "
          f"{len(global_idx)}条国际指数 + {len(macro)}项宏观 + {len(news)}条资讯")


if __name__ == "__main__":
    main()
    # 跟advisor.py同一个踩坑：Futu SDK连接开的非daemon线程会挡住进程自己退出，
    # 这个脚本每分钟跑一次，不能每次都靠外层timeout硬杀，显式flush后强制退出。
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
