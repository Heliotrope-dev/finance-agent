"""投研顾问 —— 每个工作日收盘后扫一遍 A股/港股/美股全市场，用 Futu 的股票筛选器
按市值/估值/盈利增长挑出候选，基本面为主、技术面辅助，给买卖参考。私有工具，
不在 Streamlit 页面里，由 OpenClaw 的 stock-advisor cron 触发，走
`venv/bin/python3 advisor.py`，结果打印到 stdout 给 agent 读了转成微信消息。

跟 analysis.py 刻意"只讲事实不下结论"的公开页面定位不同——这里明确要给买卖
参考，所以判断函数（judge_stock）单独新写，不改 analysis.py 里现成的那几个。

第一版做过手动关注列表+A股候选池，当时用户反馈不需要这块，改成只扫港美股；
后来（2026-08-21）又要求把 A股 候选加回来，跟港美股同一套全市场量化初筛
逻辑——data_sources.py 里原有的"候选池"函数（get_index_top_movers 之类）
靠的是人气榜/硬编码知名股名单，覆盖面完全撑不起"全市场筛选"这个目标，改用
Futu SDK 自带的 get_stock_filter（按市值/PE/盈利增长这些真实指标在全市场
服务端筛选，不是本地维护的名单），实测 US 市场一次筛选能命中一千多只符合
条件的股票，HK 三百多只，是真正的全市场覆盖。A股 没有统一的 Futu 市场代码，
沪/深两个交易所分开筛选后合并成一个候选池（见 screen_candidates）。
"""

import json
import os
import re
import sys

# 2026-08-28踩的坑：这个脚本是被OpenClaw的cron当shell命令跑起来的（不是
# 交互式终端），Python在这种非tty环境下默认给stdout用"全缓冲"而不是"行
# 缓冲"——所有print()内容全部攒在内存缓冲区里，直到脚本快结束时才一次性
# 冲出来。这本身不影响脚本自己的逻辑（结尾os._exit前有显式flush，见
# __main__），但OpenClaw自己有一层"这个shell命令是不是卡住了"的监控，
# 盯的是"这个exec调用有没有输出"，不是"跑了多久"——真实故障：2026-08-28
# 17:30那次投研顾问，脚本本身在正常judge_watchlist（跑了大约9分钟），但
# 因为全缓冲一个字节都没输出，OpenClaw在869秒完全看不到进度后判定"卡死"，
# 直接把整个agent run杀了（AbortError: agent run aborted），当天微信简报
# 没发出去，不是脚本真的卡住或跑超时，是外层监控被"看起来卡住"骗了。改成
# 行缓冲，脚本里原有的那些print（"已回填X条""持仓判断X只已更新"...）会
# 实时冲出来，让外层监控全程看得到进度，不会再误判。
sys.stdout.reconfigure(line_buffering=True)

# 必须在任何可能引入 tqdm/streamlit 的 import 之前设置，减少 exec 工具读输出时
# 的噪音（详见下方 __main__ 里的说明）。
os.environ.setdefault("TQDM_DISABLE", "1")
import logging as _logging
_logging.getLogger("streamlit").setLevel(_logging.ERROR)

import queue as _queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed as _as_completed
from concurrent.futures import wait as _futures_wait
from concurrent.futures import TimeoutError as _FuturesTimeoutError
from datetime import datetime, timedelta

import futu as ft
import toml
from openai import OpenAI

import charts
import data_sources as ds
import tracker

_SECRETS_PATH = os.path.join(os.path.dirname(__file__), ".streamlit", "secrets.toml")
# 2026-08-22从DeepSeek切到千问——五个维度(金融判断/数学推理/代码/严格指令
# 遵循/中文表达)真实同题测过，千问全面不输、两项明显赢(数学题DeepSeek/智谱
# 都在预算内被截断算不完，千问不仅算完还给了实用换算；金融判断只有千问
# 主动提到了应收账款/存货周转天数这个细节)，价格还只有DeepSeek的一半不到，
# 而且实测不需要靠堆高max_tokens才能避免空内容(2000 tokens就能给出完整
# 回答)，没有DeepSeek那个"隐藏思考链吃预算"的老毛病。math-agent那边继续用
# DeepSeek，没有一起切，是用户单独决定的，不要顺手改过去。
_MODEL = "qwen3.8-flash"
# 2026-09-01切到百炼Token Plan订阅套餐专属端点——之前用的是DashScope通用
# 端点+账户级按量付费余额，账户余额一旦欠费(哪怕只差几毛钱)所有调用直接
# 403，跟买没买套餐无关；套餐本身有独立的Credits额度和专属Base URL/API Key，
# 走这个端点消耗的是套餐额度，不再受账户级欠费影响（前提是套餐本身没到期/
# 没用完）。qwen3.7-flash在这个套餐的可用模型列表里已经没有了，换成
# qwen3.8-flash（套餐列表里现在有的flash档位）。
_QWEN_BASE = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"

# 多空辩论跨供应商（2026-08-25）：用户要求辩论的多空双方不能是同一个模型
# 自己扮演两个角色，换成阿里千问（多头）+ 智谱（空头）两家真正独立的供应商，
# 可信度上比"同一个模型左右手互搏"更站得住——2026-08-22上面那条记录里
# 智谱数学题会被截断算不完，但那是"独立完整解题"场景对严谨性要求高；
# 这里只是写一段有立场的论证（不是最终判断，最终裁决还是回落到_client()/
# _MODEL这条主线），达不到那个门槛的要求，用在这里没问题。
_ZHIPU_MODEL = "glm-4-plus"
_ZHIPU_BASE = "https://open.bigmodel.cn/api/paas/v4"

# 2026-08-26：曾经短暂试过把辩论裁判换成DeepSeek第三方（裁判跟多头选手
# 同用千问，理论上有偏袒嫌疑），但DeepSeek账号余额被math-agent那边的
# 日常调用耗光、且团队决定这两个项目都彻底不用DeepSeek了（都切到千问，
# 详见math-agent仓库2026-08-26那次改动），裁判改回_client()/_MODEL——
# 见judge_stock_with_debate最后拍板那段。

# 候选池规模——用户要求"扩大到各500支"，让候选池统计（下面 _pool_summary）
# 真实反映大盘水平，不是拿十几只样本硬充"全市场"。Futu 单次最多返回200条，
# 超过要分页拉（见 _futu_screen_pool）。
_US_POOL_TARGET = 500
_HK_POOL_TARGET = 500
_A_POOL_TARGET = 500  # A股按沪/深两个交易所分别筛，各250凑够500

# 池子里最靠前的这些才交给AI逐一判断——控制AI调用次数和运行时长，不是对
# 全部500只跑判断（500只全跑一遍AI单次要几十分钟，不现实也没必要）。因为
# 池子本身已经按净利润增速降序排，取最前面这批本身就是"增长最快的一批"。
_US_CANDIDATE_CAP = 25
_HK_CANDIDATE_CAP = 25
_A_CANDIDATE_CAP = 25

# 2026-08-25：用户要求"样本大"但又不想让运行时间跟着候选数量线性暴涨
# （今天刚为了这个把cron超时从900s调到1500s）——加一层初筛：每个市场先
# 拉一个大得多的候选池(_TRIAGE_POOL_SIZE)，只用财务摘要（不查技术面/
# 新闻/价格位置，那三样才是真正耗时的部分）快速过一遍AI，一眼看出明显
# 不行的直接筛掉，剩下的再截到原来的CANDIDATE_CAP走完整判断流程——
# 完整判断这一步的规模没有变大，只是从"更大的池子里挑出来的更靠谱的
# CANDIDATE_CAP支"，总耗时理论上只多出初筛这一层的开销（轻量、纯文本、
# 不碰Futu）。
_TRIAGE_POOL_SIZE = 100

# 2026-08-25：三市场混排的综合得分排行榜大小，取代原来"每个市场固定Top3"
# 的_TOP_PICKS/_top_picks逻辑（已删除，不再有调用方）。
_LEADERBOARD_SIZE = 10

# 2026-08-28：首页"推荐股排行榜"用的观察池——用户明确要求"不是定死，是
# 随着热度排行榜变化的"：每天用真实的热度/涨跌幅榜重新取一遍前60美股/
# 40港股/20A股（合计120支，2026-08-28从20/20/10=50支扩容），不是写死的
# 名单。港股/A股用get_index_top_movers（内部已经有"热度榜挂了退回涨跌幅榜"
# 的兜底，见该函数docstring），美股没有真正的热度榜数据源（见
# get_us_famous_movers的docstring），退而求其次用_US_FAMOUS_CODES这份知名
# 股名单按当天涨跌幅重新排序，好歹能做到"名单本身不变但每天的排名会变"。
# 扩容到120支后_US_FAMOUS_CODES/_HK_FAMOUS_CODES这两份兜底名单也各自补了
# 股票，保证就算热度榜/Futu都不可用，兜底路径也有足够的股票数覆盖目标
# 规模（见这两份名单各自的"2026-08-28"注释）。就算某支股票今天从热度榜
# 掉出去了，前一天给它的判断依然要在到期后正常回填价格算对错（见
# _backfill_due_advice/get_due_for_advice_review，回填逻辑按advice表里
# 已有的行走，不依赖这支股票还在不在当天的观察池里）。
_WATCHLIST_TARGET_SIZE = {"US": 60, "HK": 40, "A": 20}


def _build_watchlist() -> list[dict]:
    """三个市场各自的get_index_top_movers独立跑，用_run_concurrent_with_deadline
    包一层超时——港股那条路径（东财人气榜接口）真实故障时不是干脆报错，是
    整个请求挂住不返回（底层requests调用没设超时，_with_retry的重试/退避
    压根等不到第一次调用失败），如果不加这层保护，某天港股那边一卡，会把
    "每天17:30必须跑完"的整条cron拖死。三个市场互不依赖，并发跑，一个卡住
    不影响另外两个market正常出结果——某个市场这次超时/失败，观察池那天就
    少这个市场的股票，不是空手硬凑，也不该让整个watchlist流程陪着一起卡死。

    timeout=150：2026-08-29查过一次"港股这次0支是不是接口挂了"，结果不是
    挂了，是这个接口本身就慢——实测跑了92秒才拿到39条数据，跟原来这里给的
    45秒超时对比，等于每次运行都会在它快跑完之前被这层保护自己先掐断，
    "超时保护"反而成了"必定超时"。不是接口不稳定，是超时阈值定得比接口
    正常耗时还短。改成150秒，给到接近2倍实测耗时的余量。
    """
    def _fetch(market_n):
        market, n = market_n
        return ds.get_index_top_movers(market, limit=n)

    results = _run_concurrent_with_deadline(
        list(_WATCHLIST_TARGET_SIZE.items()), _fetch, timeout=150, max_workers=3,
    )
    items = []
    for market, df in ((m, results.get(i)) for i, (m, _n) in enumerate(_WATCHLIST_TARGET_SIZE.items())):
        if df is None or df.empty:
            continue
        for _, r in df.iterrows():
            items.append({"symbol": str(r["代码"]), "market": market, "name": r.get("名称", "") or ""})
    return items


# 首页排行榜只展示前5——用户明确要求"十个太多了就五个好了"，跟_LEADERBOARD_
# SIZE（私人WeChat简报里全市场扫描那份榜单，仍然是10）分开维护，不共用一个
# 常量，两份榜单的受众和取舍标准不一样。
_WATCHLIST_LEADERBOARD_SIZE = 5

# 观察池每天都会重新判断一遍（不是"判断一次以后就不动了"），所以事后校验
# 没必要等7天——次日核对足够，用户明确要求"次日验证、再给新预测"这个每日
# 循环。跟持仓/全市场扫描那两个source沿用的7天窗口分开（那两个场景是"这次
# 判断能不能扛得住一段时间"，跟这里"每天一个新判断"的定位不同）。
#
# 2026-08-30修复：原来是1（整24小时）——但cron每天固定同一时刻启动，
# watchlist判断排在main()最后，写库时间总比当天cron启动时刻晚10-20分钟，
# 导致"距上次cron启动正好24小时"这个cutoff结构性地永远卡在昨天watchlist
# 落库时间之前，实测命中数永远是0（详见_backfill_due_advice里的注释）。
# 改成0.9天（21.6小时），留出吸收这个固定偏移量的余量。
_WATCHLIST_REVIEW_MIN_AGE_DAYS = 0.9


def _load_secrets_into_env():
    """脚本独立运行（不在 streamlit run 里），data_sources.py 的 st.cache_data
    这类 Streamlit API 在无 runtime 环境下能自动降级工作（用内存缓存兜底），
    但 QWEN_API_KEY 这类配置本来是靠 st.secrets 读的，这里读不到——照抄
    app.py/_math_page.py 已有的"把 secrets.toml 灌进 os.environ"模式，让下面
    _client() 的 os.environ fallback 生效。venv 是 Python 3.10，没有 tomllib
    （3.11+ 才有），用 toml 包（streamlit 自身依赖链里就有，不用额外装）。
    """
    if os.environ.get("QWEN_API_KEY"):
        return
    try:
        secrets = toml.load(_SECRETS_PATH)
        for k, v in secrets.items():
            os.environ.setdefault(k, str(v))
    except FileNotFoundError:
        pass


_load_secrets_into_env()
_EMAIL = os.environ.get("ADVISOR_EMAIL", "")  # 私人工具，固定单用户，不做多用户


def _is_insufficient_balance_error(exc: Exception) -> bool:
    """识别"账号欠费/余额不足"这一类错误，跟网络超时/参数错误这些普通失败
    区分开——2026-08-26排查DeepSeek余额耗尽时真实见过的错误格式是
    `openai.APIStatusError: Error code: 402 - {'error': {'message':
    'Insufficient Balance', ...}}`；查阿里云官方错误码文档确认千问
    (DashScope)这边欠费的特征是HTTP 400、错误码"Arrearage"。两家measure
    格式不完全一样，都用字符串关键词匹配而不是死抠某个SDK版本的异常属性
    结构（openai SDK不同版本APIStatusError的body解析方式可能变，字符串
    表示更稳）。"""
    text = str(exc)
    return "Arrearage" in text or "Insufficient Balance" in text or "insufficient_quota" in text.lower()


def _check_qwen_balance():
    """main()一开始就先探测一次千问账号是否欠费——这个脚本几乎所有AI调用
    (screen候选判断/持仓判断/组合分析)都走千问，一旦欠费，与其让整个流程
    一步步跑到底、每一步AI调用各自失败又各自被局部try/except吞掉、最后
    只留下一句含糊的"这次没有从候选池里判断出任何有效结果"（跟Futu筛选
    失败/AkShare故障长得一模一样，没法区分），不如一开始花一次几乎零成本
    的探测调用(max_tokens=5)提前问清楚，欠费就直接终止、打印一条不会被
    误认成别的问题的明确消息，省下后面整趟徒劳的Futu/AkShare/AI调用。

    只拦截"确认是欠费"这一种情况——探测调用本身如果因为网络抖动等其它
    原因失败，不能因此判定"欠费"进而阻断整次正常运行，静默放行即可，
    真正的AI调用失败自然会在后面各自的环节暴露。
    """
    try:
        _client().chat.completions.create(
            model=_MODEL, messages=[{"role": "user", "content": "ping"}], max_tokens=5, stream=False,
        )
    except Exception as e:
        if _is_insufficient_balance_error(e):
            raise RuntimeError(
                f"紧急：QWEN_API_KEY 账号余额不足/欠费，投研顾问本次运行直接终止，"
                f"不再浪费后面的Futu/AkShare/AI调用。请立即到阿里云百炼控制台"
                f"（https://bailian.console.aliyun.com/?tab=model#/costing-balance）充值。"
                f"原始错误：{e}"
            ) from e
        # 其它失败（网络抖动等）不阻断——真正的调用失败会在后面各环节暴露。


def _client() -> OpenAI:
    key = os.environ.get("QWEN_API_KEY", "")
    if not key:
        raise RuntimeError("未配置 QWEN_API_KEY。")
    # 踩坑记录（DeepSeek时期留下的，千问目前没复现过，但保留这层超时保护
    # 不吃亏）：没设timeout时，账号余额不足这类快速失败的错误在openai SDK
    # 默认重试逻辑下可能会挂很久不返回——不是真的在处理，是客户端卡在某个
    # 没有时间上限的等待/重试循环里。显式给60秒超时，快速失败好过整批
    # 候选全部在同一个坑里陪跑到_run_concurrent_with_deadline的400秒外层超时。
    return OpenAI(api_key=key, base_url=_QWEN_BASE, max_retries=2, timeout=60)


def _zhipu_client() -> OpenAI | None:
    """空头辩论用的独立供应商客户端——没配key或初始化失败时返回None，
    调用方（_fetch_stance）据此回落到_client()，不让整个辩论功能因为
    一家供应商的配置问题而全部报废。"""
    key = os.environ.get("ZHIPU_API_KEY", "")
    if not key:
        return None
    try:
        return OpenAI(api_key=key, base_url=_ZHIPU_BASE, max_retries=1, timeout=60)
    except Exception:
        return None


def _run_concurrent_with_deadline(
    items: list, fn, timeout: float, max_workers: int = 8, progress_label: str = "",
) -> dict:
    """跟 app.py 里同名函数逻辑一致的小拷贝——那个函数本身不依赖 Streamlit
    state，但定义在 app.py 里，这个脚本不方便 import 整个 app.py（会连带跑
    Streamlit 页面配置代码），抄一份轻量独立版本，语义和踩坑记录见 app.py
    原版的 docstring：统一 deadline（从 submit 那一刻算起，不是逐个
    future.result(timeout=N)），避免总耗时失控。

    progress_label：2026-08-28新增——不传就是原来的行为（用wait()一次性等，
    中间不打印）。传了的话改用as_completed逐个消费，每完成10个就打印一行
    进度。这是给这批股票数量比较多、单批judge可能要跑很久的调用点用的：
    真实故障是OpenClaw那边有个"这个shell命令是不是卡住了"的监控，只看
    "这个exec调用有没有输出"，跟脚本自己的deadline没关系——一批judge如果
    跑十几分钟中间一个字都不打印，会被外层监控误判成卡死直接杀掉整个进程
    （2026-08-28 17:30那次投研顾问真实故障，见sys.stdout.reconfigure那条
    注释）。加了单支热门股观察池扩到120支之后单批judge时间更长，风险更高，
    这里补上周期性输出，不能只指望调用方自己在批次前后各打一行。
    """
    results: dict[int, object] = {}
    if not items:
        return results
    ex = ThreadPoolExecutor(max_workers=min(max_workers, len(items)))
    futures = {ex.submit(fn, item): i for i, item in enumerate(items)}
    if not progress_label:
        done, _not_done = _futures_wait(list(futures.keys()), timeout=timeout)
        for fut in done:
            try:
                results[futures[fut]] = fut.result()
            except Exception:
                pass
    else:
        deadline = time.time() + timeout
        done_count = 0
        try:
            for fut in _as_completed(list(futures.keys()), timeout=timeout):
                try:
                    results[futures[fut]] = fut.result()
                except Exception:
                    pass
                done_count += 1
                if done_count % 10 == 0 or time.time() >= deadline:
                    print(f"（{progress_label}：{done_count}/{len(items)} 完成…）")
        except _FuturesTimeoutError:
            print(f"（{progress_label}：{done_count}/{len(items)} 完成，到达{timeout:g}秒截止线，未完成的这批不再等）")
    ex.shutdown(wait=False, cancel_futures=True)
    return results


def _futu_call_with_timeout(fn, timeout: float = 30, default=None):
    """在独立线程里创建 Futu 连接 + 执行调用 + 关闭，全程同一个线程完成——
    遵守 Futu SDK "连接和调用必须同一线程" 的硬规则（data_sources.py 的
    `_futu_worker_loop`/`_run_with_timeout` 两处 docstring 里反复强调的同一
    个教训：连接在一个线程建、调用在另一个线程发，SDK 内部状态会错乱直接
    卡死不返回）。主线程只是排队等结果，等不到就超时返回 default，不会跨
    线程碰那个连接对象。

    实测复现过的真实故障：2026-08-21 生产环境定时 cron 跑 advisor.py 时，
    Futu 行情连接反复断开又重连，卡在拉行情这一步 6 分钟没有任何响应，
    最后被 OpenClaw 的 900 秒 agent 超时强制杀掉——当天投研简报完全没有
    生成，比"结果差一点"更糟，是"整个功能当天完全没跑出来"。这个函数是
    补这个洞：单次 Futu 调用最多等 timeout 秒，超时就放弃这一步（fn 所在的
    worker 线程会被丢弃，不强杀——Python 没有安全的跨线程强制终止手段，
    daemon=True 保证进程退出时不会被这个残留线程拖住），脚本还能继续往下
    走，不会被外层整体 abort 导致颗粒无收。
    """
    q = _queue.Queue(maxsize=1)

    def _worker():
        try:
            ctx = ft.OpenQuoteContext(host="127.0.0.1", port=11111)
        except Exception:
            q.put(default)
            return
        try:
            q.put(fn(ctx))
        except Exception:
            q.put(default)
        finally:
            try:
                ctx.close()
            except Exception:
                pass

    threading.Thread(target=_worker, daemon=True).start()
    try:
        return q.get(timeout=timeout)
    except _queue.Empty:
        return default


def _futu_screen_pool(market, quarter, cap_threshold: float, target_count: int) -> tuple[list[dict], int]:
    """在 market 全市场范围内按"市值下限 + PE在合理区间(0-50，排除亏损股和
    极端高估值) + 最近一期净利润同比增速>10%"筛选，按增速降序排，分页拉到最多
    target_count 条（Futu 单次最多返回200条一页，一次拉不完500条要翻页）。
    这三个字段是实测验证过 Futu 账号权限下能用的（VOLUME/换手率这类字段测试
    时报"不支持该过滤字段"，权限或字段类型不对，没有踩着编）。市值字段/PE
    走 SimpleFilter，净利润增速走 FinancialFilter——两者要分开建，混用会报
    "不支持该过滤字段"（实测确认过）。

    港股和A股的FinancialFilter不支持MOST_RECENT_QUARTER这个quarter选项（实测
    报错"港股和A股不支持最近季报选项"），只能用美股。这里quarter参数由调用方
    按市场传对应支持的枚举。

    返回 (候选列表, 全市场实际符合条件总数)——后者用于_pool_summary里如实
    报告"全市场有多少只符合门槛"，不是编的数字。列表里带上market_val/pe_ttm
    原始值，供_pool_summary算真实的统计摘要（中位数PE等），不用另外调AI。
    """
    f_cap = ft.SimpleFilter()
    f_cap.stock_field = ft.StockField.MARKET_VAL
    f_cap.filter_min = cap_threshold
    f_cap.is_no_filter = False

    f_pe = ft.SimpleFilter()
    f_pe.stock_field = ft.StockField.PE_TTM
    f_pe.filter_min = 0
    f_pe.filter_max = 50
    f_pe.is_no_filter = False

    f_growth = ft.FinancialFilter()
    f_growth.stock_field = ft.StockField.NET_PROFIX_GROWTH
    f_growth.filter_min = 10
    f_growth.is_no_filter = False
    f_growth.quarter = quarter
    f_growth.sort = ft.SortDir.DESCEND

    filters = [f_cap, f_pe, f_growth]
    # Futu的get_stock_filter market参数没有统一的"A股"选项，沪/深要分开传
    # (ft.Market.SH/ft.Market.SZ)，但项目里A股统一用"A"这个market_code，
    # 两个交易所都映射到"A"，调用方各自merge成一个候选池。
    market_code = {ft.Market.HK: "HK", ft.Market.SH: "A", ft.Market.SZ: "A"}.get(market, "US")
    page_size = 200  # 实测确认的单次请求上限，超过会报"请求个数超过限制"
    items: list[dict] = []
    all_count = 0
    begin = 0
    while len(items) < target_count:
        _begin = begin  # 闭包捕获循环变量要显式拷贝一份，不然下面的lambda全部指向同一个begin
        result = _futu_call_with_timeout(
            lambda ctx, _b=_begin: ctx.get_stock_filter(market=market, filter_list=filters, begin=_b, num=page_size),
            timeout=30,
        )
        if result is None:
            break
        ret, data = result
        if ret != ft.RET_OK:
            break
        _last_page, all_count, ret_list = data
        if not ret_list:
            break
        for item in ret_list:
            symbol = item.stock_code.split(".", 1)[-1]
            # 美股候选池排除非赞助存托凭证(unsponsored ADR)——2026-08-25真实
            # 筛出过CWQXY(瑞典地产)、RKGRY(德国防务)、VONOY(德国房企)这类：
            # 场外流动性差、买卖价差大，不是普通人开美股账户买得到/卖得掉的
            # "美股"，纯数值筛选(市值/PE/增速)完全过滤不掉这类。OTC市场对
            # 非赞助Level 1 ADR有个通用命名惯例——5位字母代码、以Y结尾，
            # 不是100%精确(极少数正常股票码也长这样)，但对这类问题的命中率
            # 很高，宁可漏判几个也不要把这类流动性陷阱当成推荐标的。
            if market_code == "US" and len(symbol) == 5 and symbol.isalpha() and symbol.endswith("Y"):
                continue
            items.append({
                "symbol": symbol,
                "name": item.stock_name,
                "market": market_code,
                "market_val": getattr(item, "market_val", None),
                "pe_ttm": getattr(item, "pe_ttm", None),
            })
        begin += page_size
        if begin >= all_count:
            break
    return items[:target_count], all_count


def _pool_summary(pool: list[dict], all_count: int, label: str) -> str:
    """候选池的真实统计摘要——不是AI编的，直接从Futu返回的原始数据本地算，
    跟这个项目"技术面信号本地算不靠AI编"的一贯做法一致。

    A股不走这套（见_a_share_candidate_pool的docstring：账号没有A股行情
    权限，候选来源也不是市值/PE/盈利增速的数值筛选），单独给一句如实的
    说明，不能沿用下面这句"全市场符合筛选门槛"的措辞——那是港美股Futu
    筛选的真实过程，A股照抄这句等于编了一个没发生过的筛选流程。
    """
    if label == "A股":
        if not pool:
            return "A股：本次没有拿到有效候选（涨停股池/热门板块数据源可能临时故障）。"
        return f"A股：候选来自今日涨停股池+热门板块成分股（已剔除ST），共 {len(pool)} 只——不同于港美股的市值/PE/盈利增速数值预筛（Futu账号无A股行情权限），基本面判断交给AI逐支读取真实财报后给出，详见代码注释。"
    if not pool:
        return f"{label}：本次没有拿到有效候选（Futu筛选失败或无符合条件的股票）。"
    pes = sorted(p["pe_ttm"] for p in pool if p.get("pe_ttm"))
    caps = sorted(p["market_val"] for p in pool if p.get("market_val"))
    pe_med = pes[len(pes) // 2] if pes else None
    cap_med = caps[len(caps) // 2] if caps else None
    parts = [f"{label}全市场符合筛选门槛（市值达标、估值0-50倍PE、最近盈利增速>10%）的股票共 {all_count} 只"]
    parts.append(f"本次候选池覆盖其中 {len(pool)} 只")
    if pe_med:
        parts.append(f"PE中位数约{pe_med:.1f}倍")
    if cap_med:
        parts.append(f"市值中位数约{cap_med / 1e8:.0f}亿")
    return "，".join(parts) + "。"


def _a_share_candidate_pool(target_count: int) -> tuple[list[dict], int]:
    """A股候选池——不走_futu_screen_pool。2026-08-25实测发现连的Futu账号
    没有A股行情权限（get_stock_filter对ft.Market.SH/SZ直接返回"A股市场
    股票行情权限不足"），screen_candidates()里这部分之前一直静默失败返回
    空池子，首页A股Top3从来没真正出过数据——跟README"值得一提的踩坑"里
    记录的"公式检索静默退化"是同一类问题，又中了一次。

    换用AkShare，但不是简单换个数据源做同样的市值/PE/盈利增速数值筛选：
    ak.stock_zh_a_spot_em()（全市场几千只股票快照）现场实测直接被远端
    reset连接失败，而且get_index_top_movers的踩坑记录里写过这个接口
    "之前用过、实测单次要接近2分钟"，项目里早就为了这个原因弃用了它。
    改用项目里已经验证过快且稳定的两个数据源做候选发现（涨停股池+热门
    板块成分股，跟get_index_top_movers的A股实现同源）：涨停股是今天最强
    的动量信号，热门板块成分股补充题材多样性，避免候选全扎堆同一题材。
    不做PE/市值预筛——基本面判断交给后面AI阶段：_judge_one会调
    _financial_summary_text读真实财报文本喂给AI，跟这份候选列表要不要
    带pe_ttm/market_val无关（判断逻辑本来就不依赖候选池自带这两个字段，
    只用symbol/name/market）。
    """
    seen: set[str] = set()
    items: list[dict] = []

    def _is_st(name: str) -> bool:
        return "ST" in name.upper()

    try:
        limit_df = ds.get_limit_pool("up", target_count)
    except Exception:
        limit_df = None
    if limit_df is not None and not limit_df.empty:
        for _, row in limit_df.iterrows():
            code, name = str(row.get("代码", "")), str(row.get("名称", ""))
            if not code or code in seen or _is_st(name):
                continue
            seen.add(code)
            items.append({"symbol": code, "name": name, "market": "A"})

    if len(items) < target_count:
        try:
            sectors = ds.get_hot_sectors("A", limit=5)
        except Exception:
            sectors = None
        if sectors is not None and not sectors.empty:
            for sector_name in sectors["板块"].tolist():
                if len(items) >= target_count:
                    break
                try:
                    cons = ds.get_sector_constituents("A", sector_name, limit=10)
                except Exception:
                    continue
                if cons is None or cons.empty:
                    continue
                for _, row in cons.iterrows():
                    if len(items) >= target_count:
                        break
                    code, name = str(row.get("代码", "")), str(row.get("名称", ""))
                    if not code or code in seen or _is_st(name):
                        continue
                    seen.add(code)
                    items.append({"symbol": code, "name": name, "market": "A"})

    return items[:target_count], len(items)


_TRIAGE_SYSTEM = """你是投研初筛助理，任务是只凭财务摘要快速判断一支股票"值不值得
花更多时间做深入分析"（深入分析包括技术面、新闻、52周价格位置、多空辩论，
比这一步昂贵很多）。

这一步的目的是筛掉一眼就能看出明显不行的，不是替代后面的完整判断——
不确定的、看着有点意思但数据还不够的，一律放行进入深入分析，不要在这一步
过度谨慎，宁可多放一些进去，也不要因为初筛太严把真正有潜力的标的筛没了。

明显不值得深入分析的例子（不是穷举）：
- 连续多期持续巨额亏损，且没有任何边际改善的迹象（毛利率/现金流都在恶化）
- 财务数据缺失或异常到根本没法读出任何有效信息
- 营收利润双双持续大幅下滑，找不到任何企稳信号

严格按格式输出，不要多余的话：
值得深入：[是/否]
理由：<一句话，不超过30字>
"""


def _quick_triage(item: dict) -> dict:
    """初筛单支——只查财务摘要（纯文本/AkShare，不碰Futu，天然快），
    用主线模型走一次轻量调用。失败/超时按"放行"处理，不是按"筛掉"处理——
    初筛机制本身出问题，不该连带损失掉这支股票被完整分析的机会，宁可
    这一层失效退化成"不初筛"，也不要因为初筛环节的故障静默漏判。"""
    symbol, market, name = item["symbol"], item.get("market", "US"), item.get("name", "")
    try:
        fin = _financial_summary_text(symbol, market)
        if not fin:
            return {**item, "_triage_pass": True}
        resp = _client().chat.completions.create(
            model=_MODEL,
            messages=[
                {"role": "system", "content": _TRIAGE_SYSTEM},
                {"role": "user", "content": f"股票：{name}（{symbol}）\n\n财务摘要：\n{fin}"},
            ],
            max_tokens=150,
            temperature=0.2,
            stream=False,
        )
        text = resp.choices[0].message.content or ""
        passed = "值得深入：是" in text or "值得深入:是" in text or ("否" not in text.split("\n", 1)[0])
        return {**item, "_triage_pass": passed}
    except Exception:
        return {**item, "_triage_pass": True}


def _triage_pool(pool: list[dict], keep_n: int) -> list[dict]:
    """对一个市场的候选池跑初筛，返回通过初筛、且截到keep_n条的列表——
    截断顺序沿用pool原本的顺序（按增速/热度降序排好的），初筛只做"去掉
    明显不行的"，不改变排序逻辑本身。"""
    if not pool:
        return pool
    # 实测记录（2026-08-25）：100支候选、并发10、180秒超时——180秒打满，
    # 只有约25支真正跑完初筛，其余全靠fail-open放行凑数，等于没真正筛。
    # 瓶颈是_financial_summary_text这一步的网络请求（AkShare接口本身的
    # 延迟/限流退避），不是AI调用本身。加大并发到20、超时放宽到300秒。
    results = _run_concurrent_with_deadline(pool, _quick_triage, timeout=300, max_workers=20)
    # 没在results里出现的下标=这个位置的初筛调用整批超时被drop了，同样按
    # "放行"处理（原始pool[i]本身，不是results里的值），跟_quick_triage
    # 内部try/except那条一样的原则：初筛机制故障不该导致这支股票被静默
    # 漏判，不能因为超时集合被排除在外就悄悄丢了。
    passed = [results[i] if i in results else pool[i] for i in range(len(pool))
              if results.get(i, {}).get("_triage_pass", True)]
    return passed[:keep_n]


def _leaderboard(judged: list[dict], n: int = _LEADERBOARD_SIZE, market_cap: int | None = None) -> list[dict]:
    """2026-08-25新增：三个市场混排的综合得分排行榜，取代"每个市场固定
    前3"——用户明确要求数量不用锁死、好的自然上榜、不好的不硬凑。只取有
    score的条目参与排名（None的排不进去，不是judge_stock没给，就是解析
    失败，不能当0分处理，见tracker.get_latest_leaderboard同一条注释）。

    market_cap（2026-09-01新增，默认None不生效）：某个市场当天判断出来的
    分数普遍偏高时，纯按分数排序会让前N名被同一个市场包圆——用户反馈过
    两次"首页推荐股排行榜/微信简报怎么全是港股"，不是bug，是这几个候选
    池那天恰好某个市场分数普遍更高，但纯score排序体现不出"给用户看的这份
    名单该覆盖多个市场"这个诉求。market_cap给一个每个市场最多能占的席位数
    上限，超过的名额让给分数稍低但来自其他市场的候选——只在明确要展示
    "跨市场"名单的场合传这个参数（首页推荐股排行榜、微信简报的港美股前5），
    "综合得分排行榜Top10"这种本来就是"好的自然上榜"定位的场合不传，维持
    原样不受影响。"""
    scored = [j for j in judged if j.get("score") is not None]
    scored.sort(key=lambda j: j["score"], reverse=True)
    if market_cap is None:
        return scored[:n]
    result: list[dict] = []
    market_count: dict[str, int] = {}
    for j in scored:
        if len(result) >= n:
            break
        m = j.get("market")
        if market_count.get(m, 0) >= market_cap:
            continue
        result.append(j)
        market_count[m] = market_count.get(m, 0) + 1
    return result


def judge_watchlist() -> list[dict]:
    """每天用真实热度/涨跌幅榜重新取一批热门股（_build_watchlist，规模见
    _WATCHLIST_TARGET_SIZE），给首页"推荐股排行榜"用——跟screen_candidates()
    不同，这里不需要_futu_screen_pool/_triage_pool那一整套"从几千支里筛出
    候选"的流程，_build_watchlist()取回来的就是最终要判断的这一批，直接
    并发跑judge_stock。

    2026-08-28的真实故障纠偏：这个函数早先调过几轮timeout/max_workers，
    当时以为瓶颈是"要在cron的1500秒总预算内跑完"，但17:30那次真实故障
    （见sys.stdout.reconfigure那条注释）查出来根本不是超时预算问题——
    是OpenClaw自己的"这个shell命令是不是卡住了"监控只看有没有输出，
    advisor.py全程一个字不打印导致869秒静默后被误判成卡死杀掉，跟脚本
    实际跑了多久没关系。真正的修复是sys.stdout.reconfigure(line_buffering
    =True) + _run_concurrent_with_deadline的progress_label参数（每完成10个
    打一行进度），只要中间持续有输出，这个函数本身可以跑得比较久也不会被
    误杀。搞清楚这点之后，workers/timeout就不用再为了"赶预算"束手束脚，
    改成给足够的并发（20）和宽松的超时上限（900秒，正常情况下用不到，只是
    兜底），换判断完整度。"""
    watchlist = _build_watchlist()
    print(f"（观察池取到{len(watchlist)}支，开始逐支AI判断…）")
    results = _run_concurrent_with_deadline(
        watchlist, lambda it: _judge_one(it, "watchlist"), timeout=900, max_workers=20,
        progress_label="观察池AI判断进度",
    )
    return [results[i] for i in sorted(results) if results[i] and "error" not in results[i]]


def screen_candidates() -> dict:
    # 每个市场各自的_futu_screen_pool内部已经用_futu_call_with_timeout做了
    # 超时保护（每页最多等30秒），不需要在这里再维护一个跨两个市场共用的
    # ctx——共用连接省下的那点建连开销，跟"任何一次调用卡住会拖累后面所有
    # 请求"这个风险比，完全不值得，2026-08-21生产环境的真实故障就是前车之鉴。
    us_pool, us_all = _futu_screen_pool(
        ft.Market.US, ft.FinancialQuarter.MOST_RECENT_QUARTER, 2_000_000_000, _US_POOL_TARGET
    )
    hk_pool, hk_all = _futu_screen_pool(
        ft.Market.HK, ft.FinancialQuarter.ANNUAL, 5_000_000_000, _HK_POOL_TARGET
    )
    # A股不走Futu——账号没有A股行情权限，见_a_share_candidate_pool的docstring。
    a_pool, a_all = _a_share_candidate_pool(_TRIAGE_POOL_SIZE)
    print(f"（候选池拉取完成：美股{len(us_pool)}/港股{len(hk_pool)}/A股{len(a_pool)}，开始初筛…）")

    # 初筛：每个市场先从大得多的池子（_TRIAGE_POOL_SIZE）里筛掉一眼不行的，
    # 只用财务摘要、不碰Futu，筛完再截到原来的CANDIDATE_CAP，交给下面昂贵的
    # 完整判断（技术面+新闻+价格位置）——完整判断这一步的规模没变。三个市场
    # 各自的_triage_pool内部已经并发了，但三次调用本身原来是顺序跑的，等于
    # 白白把三个市场的初筛时间加总——用线程分别起，三个市场的初筛并行进行，
    # 总耗时约等于最慢那个市场的初筛时间，不是三个市场相加。
    with ThreadPoolExecutor(max_workers=3) as _triage_ex:
        _us_fut = _triage_ex.submit(_triage_pool, us_pool[:_TRIAGE_POOL_SIZE], _US_CANDIDATE_CAP)
        _hk_fut = _triage_ex.submit(_triage_pool, hk_pool[:_TRIAGE_POOL_SIZE], _HK_CANDIDATE_CAP)
        _a_fut = _triage_ex.submit(_triage_pool, a_pool, _A_CANDIDATE_CAP)
        us_shortlist = _us_fut.result()
        hk_shortlist = _hk_fut.result()
        a_shortlist = _a_fut.result()

    shortlist = us_shortlist + hk_shortlist + a_shortlist
    print(f"（初筛完成：{len(shortlist)}支进入完整判断，开始逐支AI分析…）")
    judged: list[dict] = []
    if shortlist:
        results = _run_concurrent_with_deadline(
            shortlist, lambda it: _judge_one(it, "screen"), timeout=400, max_workers=5,
            progress_label="全市场候选AI判断进度",
        )
        judged = [results[i] for i in sorted(results) if results[i] and "error" not in results[i]]

    return {
        "us_pool": us_pool, "hk_pool": hk_pool, "a_pool": a_pool,
        "us_all_count": us_all, "hk_all_count": hk_all, "a_all_count": a_all,
        "judged": judged,
    }


def _extract_action(text: str) -> str:
    """从AI输出里解析"结论：XXX"这一行的动作——真实踩过的坑：prompt里的格式
    示例写的是"结论：[买入/卖出/持有/观望]"（方括号是"四选一"的记号，不是要求
    照抄），但DeepSeek偶尔会把方括号也原样输出成"结论：[持有]"，用精确子串匹配
    "结论：持有"就完全匹配不上，会静默落回默认值"观望"——这是判断明明是"持有"
    却被记成"观望"的真实数据错误，不是无害的格式问题，会污染后续的排序/统计。
    改用正则，方括号/全半角冒号/前后空格都容错。"""
    m = re.search(r"结论[：:]\s*[\[【]?\s*(买入|卖出|持有|观望)\s*[\]】]?", text)
    return m.group(1) if m else "观望"


def _extract_score(text: str) -> int | None:
    """解析"综合得分：XX"这一行，跟_extract_action同一个套路——正则容错
    方括号/全半角冒号/前后空格，解析不到返回None（调用方按"这条没有分数、
    排行榜排最后"处理，不要默认成0分——0分意味着"不具备任何买入逻辑甚至
    有卖出信号"，那是一个真实的判断结论，跟"没解析到分数"完全是两回事，
    默认成0会把解析失败误判成最差评级，污染排行榜。"""
    m = re.search(r"综合得分[：:]\s*(\d{1,3})", text)
    if not m:
        return None
    score = int(m.group(1))
    return max(0, min(100, score))


_JUDGE_SYSTEM = """你是一位理性、保守的投研助理，服务对象是一位会自己做最终决策的
个人投资者——你的任务是基于给定的真实数据给出参考判断，不是替他下单。

要求：
1. 先看基本面（盈利能力、营收/利润增长趋势、负债水平、估值是否处于合理区间），
   这是判断的主要依据。
   - 如果下方给了"估值"数据（PE/PB及其历史分位），必须具体引用这个数字
     （比如"PE(TTM)处于近3年历史分位约X%"），不能只说"估值合理"这类空泛
     评价——分位数据是本地算好的真实值，不是编的；如果这次没给出历史分位、
     只有静态倍数（美股常见），也要如实说明"缺少相对自身历史的参照"，不能
     假装有分位数据。
   - 增长数字要甄别质量，不能拿到就直接当结论用：如果营收/净利润同比增速是
     三位数（100%+）这类异常大的数字，要主动判断这更像是低基数效应/一次性
     损益（比如资产处置、政府补贴、汇兑收益、上年同期基数极低）驱动的，还是
     核心主营业务结构性改善（毛利率、经营现金流是否同步跟着改善，是判断标尺
     之一）——判断不出来就如实说"数据不足以判断增长质量"，不能把三位数增速
     直接等同于"业绩反转确立"，这是常见的误判来源。
   - 强周期行业（航运、大宗商品、半导体存储、原材料）要额外考虑当前处于
     周期的什么阶段（供给端还是需求端驱动、行业整体是不是也在同向变化），
     不能只看这一家公司自己的数字。
2. 技术面信号只作为买卖时机的辅助确认，不能单独作为理由。
3. 必须把"价格所处的位置"当成独立于基本面之外的第三个判断维度，不能只看财报
   数字好不好看就下结论——同样是"基本面好"，现价接近52周高点、或者相对52周
   低点已经涨了几倍甚至几十倍，跟现价刚从低位启动，风险回报比完全不是一回事。
   价格位置数据是本地算好的真实52周高低点，不是编的，必须引用具体数字（比如
   "现价较52周高点回撤X%"或"是52周低点的X倍"），但**具体怎么表达这个判断、
   用什么句式，要你自己根据这家公司的实际情况组织语言，不要每支股票都套用
   同一套句式和同一套论证顺序**——不同行业、不同基本面强弱组合，价格位置这
   件事在整个判断里该占多大分量是不一样的，你要体现出真的针对这家公司想过，
   不是机械地把三段模板拼起来。
4. 结论必须是以下四选一：买入 / 卖出 / 持有 / 观望。基本面不过关的，即使技术面
   好看也不能给买入；数据不足以支撑判断时选观望，不要勉强给结论。
5. 明确写清楚为什么——依据是财报里的哪一点、技术面的哪个信号、价格位置怎么样，
   不要空泛地说"综合来看""值得关注"这类没有信息量的话。
6. 只能用给定的数据做判断，不能编造任何数字或消息。
7. 结论是"买入"时，必须给一个具体的止损参考位（比如"若跌破X元/较建仓价回撤
   超过X%应考虑止损"），不能只说"控制仓位""注意风险"这类没有具体数字的空话——
   跟持仓判断要求给卖出触发条件是同一个道理，新买入同样需要一个明确的退出
   纪律，不是买了就不管。
8. 除了四选一的结论，还要给一个0-100的综合得分，用来跨市场、跨股票统一
   排名。为了让不同评分人给出的分数真的能放在一起比较，不是各凭印象打分，
   综合得分必须由下面四个维度分别打分后加总得到，不能跳过拆解直接给一个
   总分：
   - 基本面质量（0-40分）：盈利能力、增长的真实性（结构性改善还是一次性
     因素驱动）、负债水平、所处行业周期位置。40分附近=数据扎实、增长可
     持续、负债健康；20分附近=有明显硬伤或数据不足以下判断；0分附近=基本面
     正在恶化。
   - 价格位置安全边际（0-30分）：现价相对52周高低点的位置，结合估值历史
     分位——有分位数据时必须按分位打分，只有静态倍数（没有历史分位）时最高
     不超过20分，因为缺了"相对自己历史贵不贵"这个参照，打不出高确定性的分。
     30分附近=明确低位+估值处于历史低分位；15分附近=位置中性；0分附近=接近
     52周高点或估值明显偏贵。
   - 技术面确认（0-15分）：技术面信号是否支持基本面结论、有没有背离。
     15分=技术面与基本面同向确认；7分附近=信号中性或数据不足；0分=技术面
     跟基本面结论明显背离，应该在理由里明确写出背离在哪。
   - 数据确定性（0-15分）：财务/新闻/技术面这三类数据是不是都拿到了、互相
     是否印证——缺一类就要扣分，不能假装齐全。15分=三类数据齐全且互相
     印证；0分=数据严重缺失，判断基本靠猜。**"没有估值历史分位数据"这一点
     不算在这个维度里**——美股这类结构性缺分位数据的市场，"价格位置"那一项
     已经因为这个原因把上限压到了20/30分，这里不能因为同一个缺口再扣一次，
     不然同一件事被罚了两次分，跨市场比较会失真。
   四项分数必须在"维度打分"里逐项列出来，综合得分原则上等于四项之和；如果
   四项加总跟你的整体判断有出入，允许再做不超过±5分的微调，但必须在"理由"
   里说明为什么调（比如"四项之和72，但新闻里有一条未被上面数据覆盖的重大
   负面消息，下调5分"），不能四项加完之后随意大改，那样拆解打分就失去意义。
   分数要跟结论逻辑自洽（比如给"卖出"却打70分以上是不合理的）。
9. 最后必须附一句："仅供参考，不构成投资建议，请自行判断。"

严格按以下格式输出（不要多余寒暄）：
结论：[买入/卖出/持有/观望]
维度打分：基本面X/40 · 价格位置X/30 · 技术面X/15 · 数据确定性X/15
综合得分：[0-100的整数]
置信度：[高/中/低]
基本面：<一到两句话，增长数字要点出是一次性还是结构性>
技术面：<一句话>
价格位置：<一句话，引用52周高低点具体数字>
理由：<两到三句话，具体点出依据，必须体现价格位置对结论的影响；结论是买入的话必须包含止损参考位>
"""

# 持仓是用户已经持有的仓位，判断的问题不是"要不要买"而是"要不要卖/继续拿"——
# 用同一套四选一结论框架（这里"买入"含义变成"可以加仓"），但额外要求给出
# 具体的卖出触发条件，不能只说"注意风险"这种空话。追加在_JUDGE_SYSTEM后面，
# 不改原来的prompt（screen候选池用的是原版，两者场景不同不能共用一份预期）。
_HOLDING_ADDENDUM = """

补充要求（这是用户已经持有的持仓，不是新的候选）：
10. 你的核心任务是回答"继续持有还是应该卖出"，不是"值不值得新买入"——判断
    基调从"入场时机"切换成"仓位管理"。
11. 结论如果是"持有"或"观望"，必须给出具体的卖出触发条件，二选一或都给：
    - 价格触发：具体点位（比如"若跌破52周低点X"或"若涨到接近52周高点X附近
      可考虑获利了结一部分"）。
    - 信号触发：具体的基本面或技术面变化（比如"若下一季度营收增速跌破X%"
      或"若MA5下穿MA20出现死叉"）。
    不能只写"继续观察""注意风险"这类没有具体触发条件的空话。
12. 结论是"卖出"时，理由要说清楚是基本面恶化、还是纯粹价格位置过高落袋为安，
    两者性质不同，不能混着说。
"""

# 多空辩论——参考开源项目TradingAgents（TauricResearch）的架构思路：单次AI
# 判断容易带单边偏向，让两个立场相反的AI各自基于同一份真实数据把最有力的
# 论据摆出来，再让裁判权衡谁更站得住脚，比直接一次性下结论更扎实。只用在
# 持仓判断(judge_stock_with_debate)，不用在候选池初筛(judge_stock)——候选池
# 一天要判断几十支，3倍AI调用成本/耗时在这个量级上不划算；持仓通常只有
# 几支，多花的成本对判断质量提升是值得的，这里的场景比初筛更值得较真。
_BULL_SYSTEM = """你是一位专业的多头研究员，正在为"这支股票该不该继续持有/加仓"这个
辩论准备论据。只能用下面给出的真实数据（财务/技术面/价格位置/新闻），不能编造
任何数字或消息，但你的立场是多头——从这些真实数据里，尽你所能挖掘出对多头最
有利的解读角度，把最强的论据摆出来。不用面面俱到，挑最有说服力的2-3点讲透，
每一点都要引用具体数字。最后一句话总结你的核心论点。"""

_BEAR_SYSTEM = """你是一位专业的空头研究员，正在为"这支股票该不该减仓/卖出"这个
辩论准备论据。只能用下面给出的真实数据（财务/技术面/价格位置/新闻），不能编造
任何数字或消息，但你的立场是空头——从这些真实数据里，尽你所能挖掘出对空头最
有利的解读角度，把最强的论据摆出来。不用面面俱到，挑最有说服力的2-3点讲透，
每一点都要引用具体数字。最后一句话总结你的核心论点。"""

_DEBATE_JUDGE_ADDENDUM = """

补充要求（这次你会看到两份针对同一支股票的对立论证，不是直接看原始数据）：
13. 你会拿到"多头论证"和"空头论证"两段文字，都是基于同一份真实数据但立场
    相反写的。你的任务是权衡哪一边的论据更站得住脚，不能各打五十大板、和稀泥，
    必须明确表态更认同哪一边、具体是哪一条论据说服了你。
14. 被你否决的那一方如果有合理的点（比如空头指出的风险确实存在，只是你认为
    不足以推翻买入逻辑），要在理由里明确提一句承认，不能假装对方论证不存在。
"""


def _build_judge_user_content(symbol: str, market: str, name: str, financial_summary: str,
                               technical_summary: str, news_summary: str, position_summary: str,
                               valuation_summary: str = "") -> str:
    return (
        f"股票：{name}（{symbol}，{market}股）\n\n"
        f"财务摘要：\n{financial_summary or '（暂无财务数据）'}\n\n"
        f"估值：{valuation_summary or '（暂无估值数据）'}\n\n"
        f"技术面信号：{technical_summary or '（数据不足）'}\n\n"
        f"价格位置（52周区间）：{position_summary or '（数据不足）'}\n\n"
        f"近期新闻：\n{news_summary or '（暂无相关新闻）'}"
    )


def judge_stock_with_debate(symbol: str, market: str, name: str, financial_summary: str,
                             technical_summary: str, news_summary: str, position_summary: str = "",
                             valuation_summary: str = "", holding: bool = True) -> dict:
    """带多空辩论的判断——只给持仓用（见上面的成本考量注释）。bull/bear两个
    论证并发生成（互不依赖，同时发也不影响独立性——两边都只能看到原始数据，
    看不到对方的论证，这才是真正各自独立的论据，不是一个抄另一个）。"""
    data_context = _build_judge_user_content(symbol, market, name, financial_summary, technical_summary, news_summary, position_summary, valuation_summary)

    def _call_stance(client, model, system_prompt):
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": data_context}],
            max_tokens=4000,  # 论据比最终判断的六段结构化输出短得多，但同样的空内容坑保底调高一点
            temperature=0.5,  # 辩论双方要有观点区分度，比最终判断的0.3稍高
            stream=False,
        )
        text = resp.choices[0].message.content or ""
        if not text.strip():
            raise RuntimeError(f"AI返回空内容（finish_reason={resp.choices[0].finish_reason}）")
        return text

    def _fetch_stance(spec):
        system_prompt, client, model, is_primary = spec
        # 空头这边配置了智谱但调用失败（没配key/网络问题/超时）时，回落到
        # 千问——保证辩论功能本身不会因为一家供应商的问题整个报废，退化成
        # "两边都用千问"而不是直接跳过这支股票的判断。is_primary标记这一条
        # 本来就是主线供应商，失败了没有更下游的兜底可回落，直接抛出。
        if client is None:
            if is_primary:
                raise RuntimeError("主线供应商未配置")
            return _call_stance(_client(), _MODEL, system_prompt)
        try:
            return _call_stance(client, model, system_prompt)
        except Exception:
            if is_primary:
                raise
            return _call_stance(_client(), _MODEL, system_prompt)

    # 多头：阿里千问；空头：智谱——两家真正独立的供应商各自只看原始数据
    # 单独出论证，互相看不到对方，不是同一个模型左右手互搏。
    stance_results = _run_concurrent_with_deadline(
        [(_BULL_SYSTEM, _client(), _MODEL, True), (_BEAR_SYSTEM, _zhipu_client(), _ZHIPU_MODEL, False)],
        _fetch_stance, timeout=60, max_workers=2,
    )
    if 0 not in stance_results or 1 not in stance_results:
        raise RuntimeError("多空论证生成失败（可能是网络/AI调用超时），跳过这次辩论判断。")
    bull_text, bear_text = stance_results[0], stance_results[1]

    final_user_content = f"{data_context}\n\n多头论证：\n{bull_text}\n\n空头论证：\n{bear_text}"
    system_prompt = _JUDGE_SYSTEM + (_HOLDING_ADDENDUM if holding else "") + _DEBATE_JUDGE_ADDENDUM

    # 裁判用_client()/_MODEL(千问)——2026-08-26曾经短暂换成DeepSeek第三方
    # （理由见上面模块级注释），但团队决定两个项目都彻底不用DeepSeek，改回来。
    # 裁判跟多头选手同一供应商这个理论上的偏袒问题目前没有更好的解法（智谱
    # 已经是空头那边独立供应商了，裁判用智谱只是把偏袒方向倒过来，不是真的
    # 消除），暂时接受这个局限。
    resp = _client().chat.completions.create(
        model=_MODEL,
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": final_user_content}],
        max_tokens=8000,
        temperature=0.3,
        stream=False,
    )
    text = resp.choices[0].message.content or ""
    if not text.strip():
        raise RuntimeError(f"AI返回空内容（finish_reason={resp.choices[0].finish_reason}）")
    action = _extract_action(text)
    return {
        "action": action, "score": _extract_score(text), "fundamental_verdict": text,
        "bull_argument": bull_text, "bear_argument": bear_text,
    }


def judge_stock(symbol: str, market: str, name: str, financial_summary: str,
                 technical_summary: str, news_summary: str, position_summary: str = "",
                 valuation_summary: str = "", holding: bool = False) -> dict:
    user_content = _build_judge_user_content(symbol, market, name, financial_summary, technical_summary, news_summary, position_summary, valuation_summary)
    system_prompt = _JUDGE_SYSTEM + _HOLDING_ADDENDUM if holding else _JUDGE_SYSTEM
    resp = _client().chat.completions.create(
        model=_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        # DeepSeek隐藏的reasoning_content跟正式回答共用同一个max_tokens预算，
        # 这个项目反复踩过的老坑（README"AI分析概率性返回空内容"）。这个判断
        # 要求综合基本面+成长性+负债+估值+技术面五项给结论，思考链容易变长，
        # 实测4000时仍有41%概率返回空内容，调到8000。
        max_tokens=8000,
        temperature=0.3,
        stream=False,
    )
    text = resp.choices[0].message.content or ""
    if not text.strip():
        # 空内容不当成"观望"记下去——那是"没判断出来"，跟AI主动判断"没有把握
        # 所以观望"是两回事，前者写进advice表会污染统计还会让人误以为是真判断。
        # 交给调用方(_judge_one)的except分支处理为失败，跳过这条不记录。
        raise RuntimeError(f"AI返回空内容（finish_reason={resp.choices[0].finish_reason}）")
    action = _extract_action(text)
    return {"action": action, "score": _extract_score(text), "fundamental_verdict": text}


def _financial_summary_text(symbol: str, market: str) -> str:
    try:
        df = ds.get_financial_abstract(symbol, market)
    except Exception:
        return ""
    if df is None or df.empty:
        return ""
    return df.head(20).to_string(index=False)


def _valuation_text(symbol: str, market: str) -> str:
    """估值倍数——原来的判断链条完全没有这块：_JUDGE_SYSTEM第一条明明要求
    "估值是否处于合理区间"，但AI手上其实没有这个数字（候选筛选阶段Futu已经
    拉过PE_TTM，但只用来筛候选池，筛完就扔，没往后传给判断这一步），等于
    要求AI回答一个它压根没被喂数据的问题——2026-08-25排查排行榜可信度时
    发现的真实数据洞。

    A股/港股用百度股市通的近三年历史分位（ds.get_valuation_percentile），
    不是只给一个孤立的当前倍数，而是回答"相对自己历史贵不贵"。美股同源接口
    实测挂了（见该函数docstring），改用Futu快照里的静态PE(TTM)/PB兜底，
    没有历史分位就如实说明，不编一个假分位数糊弄。
    """
    if market in ("A", "HK"):
        try:
            data = ds.get_valuation_percentile(symbol, market)
        except Exception:
            data = {}
        if not data:
            return ""
        parts = []
        thin_sample = False
        for label, unit in (("pe_ttm", "PE(TTM)"), ("pb", "PB")):
            d = data.get(label)
            if not d:
                continue
            years = d["years"]
            # 新股/次新股（比如2026-08-26真实撞到的思格新能，上市才4个月，
            # 132个交易日）years算出来是0.5这类小数，原来".0f"格式化会把
            # 0.5四舍五入成"0"（Python银行家舍入），拼出"处于近0年历史
            # 分位"这种自相矛盾的文本——分位数字本身也是从不到半年的样本里
            # 算出来的，参考价值天然有限，不能让AI拿着当成跟"近3年"分位
            # 同等confident的证据来用。年数少于1年时换算成月份表述，并显式
            # 标注样本短，交给下面的_JUDGE_SYSTEM规则提醒AI据此降低置信度。
            if years < 1.0:
                thin_sample = True
                span_text = f"近{round(years * 12)}个月（样本较短，仅上市{round(years*12)}个月左右）"
            else:
                span_text = f"近{years:.1f}年"
            parts.append(f"{unit} {d['current']:.1f}倍，处于{span_text}历史分位约{d['percentile']:.0f}%")
        if not parts:
            return ""
        note = "分位越接近100%表示相对自己历史估值区间越贵，越接近0%越便宜"
        if thin_sample:
            note += "；样本时间较短的分位数据波动性大、参考力度弱于长期分位，判断时要相应降低这条证据的权重，不能当成跟长期分位同等确定的依据"
        return "，".join(parts) + f"（{note}）。"

    # 美股：百度估值接口实测失效（AAPL/BILI等任意代码都404级失败），改用
    # Futu快照的静态PE/PB，跟_price_position_text的美股分支同一个数据源，
    # 但这里单独开一个连接（跟该函数docstring里"每次调用独立开关"同一个
    # 原则，不跨线程共享连接对象）。
    code = f"{market}.{symbol}"
    result = _futu_call_with_timeout(lambda ctx: ctx.get_market_snapshot([code]), timeout=20)
    if result is None:
        return ""
    ret, data = result
    if ret != ft.RET_OK or data is None or data.empty:
        return ""
    row = data.iloc[0]
    pe = row.get("pe_ttm_ratio")
    pb = row.get("pb_ratio")
    if not pe and not pb:
        return ""
    parts = []
    if pe:
        parts.append(f"PE(TTM) {pe:.1f}倍")
    if pb:
        parts.append(f"PB {pb:.1f}倍")
    return "，".join(parts) + "（无历史分位数据，百度估值接口对美股暂不可用，只有当前静态倍数，缺少相对自身历史贵贱的参照，判断时要如实体现这个局限）。"


def _technical_summary_text(symbol: str, market: str) -> str:
    try:
        end = ds.cn_now().strftime("%Y%m%d")
        start = (ds.cn_now() - timedelta(days=90)).strftime("%Y%m%d")
        hist = ds.get_stock_history(symbol, start, end, market=market)
        if hist is None or hist.empty:
            return ""
        return charts.compute_technical_signal(hist)
    except Exception:
        return ""


# 做空/监管/诉讼类关键词——命中就单独标记出来，防止这类真正的黑天鹅信息
# 被"只展示最近5条"的排序悄悄挤出去（比如最新5条都是股价异动播报，但3天前
# 有一条监管立案调查被排到第6条，原来的逻辑会直接漏掉）。
_NEGATIVE_NEWS_KEYWORDS = ["做空", "调查", "诉讼", "立案", "处罚", "退市", "违规", "造假", "问询函", "评级下调", "遭减持", "被减持"]


def _news_summary_text(symbol: str, market: str, name: str) -> str:
    """近期新闻——原来直接调ds.get_stock_news(name)，那个函数的数据源
    (get_market_news)本质是财新的大盘宏观资讯（美联储利率决议/中东局势
    这类），不是个股新闻，靠公司名做子串匹配命中率极低，2026-08-25实测
    对"哔哩哔哩"这种真实候选直接返回空——不是那天没有相关新闻，是这个数据
    源结构上就不含个股新闻，之前每次判断的"近期新闻"这个维度基本等于没有
    真正生效过。

    改用app.py _fetch_news_items已经验证过的同一套优先级链路（A股官方
    公告 > Futu资讯搜索，真按关键词匹配、三个市场通吃 > 财新兜底），不是
    另起炉灶接一个新数据源——这条链路已经在公开详情页跑了很久，可信。
    """
    df = None
    if market == "A" and symbol:
        try:
            df = ds.get_stock_notices(symbol)
        except Exception:
            df = None
    if df is None or df.empty:
        try:
            df = ds.get_futu_news(name or symbol, max_count=8)
        except Exception:
            df = None
    if df is None or df.empty:
        try:
            df = ds.get_stock_news(name or symbol, limit=8)
        except Exception:
            df = None
    if df is None or df.empty or "新闻标题" not in df.columns:
        return ""

    titles = df["新闻标题"].head(8).tolist()
    lines = [f"- {t}" for t in titles[:5]]
    flagged = [t for t in titles if any(k in t for k in _NEGATIVE_NEWS_KEYWORDS)]
    if flagged:
        lines.append("注意：命中做空/监管/诉讼类关键词的标题（不论是否在上面最近5条里都要看到）：" + "；".join(flagged))
    else:
        lines.append("（已排查做空/监管/诉讼类关键词，未在近期标题中发现）")
    return "\n".join(lines)


def _price_position_text(symbol: str, market: str) -> str:
    """现价在52周区间里处于什么位置——这是原来这套判断完全缺失的一环：
    基本面数据只回答"公司好不好"，不回答"现在这个价格有没有把好消息/坏消息
    过度定价"。用户举的例子很典型：英伟达基本面很强但如果已经涨了很多，
    追高不一定赚钱；Beyond Meat基本面很弱但跌得足够多也可能有超跌反弹机会。
    这个函数把"离52周高点回撤多少、比52周低点涨了多少倍"这两个客观数字算
    出来，明确喂给AI，逼它把"价格位置"当成独立于基本面之外的第三个判断维度，
    不能光看财报数字好看就无脑给买入。

    每次调用单独开一个短连接（通过_futu_call_with_timeout，带超时保护，见
    该函数docstring里2026-08-21的真实故障记录）——跟_futu_screen_pool的
    连接是分开的，_judge_one并发调用时各自独立开关，不跨线程共享连接对象。
    """
    # A股不走Futu——账号没有A股行情权限（跟_a_share_candidate_pool同一个
    # 根因，2026-08-25实测确认），get_market_snapshot对A股代码必然返回
    # 失败，之前这里静默return ""，导致A股候选的"价格位置"这一整个判断
    # 维度长期系统性缺失：抽查当天真实judge结果发现，A股观望占比明显
    # 高于港美股（97% vs 60%左右），逐条看理由文本，AI反复提到"缺失52周
    # 高低点数据，无法判断价格位置"——不是AI瞎判，是这个数据洞客观上
    # 让它没法给出比"观望"更有把握的结论。改用AkShare一年日线历史本地
    # 算最高/最低，不依赖Futu权限。
    if market == "A":
        try:
            end = ds.cn_now().strftime("%Y-%m-%d")
            start = (ds.cn_now() - timedelta(days=365)).strftime("%Y-%m-%d")
            hist = ds.get_stock_history(symbol, start, end, market="A")
        except Exception:
            hist = None
        if hist is None or hist.empty or "最高" not in hist.columns or "最低" not in hist.columns:
            return ""
        hi = float(hist["最高"].max())
        lo = float(hist["最低"].min())
        cur = float(hist.sort_values(hist.columns[0]).iloc[-1]["收盘"]) if "收盘" in hist.columns else None
        if not cur or not hi or not lo or hi <= 0 or lo <= 0:
            return ""
        from_high_pct = (cur - hi) / hi * 100
        from_low_x = cur / lo
        percentile = (cur - lo) / (hi - lo) * 100 if hi > lo else 50
        return (
            f"现价{cur:.2f}，52周最高{hi:.2f}（较高点{from_high_pct:+.1f}%），"
            f"52周最低{lo:.2f}（是低点的{from_low_x:.1f}倍），"
            f"当前处于52周区间约{percentile:.0f}%分位（越接近100%越接近高点）。"
        )

    # 港股/美股：Futu有正常行情权限，走原路径。
    # get_market_snapshot要求带交易所前缀的代码(SH.600000/SZ.000858)，
    # 不接受项目里统一用的market_code"A"——按代码开头判断沪/深，
    # 跟data_sources.py _sina_symbol同一套"6/9开头沪市，其它深市"规则。
    code_prefix = market
    code = f"{code_prefix}.{symbol}"
    result = _futu_call_with_timeout(lambda ctx: ctx.get_market_snapshot([code]), timeout=20)
    if result is None:
        return ""
    ret, data = result
    if ret != ft.RET_OK or data is None or data.empty:
        return ""
    row = data.iloc[0]
    cur = row.get("last_price")
    hi = row.get("highest52weeks_price")
    lo = row.get("lowest52weeks_price")
    if not cur or not hi or not lo or hi <= 0 or lo <= 0:
        return ""
    from_high_pct = (cur - hi) / hi * 100
    from_low_x = cur / lo
    percentile = (cur - lo) / (hi - lo) * 100 if hi > lo else 50
    return (
        f"现价{cur:.2f}，52周最高{hi:.2f}（较高点{from_high_pct:+.1f}%），"
        f"52周最低{lo:.2f}（是低点的{from_low_x:.1f}倍），"
        f"当前处于52周区间约{percentile:.0f}%分位（越接近100%越接近高点）。"
    )


def _judge_one(item: dict, source: str) -> dict | None:
    holding = source == "position"
    symbol, market, name = item["symbol"], item.get("market", "US"), item.get("name", "")
    try:
        price = ds.get_stock_realtime(symbol, market).get("最新价")
    except Exception:
        price = None
    fin = _financial_summary_text(symbol, market)
    valuation = _valuation_text(symbol, market)
    tech = _technical_summary_text(symbol, market)
    news = _news_summary_text(symbol, market, name)
    position = _price_position_text(symbol, market)
    try:
        # 持仓判断走多空辩论版本（更扎实但3倍AI调用），候选池初筛(source=
        # "screen")继续用单次判断——几十支候选一天判断一遍，辩论版本的
        # 调用量级在那个场景下不划算，见judge_stock_with_debate上面的注释。
        if holding:
            verdict = judge_stock_with_debate(symbol, market, name, fin, tech, news, position, valuation, holding=True)
        else:
            verdict = judge_stock(symbol, market, name, fin, tech, news, position, valuation, holding=False)
    except Exception as e:
        return {"symbol": symbol, "market": market, "name": name, "error": str(e)}
    return {
        "symbol": symbol, "market": market, "name": name, "price": price,
        "action": verdict["action"], "score": verdict.get("score"),
        "fundamental_verdict": verdict["fundamental_verdict"],
        "technical_signal": tech, "source": source,
    }


def _backfill_due_advice() -> int:
    # position/screen沿用7天窗口（默认min_age_days），watchlist每天都出新
    # 判断，单独用更短的窗口——不能合并成一次不带source过滤的查询，那样watchlist
    # 也会被7天窗口卡住，第二天该有的"次日核对"就出不来了（见
    # get_due_for_advice_review的source参数注释）。
    #
    # 2026-08-30修复：watchlist原来用min_age_days=1（整24小时），但cron每天
    # 固定同一时刻（17:30）启动，watchlist判断是main()里排最后、要等
    # backfill+持仓判断+组合分析都跑完才开始写，比当天cron启动时刻晚
    # 10-20分钟——这意味着"距上次cron启动正好24小时"这个cutoff永远比
    # 昨天watchlist的落库时间早那10-20分钟，"created_at <= cutoff"这个条件
    # 结构性地永远为假，不是偶尔差一点，是每天都会被卡住，实测8-28这批
    # 49条在8-29的真实cutoff下命中数是0。改成0.9天（21.6小时），留出
    # 足够吸收这个固定偏移量的余量。position/screen不用管，它们的7天窗口
    # 本来就有充裕余量。
    due = (
        tracker.get_due_for_advice_review(_EMAIL, limit=200, source="position")
        + tracker.get_due_for_advice_review(_EMAIL, limit=200, source="screen")
        + tracker.get_due_for_advice_review(
            _EMAIL, min_age_days=_WATCHLIST_REVIEW_MIN_AGE_DAYS,
            limit=sum(_WATCHLIST_TARGET_SIZE.values()) * 2, source="watchlist",
        )
    )
    if not due:
        return 0

    def _fetch_price(row):
        try:
            return ds.get_stock_realtime(row["symbol"], row.get("market", "US")).get("最新价")
        except Exception:
            return None

    results = _run_concurrent_with_deadline(due, _fetch_price, timeout=60, max_workers=8)
    n = 0
    for i, row in enumerate(due):
        price = results.get(i)
        # 2026-08-30修复：回填价格等于入场价，大概率是市场当天没开盘
        # （周末/节假日回填到的还是上一个交易日收盘价，跟入场价撞了同一个
        # 数），不是真的"次日零涨跌"——按这个项目一贯"取不到就留NULL不
        # 硬凑"的原则，这种情况不记review_price，留着下次（下一个真实
        # 交易日）再试，不能当0%收益记进回测统计（8-28那批周六回填出的
        # 31条review_price=price_at_advice就是这么污染的，把回测胜率
        # 直接拉到12%~26%，正常该在50%上下）。
        if price and price != row.get("price_at_advice"):
            tracker.record_advice_review(row["id"], price)
            n += 1
    return n


def advise_positions() -> list[dict]:
    """持仓判断——跟screen候选不同，这是"要不要卖"的仓位管理判断
    (holding=True，见judge_stock的_HOLDING_ADDENDUM)。结果只落库给网站
    持仓页面用，不进每日微信简报正文——用户明确说过不用在微信里报持仓
    这块，微信简报的定位是"发现新机会"，持仓走网站页面自己看。
    """
    items = tracker.get_positions(_EMAIL)
    if not items:
        return []
    results = _run_concurrent_with_deadline(
        items, lambda it: _judge_one(it, "position"), timeout=250, max_workers=4
    )
    return [results[i] for i in sorted(results) if results[i] and "error" not in results[i]]


_HOLDING_ACTIONS = ("继续持有", "加仓", "减仓", "定投", "止盈", "割肉")

_PORTFOLIO_SYSTEM = """你是一位理性、保守的投研助理，正在给一位个人投资者做组合层面的整体
体检——不是评价某一支股票，是评价"这些持仓摆在一起是否健康"，这是单支
判断给不出的信息（单支判断只知道"这支该不该买"，不知道"买多了会不会
太集中"）。

这份体检要同时覆盖两个角度，缺一不可：
- 组合配置角度：集中度、市场敞口是否失衡。
- 个股跟踪角度：每支持仓最近的价格位置（52周区间）、技术面异动、
  近期新闻，结合当前点位判断这支具体该怎么处理——不能只谈配置比例、
  不看每支个股当下的真实状态，那样等于没跟踪。

要求：
1. 只能用给定的真实数字和新闻做判断（集中度、市场敞口、每支的价格位置/
   技术面/新闻、单支AI判断），不能编造任何数字或消息，也不能重复一遍
   已经给你的数字，要在数字基础上给出解读。不分析币种敞口——市场和币种
   在这个组合里是一一对应的（HK仓位=港币、US仓位=美元），市场敞口已经
   说明了这层风险，重复分析币种没有额外信息量。
2. 集中度：如果最大单一持仓或前三大合计占比明显过高（比如单支超过30%、
   前三合计超过60%这个量级，具体门槛你自己判断但要讲清楚为什么觉得高/
   不高），要明确指出这是风险，不能因为"这几支基本面都不错"就忽略集中度
   本身的风险——基本面好和分散度不够是两个独立的问题。
3. 逐支必须从"继续持有/加仓/减仓/定投/止盈/割肉"这六个动作里选一个
   （止盈=浮盈了结获利；割肉=浮亏认赔离场；减仓≠割肉，减仓是仍然看好
   只是仓位太重；定投=分批建仓/摊薄成本，不是一次性加仓）。**不能只给
   百分比或方向，必须换算成具体的股数和金额**——每支持仓下面给了"每变动
   1个百分点仓位≈多少股/多少钱"这个换算参考，直接拿这个数字乘以你判断
   的变动百分点数，算出具体股数和金额写进建议里（比如"减仓10个百分点，
   约卖出35股，约¥1.6万"），不能只说"减仓10个百分点"就不管了。结合这支
   的价格位置（离52周高低点多远）、技术面信号、最近新闻里有没有值得
   注意的异动，说明这个动作是价格位置驱动、新闻/基本面驱动、还是纯粹
   仓位过重驱动——这三种性质不同，不能混着说。没有相关新闻或数据不足的
   持仓要如实说"暂无重要异动"，不能硬编一个理由。每一条动作建议后面
   必须紧跟支持这个建议的具体理由，不能只给动作不给理由。
4. 除了逐支的动作建议外，还要给出组合层面的操作触发条件，每条都要写成
   "如果/当XXX，应该XXX"的形式，引用真实数字（仓位占比、价格点位），
   不能是"注意风险""保持关注"这类空话。如果用户设定了资金上限，所有
   "加仓"建议必须在剩余额度内，不能假装用户有无限资金；如果已经超出
   上限或接近上限，要明确提醒，加仓建议要让位给减仓/止盈建议。用户没
   设上限时不能替他假设一个数字，只按仓位占比给建议。
5. 如果某支持仓单独判断（会在下面给你）已经是"卖出"，但这份组合分析
   发现它占比很低不影响大局，或者占比很高值得优先处理，要明确点出这个
   落差——这是组合层面才能提供的信息，单支判断本身看不到。
6. 如果给了"近期AI候选"数据（每日全市场初筛+判断出来的买入/持有推荐，
   不是专门为这个组合挑的），看一下里面有没有能改善这个组合健康度的
   标的（比如组合缺A股敞口、候选里刚好有A股买入推荐；组合过度集中在
   科技股、候选里有其它行业的买入推荐）——如果有，具体提1-2支，给一个
   参考仓位（比如"总资产的5%"，如果设了资金上限就换算成具体金额），
   说明为什么这支能改善组合而不是单纯"它被判断为买入"（候选池的买入
   判断是单支层面的，跟"适不适合放进这个组合"是两个问题）。没有合适的
   就如实说"近期候选里没有明显能改善组合结构的标的"，不能为了凑内容
   硬推荐一支。
7. 用**加粗**标出你认为整段分析里最重要、用户最应该马上看到的结论性
   判断（比如"集中度过高必须优先处理"这类），不要把无关紧要的内容也
   加粗，加粗是用来突出重点的，滥用就失去意义了。
8. 最后附加一个"交易信号"结构化区块——这是给用户对照着去券商手动下单用
   的，格式必须严格遵守（程序要解析这部分，格式错了解析不出来）：每支
   持仓（不管动作是什么，包括"继续持有"）单独一行，用竖线分隔六个字段，
   不要加多余的空格、不要用中文顿号代替竖线：
   名称|代码|市场代码(A/HK/US)|动作(买入/卖出/不动)|股数(纯数字，不动填0)|金额(折人民币，纯数字，不动填0)
   "加仓/定投"对应动作填"买入"，"减仓/止盈/割肉"对应动作填"卖出"，
   "继续持有"对应动作填"不动"——这是给程序解析用的简化三分类，跟"逐支
   跟踪"里六选一的细分类是两个不同粒度，不矛盾。股数用逐支跟踪里同样
   算出来的数字，不要重新编一个不一致的数字。
9. 最后必须附一句："仅供参考，不构成投资建议，请自行判断。"

严格按以下格式输出（不要多余寒暄）：
总体评估：<两三句话，这个组合现在处于什么状态，健康还是有明显问题>
集中度风险：<引用真实的占比数字，明确指出是否过于集中>
市场敞口：<引用真实的敞口数字，指出有没有明显失衡>
逐支跟踪：<对每一支持仓单独一行，格式"标的名（代码）：【动作】具体股数
+金额——理由"，动作必须是继续持有/加仓/减仓/定投/止盈/割肉之一>
新增配置建议：<结合近期AI候选，1-2支能改善组合结构的标的+参考仓位+理由，
没有合适的就如实说没有>
操作建议：<组合层面的触发条件，每条一行，必须可执行>
交易信号：<按要求8的格式，每支一行，不要任何多余文字或表头>
"""

_SIGNAL_ACTIONS = ("买入", "卖出", "不动")


def _parse_trade_signals(text: str) -> list[dict]:
    """从AI输出的"交易信号"区块里解析出结构化的买卖信号——这是"先只调研+
    搭好架子，不接真实下单"这个决定的核心产物：把现在这种一段话式的建议
    升级成可以直接照着操作的结构化数据（标的/方向/股数/金额）。这个函数
    本身只做文本解析，不调用任何富途交易接口，格式不对的行跳过，不强行
    凑数据，宁可信号少也不能编。

    2026-09-01更新：这里解析出的signals现在多了一个可选去处——调用方
    advise_portfolio如果检测到用户打开了"AI模拟交易"开关，会把这批信号
    转手给sim_trader.execute_simulated_trades去富途SIMULATE模拟盘下单。
    那是真实下单（只是模拟盘、不涉及真金白银），跟这里"纯解析不下单"
    不矛盾——下单逻辑独立在sim_trader.py里，不在这个函数里。
    """
    # 只在"交易信号"这个表头本身上切一刀，不能对结果再切第二刀——之前是
    # 连续调用两次.split()，第二次是想兼容半角冒号表头，但两次split都是
    # 对同一段文字生效：如果表头本身用的是全角冒号（预期情况），第一次
    # split已经切出了信号正文，第二次split会在这段正文里找任意一个半角
    # 冒号再切一刀——如果某支标的的名称里恰好带半角冒号，正文前面所有
    # 已经解析对的信号行会被这一刀切掉，是真实的丢数据bug，不是可以接受
    # 的边界情况。改成用正则只在表头处切一次，不会误伤正文内容。
    m = re.search(r"交易信号[：:]", text)
    if not m:
        return []
    block = text[m.end():]
    signals = []
    for line in block.strip().splitlines():
        line = line.strip().lstrip("-•").strip()
        if not line or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 6:
            continue
        name, symbol, market, action, shares_s, amount_s = parts
        if action not in _SIGNAL_ACTIONS or market not in ("A", "HK", "US"):
            continue
        try:
            shares = float(shares_s)
            amount_cny = float(amount_s)
        except ValueError:
            continue
        signals.append({
            "name": name, "symbol": symbol, "market": market, "action": action,
            "shares": shares, "amount_cny": amount_cny,
        })
    return signals


def advise_portfolio(email: str) -> dict | None:
    """组合层面的整体体检——跟advise_positions（单支"要不要卖"判断）是两个
    不同维度，这个函数看的是"这些持仓摆在一起是否健康"（集中度、币种/市场
    敞口、跟单支判断的衔接），单支判断给不出这类信息。只有真实持仓
    (shares>0)才计入，纯关注(shares=0)不算仓位、不参与集中度计算。

    本地先把真实数字都算好再喂给AI（集中度/HHI、币种敞口、市场敞口、
    浮盈浮亏），不让AI自己编数字，这是这个项目一贯的"技术面信号本地算"
    原则的延伸。价格/汇率任一环节失败就跳过那一支并如实统计，不拿旧数字
    硬凑。持仓不足2支时集中度分析意义不大（1支必然100%），直接跳过不
    生成报告，不做没有信息量的判断。
    """
    positions = [p for p in tracker.get_positions(email) if (p.get("shares") or 0) > 0]
    if len(positions) < 2:
        return None

    rows = []
    skipped = 0
    for p in positions:
        symbol, market = p["symbol"], p.get("market", "A")
        try:
            price = ds.get_stock_realtime(symbol, market).get("最新价")
        except Exception:
            price = None
        if not price:
            skipped += 1
            continue
        shares, cost_total, currency = p["shares"], p.get("cost_total") or 0, p.get("currency", "CNY")
        value_native = shares * price
        value_cny, _note = ds.to_cny(value_native, currency)
        cost_cny, _note2 = ds.to_cny(cost_total, currency)
        if value_cny is None or cost_cny is None:
            skipped += 1
            continue
        rows.append({
            "symbol": symbol, "name": p.get("name", symbol), "market": market, "currency": currency,
            "shares": shares, "price": price, "value_cny": value_cny, "cost_cny": cost_cny,
            "pnl_pct": (value_cny - cost_cny) / cost_cny * 100 if cost_cny else 0,
        })

    if len(rows) < 2:
        return None

    total_value_cny = sum(r["value_cny"] for r in rows)
    for r in rows:
        r["weight_pct"] = r["value_cny"] / total_value_cny * 100 if total_value_cny else 0

    rows.sort(key=lambda r: r["value_cny"], reverse=True)
    hhi = sum((r["weight_pct"] / 100) ** 2 for r in rows)
    top1_pct = rows[0]["weight_pct"]
    top3_pct = sum(r["weight_pct"] for r in rows[:3])

    # 只算市场敞口，不算币种敞口——这个项目里market跟currency是一一对应的
    # (HK=HKD/US=USD/A=CNY)，币种敞口是市场敞口的重复信息，用户明确反馈
    # "AI币种分析不要"，干脆不喂给AI，省得它自己再讲一遍。
    by_market: dict[str, float] = {}
    for r in rows:
        by_market[r["market"]] = by_market.get(r["market"], 0) + r["weight_pct"]

    single_advice = tracker.get_position_advice(email)

    # 用户明确要求组合分析不能只谈配置比例，还要"持续关注这些持仓股的异动
    # 和最新资讯，结合当前点位"——跟judge_stock逐支判断用的是同一套本地
    # 取数函数(价格位置/技术面/新闻)，这里复用而不是重新发明一套，保证
    # 口径一致。逐支并发取，单支慢/失败不拖累其它持仓（跟_judge_one并发
    # 判断候选池同一个模式）。
    def _fetch_holding_context(r):
        return {
            "price_position": _price_position_text(r["symbol"], r["market"]),
            "technical": _technical_summary_text(r["symbol"], r["market"]),
            "news": _news_summary_text(r["symbol"], r["market"], r["name"]),
        }

    ctx_results = _run_concurrent_with_deadline(rows, _fetch_holding_context, timeout=90, max_workers=5)

    holdings_lines = []
    for i, r in enumerate(rows):
        ctx = ctx_results.get(i) or {}
        adv = single_advice.get(r["symbol"])
        adv_action = adv.get("action") if adv else "（尚无单支判断）"
        # 换算参考——本地算好"每变动1个百分点仓位对应多少股/多少钱"喂给AI，
        # 不让AI自己从头算乘除法（用户反馈建议要具体到股数/金额，不能是
        # 空泛的百分比叙述；用户实际持有的股数是真实存在的整数，AI直接口算
        # 容易算错，给它一个现成的换算系数更可靠）。
        per_pct_shares = r["shares"] / r["weight_pct"] if r["weight_pct"] else 0
        per_pct_cny = r["value_cny"] / r["weight_pct"] if r["weight_pct"] else 0
        holdings_lines.append(
            f"- {r['name']}（{r['symbol']}·{r['market']}）：仓位占比{r['weight_pct']:.1f}%，"
            f"当前持有{r['shares']:g}股，现价{r['price']:.2f}，浮动盈亏{r['pnl_pct']:+.1f}%，"
            f"单支AI最近判断：{adv_action}\n"
            f"  每变动1个百分点仓位≈{per_pct_shares:.0f}股（约¥{per_pct_cny:,.0f}）\n"
            f"  价格位置：{ctx.get('price_position') or '（数据不足）'}\n"
            f"  技术面：{ctx.get('technical') or '（数据不足）'}\n"
            f"  近期新闻：{ctx.get('news') or '（暂无相关新闻）'}"
        )

    # 用户明确要求"AI要知道我们总共有多少钱，不能盲目加仓"——只看已投入
    # 金额算不出"还有多少余量"，这个值是用户自己在持仓页面设的资金上限，
    # 不设就是None，不能瞎猜一个数字糊弄AI。
    max_capital = tracker.get_max_capital(email)
    if max_capital:
        remaining = max_capital - total_value_cny
        capital_line = (
            f"用户设定的最大资金投入上限（折人民币）：¥{max_capital:,.0f}，"
            f"当前已投入¥{total_value_cny:,.0f}，剩余可投入额度约¥{remaining:,.0f}"
            + ("（已超出上限，不应再建议加仓，只应考虑减仓）" if remaining < 0 else "")
        )
    else:
        capital_line = "用户没有设定资金上限——给加仓建议时只能按仓位占比说，不能假设有多少余量可用。"

    # 近期AI候选(首页"投研候选"同一份数据，screen_candidates每工作日跑一次)——
    # 用户要求"结合之前推荐的股票适当买进保持持仓动态健康平衡"，只喂
    # 买入/持有这两种正面判断的候选（卖出/观望的候选没有加进组合的理由），
    # 只给名称/代码/市场/动作，不带完整判断原文（那是单支层面的详细理由，
    # 组合分析只需要知道"有没有能考虑的候选"，不需要重新讲一遍）。
    try:
        latest = tracker.get_latest_advice(limit_per_market=5)
        candidate_lines = []
        for market_key in ("US", "HK", "A"):
            for c in latest.get(market_key) or []:
                if c.get("action") in ("买入", "持有"):
                    candidate_lines.append(f"- {c['name']}（{c['symbol']}·{market_key}）：{c['action']}")
        candidates_text = "\n".join(candidate_lines) if candidate_lines else "（暂无买入/持有评级的候选）"
    except Exception:
        candidates_text = "（读取失败）"

    user_content = (
        f"组合总资产（折人民币）：¥{total_value_cny:,.0f}，共{len(rows)}支持仓"
        + (f"（另有{skipped}支因行情/汇率暂时获取不到未计入）" if skipped else "") + "\n"
        f"{capital_line}\n\n"
        f"集中度：最大单一持仓占比{top1_pct:.1f}%，前三大合计占比{top3_pct:.1f}%，"
        f"HHI指数{hhi:.3f}（0-1，越接近1越集中，0.15以下通常认为分散度尚可）\n\n"
        f"市场敞口：{'，'.join(f'{m} {w:.1f}%' for m, w in sorted(by_market.items(), key=lambda x: -x[1]))}\n\n"
        f"各持仓明细：\n" + "\n".join(holdings_lines) + "\n\n"
        f"近期AI候选（全市场量化初筛+AI判断，不是专门为这个组合挑的，仅供你参考是否有能改善组合结构的）：\n"
        + candidates_text
    )

    resp = _client().chat.completions.create(
        model=_MODEL,
        messages=[
            {"role": "system", "content": _PORTFOLIO_SYSTEM},
            {"role": "user", "content": user_content},
        ],
        # 同judge_stock的踩坑记录：DeepSeek隐藏思考链跟正式输出共用预算。
        # 这个prompt要求已经很细（逐支六选一动作+量化换算+资金上限+候选
        # 交叉引用+标红），8000/12000/16000都实测过finish_reason=length
        # 拿到空内容——这个prompt的隐藏思考链比前几版都长，调到24000。
        max_tokens=24000,
        temperature=0.3,
        stream=False,
    )
    text = resp.choices[0].message.content or ""
    if not text.strip():
        raise RuntimeError(f"AI返回空内容（finish_reason={resp.choices[0].finish_reason}）")

    holdings_json = json.dumps(
        [{"symbol": r["symbol"], "name": r["name"], "weight_pct": round(r["weight_pct"], 2),
          "value_cny": round(r["value_cny"], 2), "pnl_pct": round(r["pnl_pct"], 2)} for r in rows],
        ensure_ascii=False,
    )
    signals = _parse_trade_signals(text)
    signals_json = json.dumps(signals, ensure_ascii=False)
    # 展示用的正文不带"交易信号"那段原始竖线分隔文本——那段是给程序解析的，
    # 直接混在叙述性文字里显示很生硬，已经解析成signals结构化数据单独展示，
    # 正文里去掉避免重复。
    sig_idx = text.find("交易信号：")
    if sig_idx == -1:
        sig_idx = text.find("交易信号:")
    analysis_text = text[:sig_idx].rstrip() if sig_idx != -1 else text

    tracker.log_portfolio_advice(email, total_value_cny, holdings_json, analysis_text, signals_json)

    # 2026-09-01用户明确要求"让内置AI模拟买卖"——默认关闭，用户在持仓页自己
    # 打开开关才会走到这里。只对接富途自己的SIMULATE模拟盘（见sim_trader.py
    # 开头的说明），不是真实下单，跟_parse_trade_signals文档字符串里"不接
    # 真实下单"那句话不矛盾：那句话说的是REAL环境，这里从头到尾没出现过
    # TrdEnv.REAL。单条信号失败不影响这次advise_portfolio整体返回，模拟盘
    # 执行失败不该让组合分析这次白跑。
    if tracker.get_ai_sim_trading(email):
        try:
            import sim_trader
            sim_trader.execute_simulated_trades(email, signals)
        except Exception as e:
            # 之前是except Exception: pass——execute_simulated_trades如果在
            # 建连接这步就炸了（比如OpenD没起来），异常在函数内部还没走到
            # 任何tracker.log_simulated_order就直接冒出来，静默吞掉的话
            # simulated_orders表里连一条失败记录都不会有，用户完全看不出
            # 今天这次自动下单其实根本没跑，跟这个项目"失败要如实说明"的
            # 一贯原则矛盾。改成印出来——advisor.py的stdout本来就是cron
            # 那个agent读了去整理微信简报的，至少能在日常巡检里露出来。
            print(f"（AI模拟盘自动下单失败：{e}）")

    return {"email": email, "total_value_cny": total_value_cny, "analysis_text": analysis_text, "signals": signals}


def _fmt_entry(e: dict, rank: int | None = None) -> str:
    price = f"{e['price']:.2f}" if e.get("price") else "—"
    score = e.get("score")
    score_text = f" · 综合得分{score}" if score is not None else ""
    prefix = f"#{rank} " if rank is not None else ""
    return f"{prefix}【{e['action']}】{e['name']}（{e['symbol']}·{e['market']}） 现价{price}{score_text}\n{e['fundamental_verdict']}\n"


def main():
    _load_secrets_into_env()
    _check_qwen_balance()

    backfilled = _backfill_due_advice()
    print(f"（已回填 {backfilled} 条到期的历史建议价格）\n")

    position_results = advise_positions()
    for e in position_results:
        tracker.log_advice(
            _EMAIL, e["symbol"], e.get("price"), e["fundamental_verdict"],
            e["technical_signal"], e["action"], e["market"], e["name"], source="position",
            score=e.get("score"),
        )
    print(f"（持仓判断：{len(position_results)} 只已更新，结果在网站持仓页面查看，不进本条简报正文）\n")

    # 组合分析要覆盖所有注册用户（不是只给_EMAIL这一个固定账号算）——
    # 用户明确要求过，跟上面单支持仓判断/screen候选只服务_EMAIL这个私人
    # 脚本账号是两条不同的规则，不要改混了。每个用户各自一次AI调用，
    # 互相独立，一个失败不影响其他人。完整的组合分析文字依然只在网站看，
    # 但_EMAIL（这个私人脚本自己的账号）的交易信号额外并进微信简报——
    # 用户明确要求过"交易信号同步到Mac"，选的方案是并进微信推送（微信
    # 本身在手机/Mac间自动同步，不用另外搭同步机制）。只给_EMAIL这一个
    # 账号推，不能把别的用户的持仓信号也发进这条私人简报，会泄露别人的
    # 真实持仓数据。
    portfolio_emails = tracker.get_all_position_emails()
    portfolio_done = 0
    my_portfolio_result = None
    for pe in portfolio_emails:
        try:
            result = advise_portfolio(pe)
            if result:
                portfolio_done += 1
                if pe == _EMAIL:
                    my_portfolio_result = result
        except Exception as e:
            print(f"（{pe} 的组合分析失败：{e}）")
    print(f"（组合分析：{portfolio_done}/{len(portfolio_emails)} 个用户已更新，完整分析在网站持仓页面查看）\n")

    if my_portfolio_result and my_portfolio_result.get("signals"):
        action_signals = [s for s in my_portfolio_result["signals"] if s["action"] != "不动"]
        print("==================== 持仓交易信号 ====================")
        if action_signals:
            for s in action_signals:
                print(f"{s['name']}：{s['action']} {s['shares']:g}股 · 约¥{s['amount_cny']:,.0f}")
            print("（仅供参考，需自己去券商手动下单，不会自动执行）\n")
        else:
            print("本次维持不动，没有需要操作的标的。\n")

    # 固定观察池（首页"推荐股排行榜"用）——放在screen_candidates()前面跑，
    # 后者是全市场扫描，耗时最长也最容易因为Futu限流/超时失败，watchlist
    # 判断量小（约50支热门股，每天用热度/涨跌幅榜重新取），先跑完先落库，
    # 不会因为后面screen_candidates出问题而连累首页这块每天都要更新的数据。
    watchlist_judged = judge_watchlist()
    for e in watchlist_judged:
        tracker.log_advice(
            _EMAIL, e["symbol"], e.get("price"), e["fundamental_verdict"],
            e["technical_signal"], e["action"], e["market"], e["name"], source="watchlist",
            score=e.get("score"),
        )
    # market_cap=3——用户反馈过"首页推荐股排行榜怎么全是港股"，见_leaderboard
    # 的market_cap参数说明，这份名单要覆盖多个市场，不能被单一市场包圆。
    watchlist_board = _leaderboard(watchlist_judged, _WATCHLIST_LEADERBOARD_SIZE, market_cap=3)
    print(f"（热门观察池：{len(watchlist_judged)} 支已更新，首页推荐股排行榜取前{len(watchlist_board)}）\n")
    # 用户明确要求这份排行榜同步到微信（OpenClaw读取本脚本stdout转发的那条
    # 简报）——跟下面screen_candidates那份"综合得分排行榜"用同样的章节格式/
    # 同一个_fmt_entry，保持简报里两份榜单的排版一致。
    print(f"==================== 首页推荐股排行榜 Top {len(watchlist_board)}（热门观察池） ====================")
    if watchlist_board:
        for i, e in enumerate(watchlist_board, 1):
            print(_fmt_entry(e, rank=i))
    else:
        print("（本次观察池判断全部失败或都没有有效得分，跳过。）\n")

    data = screen_candidates()
    print(_pool_summary(data["us_pool"], data["us_all_count"], "美股"))
    print(_pool_summary(data["hk_pool"], data["hk_all_count"], "港股"))
    print(_pool_summary(data["a_pool"], data["a_all_count"], "A股"))
    print()

    judged = data["judged"]
    if not judged:
        print("这次没有从候选池里判断出任何有效结果（可能是Futu筛选失败或AI判断全部失败），本次不生成建议。")
        return

    for e in judged:
        tracker.log_advice(
            _EMAIL, e["symbol"], e.get("price"), e["fundamental_verdict"],
            e["technical_signal"], e["action"], e["market"], e["name"], source="screen",
            score=e.get("score"),
        )

    # 三个市场混排的综合得分排行榜，取代原来"每个市场固定Top3"——好的自然
    # 排上去，某个市场这次没有靠谱标的就不会被硬凑数量占榜。
    board = _leaderboard(judged, _LEADERBOARD_SIZE)
    print(f"==================== 综合得分排行榜 Top {len(board)}（三市场混排） ====================")
    for i, e in enumerate(board, 1):
        print(_fmt_entry(e, rank=i))

    # 用户明确要求微信简报"不要都是港股，是港美股综合打分前五名"——之前
    # 让负责转发微信的那个agentTurn cron自己从上面的混排榜单里现场剔除A股、
    # 重新排序取前5，结果它读串了行（把上面"首页推荐股排行榜"那份不同的
    # 榜单内容也混进来了），不可靠。改成这里直接算好、单独打一段专门给
    # 微信简报用的"港美股综合得分前5"，格式跟上面一致，agentTurn那边只要
    # 原样转述这一段就行，不用自己做筛选/排序判断，从源头上排除误判空间。
    hk_us_judged = [e for e in judged if e.get("market") in ("HK", "US")]
    hk_us_board = _leaderboard(hk_us_judged, 5, market_cap=3)
    print(f"==================== 港美股综合得分前 {len(hk_us_board)}（不含A股，微信简报用这份） ====================")
    if hk_us_board:
        for i, e in enumerate(hk_us_board, 1):
            print(_fmt_entry(e, rank=i))
    else:
        print("（本次港股/美股候选都没有产出有效得分，跳过。）\n")

    print(f"（本次共对 {len(judged)} 只候选做了完整判断，全部记录进数据库。以上为数据驱动的参考意见，不构成投资建议，请自行判断。）")


if __name__ == "__main__":
    import sys as _sys
    main()
    # get_stock_realtime_futu 建立的 Futu SDK 连接会开一个非 daemon 线程，main()
    # 跑完所有逻辑之后进程并不会自己退出——实测复现过：日志打印完"共N条判断已
    # 记录"，进程还是挂着一直到外层 timeout 才被杀掉。这里所有该做的事（DB写入/
    # 打印结果）都已经在 main() 里同步完成了，直接强制退出，不等这些残留线程。
    #
    # 踩坑记录：os._exit() 不会走 Python 正常退出流程，不会 flush stdio 缓冲区。
    # stdout 重定向到文件/管道时（这个脚本被 ssh/exec 起来时就是这种情况，不是
    # 交互式终端）默认是全缓冲而不是行缓冲——实测复现过：advice 表里真实写进了
    # 35 条记录（判断和入库逻辑全部跑完了），但输出文件里 _pool_summary 和 Top
    # 榜单那几段 print 内容完全不见踪影，就是缓冲区里的内容被 os._exit 直接
    # 丢弃了。os._exit 之前必须显式 flush，不能只信任"逻辑跑完了输出就一定在"。
    _sys.stdout.flush()
    _sys.stderr.flush()
    import os as _os
    _os._exit(0)
