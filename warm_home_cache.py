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

    ds.save_home_map_cache(snaps, global_idx)
    print(f"预热完成：{sum(len(v) for v in snaps.values())}条市场指数 + {len(global_idx)}条国际指数")


if __name__ == "__main__":
    main()
    # 跟advisor.py同一个踩坑：Futu SDK连接开的非daemon线程会挡住进程自己退出，
    # 这个脚本每分钟跑一次，不能每次都靠外层timeout硬杀，显式flush后强制退出。
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
