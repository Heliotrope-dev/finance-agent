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

# 必须在任何可能引入 tqdm/streamlit 的 import 之前设置，减少 exec 工具读输出时
# 的噪音（详见下方 __main__ 里的说明）。
os.environ.setdefault("TQDM_DISABLE", "1")
import logging as _logging
_logging.getLogger("streamlit").setLevel(_logging.ERROR)

import queue as _queue
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as _futures_wait
from datetime import datetime, timedelta

import futu as ft
import toml
from openai import OpenAI

import charts
import data_sources as ds
import tracker

_EMAIL = "a13989358483@gmail.com"  # 私人工具，固定单用户，不做多用户
_SECRETS_PATH = os.path.join(os.path.dirname(__file__), ".streamlit", "secrets.toml")
_MODEL = "deepseek-v4-flash"
_DEEPSEEK_BASE = "https://api.deepseek.com"

# 候选池规模——用户要求"扩大到各500支"，让候选池统计（下面 _pool_summary）
# 真实反映大盘水平，不是拿十几只样本硬充"全市场"。Futu 单次最多返回200条，
# 超过要分页拉（见 _futu_screen_pool）。
_US_POOL_TARGET = 500
_HK_POOL_TARGET = 500
_A_POOL_TARGET = 500  # A股按沪/深两个交易所分别筛，各250凑够500

# 池子里最靠前的这些才交给AI逐一判断——控制AI调用次数和运行时长，不是对
# 全部500只跑判断（500只全跑一遍AI单次要几十分钟，不现实也没必要）。因为
# 池子本身已经按净利润增速降序排，取最前面这批本身就是"增长最快的一批"。
_US_CANDIDATE_CAP = 20
_HK_CANDIDATE_CAP = 20
_A_CANDIDATE_CAP = 20

# 最终重点介绍几支——每个市场挑action优先级最高的几支详细展开理由+数据。
_TOP_PICKS = 3


def _load_secrets_into_env():
    """脚本独立运行（不在 streamlit run 里），data_sources.py 的 st.cache_data
    这类 Streamlit API 在无 runtime 环境下能自动降级工作（用内存缓存兜底），
    但 DeepSeek key 这类配置本来是靠 st.secrets 读的，这里读不到——照抄
    app.py/_math_page.py 已有的"把 secrets.toml 灌进 os.environ"模式，让下面
    _client() 的 os.environ fallback 生效。venv 是 Python 3.10，没有 tomllib
    （3.11+ 才有），用 toml 包（streamlit 自身依赖链里就有，不用额外装）。
    """
    if os.environ.get("DEEPSEEK_API_KEY"):
        return
    try:
        secrets = toml.load(_SECRETS_PATH)
        for k, v in secrets.items():
            os.environ.setdefault(k, str(v))
    except FileNotFoundError:
        pass


def _client() -> OpenAI:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY。")
    # 踩坑记录：没设timeout时，账号余额不足(402)这类快速失败的错误在openai SDK
    # 默认重试逻辑下实测挂了将近10分钟没返回——不是DeepSeek真的在处理，是客户端
    # 卡在某个没有时间上限的等待/重试循环里。显式给60秒超时，快速失败好过整批
    # 候选全部在同一个坑里陪跑到_run_concurrent_with_deadline的400秒外层超时。
    return OpenAI(api_key=key, base_url=_DEEPSEEK_BASE, max_retries=2, timeout=60)


def _run_concurrent_with_deadline(items: list, fn, timeout: float, max_workers: int = 8) -> dict:
    """跟 app.py 里同名函数逻辑一致的小拷贝——那个函数本身不依赖 Streamlit
    state，但定义在 app.py 里，这个脚本不方便 import 整个 app.py（会连带跑
    Streamlit 页面配置代码），抄一份轻量独立版本，语义和踩坑记录见 app.py
    原版的 docstring：统一 deadline（从 submit 那一刻算起，不是逐个
    future.result(timeout=N)），避免总耗时失控。
    """
    results: dict[int, object] = {}
    if not items:
        return results
    ex = ThreadPoolExecutor(max_workers=min(max_workers, len(items)))
    futures = {ex.submit(fn, item): i for i, item in enumerate(items)}
    done, _not_done = _futures_wait(list(futures.keys()), timeout=timeout)
    for fut in done:
        try:
            results[futures[fut]] = fut.result()
        except Exception:
            pass
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
            items.append({
                "symbol": item.stock_code.split(".", 1)[-1],
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
    跟这个项目"技术面信号本地算不靠AI编"的一贯做法一致。"""
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


_ACTION_PRIORITY = {"买入": 0, "持有": 1, "观望": 2, "卖出": 3}


def _top_picks(judged: list[dict], market: str, n: int = _TOP_PICKS) -> list[dict]:
    """每个市场挑action优先级最高的n支重点介绍——judged本身已经按增速降序
    （候选池原始顺序）排列，list.sort是稳定排序，同优先级内保留这个顺序当
    tie-break，不是随机挑的。"""
    pool = [j for j in judged if j["market"] == market]
    pool.sort(key=lambda j: _ACTION_PRIORITY.get(j["action"], 9))
    return pool[:n]


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
    # A股没有统一的Futu市场代码，沪/深分开筛(各250凑够500)再合并成一个池子。
    # 市值门槛100亿人民币，量级上大致对应US的20亿美元/HK的50亿港元门槛
    # （同一个"中大盘"筛选意图，按各市场估值/汇率量级换算，不是精确折算）。
    sh_pool, sh_all = _futu_screen_pool(
        ft.Market.SH, ft.FinancialQuarter.ANNUAL, 10_000_000_000, _A_POOL_TARGET // 2
    )
    sz_pool, sz_all = _futu_screen_pool(
        ft.Market.SZ, ft.FinancialQuarter.ANNUAL, 10_000_000_000, _A_POOL_TARGET // 2
    )
    a_pool, a_all = sh_pool + sz_pool, sh_all + sz_all

    shortlist = us_pool[:_US_CANDIDATE_CAP] + hk_pool[:_HK_CANDIDATE_CAP] + a_pool[:_A_CANDIDATE_CAP]
    judged: list[dict] = []
    if shortlist:
        results = _run_concurrent_with_deadline(
            shortlist, lambda it: _judge_one(it, "screen"), timeout=400, max_workers=5
        )
        judged = [results[i] for i in sorted(results) if results[i] and "error" not in results[i]]

    return {
        "us_pool": us_pool, "hk_pool": hk_pool, "a_pool": a_pool,
        "us_all_count": us_all, "hk_all_count": hk_all, "a_all_count": a_all,
        "judged": judged,
    }


_JUDGE_SYSTEM = """你是一位理性、保守的投研助理，服务对象是一位会自己做最终决策的
个人投资者——你的任务是基于给定的真实数据给出参考判断，不是替他下单。

要求：
1. 先看基本面（盈利能力、营收/利润增长趋势、负债水平、估值是否处于合理区间），
   这是判断的主要依据。
2. 技术面信号只作为买卖时机的辅助确认，不能单独作为理由。
3. 必须把"价格所处的位置"当成独立于基本面之外的第三个判断维度，不能只看财报
   数字好不好看就下结论——同样是"基本面好"，现价接近52周高点、或者相对52周
   低点已经涨了几倍甚至几十倍，跟现价刚从低位启动，风险回报比完全不是一回事：
   - 现价越接近52周高点、离低点涨幅越大，即使基本面确实改善，也要在理由里
     明确指出"当前位置已经计入较多乐观预期，追高/继续买入的性价比在下降"，
     基本面好不能作为无条件买入的理由——参考现实案例：一家基本面很强的公司
     如果股价已经涨了很多，现在追高不代表还能赚钱。
   - 现价越接近52周低点、离高点跌幅越大，即使基本面偏弱，也要考虑"是否存在
     超跌反弹的可能性"，但必须明确这是"博反弹"性质的判断，跟"基本面驱动的
     买入"要清楚分开说，不能混为一谈——参考现实案例：一家基本面很弱的公司
     如果已经跌了很多，跌深了本身也可能带来技术性反弹机会，但这跟"这家公司
     值得长期持有"是两码事。
   - 价格位置数据是本地算好的真实52周高低点，不是编的，必须引用具体数字
     （比如"现价较52周高点回撤X%"或"是52周低点的X倍"）。
4. 结论必须是以下四选一：买入 / 卖出 / 持有 / 观望。基本面不过关的，即使技术面
   好看也不能给买入；数据不足以支撑判断时选观望，不要勉强给结论。
5. 明确写清楚为什么——依据是财报里的哪一点、技术面的哪个信号、价格位置怎么样，
   不要空泛地说"综合来看""值得关注"这类没有信息量的话。
6. 只能用给定的数据做判断，不能编造任何数字或消息。
7. 最后必须附一句："仅供参考，不构成投资建议，请自行判断。"

严格按以下格式输出（不要多余寒暄）：
结论：[买入/卖出/持有/观望]
置信度：[高/中/低]
基本面：<一到两句话>
技术面：<一句话>
价格位置：<一句话，引用52周高低点具体数字>
理由：<两到三句话，具体点出依据，必须体现价格位置对结论的影响>
"""

# 持仓是用户已经持有的仓位，判断的问题不是"要不要买"而是"要不要卖/继续拿"——
# 用同一套四选一结论框架（这里"买入"含义变成"可以加仓"），但额外要求给出
# 具体的卖出触发条件，不能只说"注意风险"这种空话。追加在_JUDGE_SYSTEM后面，
# 不改原来的prompt（screen候选池用的是原版，两者场景不同不能共用一份预期）。
_HOLDING_ADDENDUM = """

补充要求（这是用户已经持有的持仓，不是新的候选）：
8. 你的核心任务是回答"继续持有还是应该卖出"，不是"值不值得新买入"——判断
   基调从"入场时机"切换成"仓位管理"。
9. 结论如果是"持有"或"观望"，必须给出具体的卖出触发条件，二选一或都给：
   - 价格触发：具体点位（比如"若跌破52周低点X"或"若涨到接近52周高点X附近
     可考虑获利了结一部分"）。
   - 信号触发：具体的基本面或技术面变化（比如"若下一季度营收增速跌破X%"
     或"若MA5下穿MA20出现死叉"）。
   不能只写"继续观察""注意风险"这类没有具体触发条件的空话。
10. 结论是"卖出"时，理由要说清楚是基本面恶化、还是纯粹价格位置过高落袋为安，
    两者性质不同，不能混着说。
"""


def judge_stock(symbol: str, market: str, name: str, financial_summary: str,
                 technical_summary: str, news_summary: str, position_summary: str = "",
                 holding: bool = False) -> dict:
    user_content = (
        f"股票：{name}（{symbol}，{market}股）\n\n"
        f"财务摘要：\n{financial_summary or '（暂无财务数据）'}\n\n"
        f"技术面信号：{technical_summary or '（数据不足）'}\n\n"
        f"价格位置（52周区间）：{position_summary or '（数据不足）'}\n\n"
        f"近期新闻：\n{news_summary or '（暂无相关新闻）'}"
    )
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
    action = "观望"
    for a in ("买入", "卖出", "持有", "观望"):
        if f"结论：{a}" in text or f"结论:{a}" in text:
            action = a
            break
    return {"action": action, "fundamental_verdict": text}


def _financial_summary_text(symbol: str, market: str) -> str:
    try:
        df = ds.get_financial_abstract(symbol, market)
    except Exception:
        return ""
    if df is None or df.empty:
        return ""
    return df.head(20).to_string(index=False)


def _technical_summary_text(symbol: str, market: str) -> str:
    try:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")
        hist = ds.get_stock_history(symbol, start, end, market=market)
        if hist is None or hist.empty:
            return ""
        return charts.compute_technical_signal(hist)
    except Exception:
        return ""


def _news_summary_text(name: str) -> str:
    try:
        df = ds.get_stock_news(name, limit=5)
        if df is None or df.empty:
            return ""
        col = "标题" if "标题" in df.columns else df.columns[0]
        return "\n".join(f"- {t}" for t in df[col].head(5))
    except Exception:
        return ""


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
    # Futu的get_market_snapshot要求带交易所前缀的代码(SH.600000/SZ.000858)，
    # 不接受项目里统一用的market_code"A"——按代码开头判断沪/深，
    # 跟data_sources.py _sina_symbol同一套"6/9开头沪市，其它深市"规则。
    code_prefix = ("SH" if symbol.startswith(("6", "9")) else "SZ") if market == "A" else market
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
    tech = _technical_summary_text(symbol, market)
    news = _news_summary_text(name or symbol)
    position = _price_position_text(symbol, market)
    try:
        verdict = judge_stock(symbol, market, name, fin, tech, news, position, holding=holding)
    except Exception as e:
        return {"symbol": symbol, "market": market, "name": name, "error": str(e)}
    return {
        "symbol": symbol, "market": market, "name": name, "price": price,
        "action": verdict["action"], "fundamental_verdict": verdict["fundamental_verdict"],
        "technical_signal": tech, "source": source,
    }


def _backfill_due_advice() -> int:
    due = tracker.get_due_for_advice_review(_EMAIL)
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
        if price:
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


_PORTFOLIO_SYSTEM = """你是一位理性、保守的投研助理，正在给一位个人投资者做组合层面的整体
体检——不是评价某一支股票，是评价"这些持仓摆在一起是否健康"，这是单支
判断给不出的信息（单支判断只知道"这支该不该买"，不知道"买多了会不会
太集中"）。

这份体检要同时覆盖两个角度，缺一不可：
- 组合配置角度：集中度、币种/市场敞口是否失衡。
- 个股跟踪角度：每支持仓最近的价格位置（52周区间）、技术面异动、
  近期新闻，结合当前点位判断这支具体该补仓/减仓/清仓还是不动——
  不能只谈配置比例、不看每支个股当下的真实状态，那样等于没跟踪。

要求：
1. 只能用给定的真实数字和新闻做判断（集中度、币种/市场敞口、每支的价格
   位置/技术面/新闻、单支AI判断），不能编造任何数字或消息，也不能重复
   一遍已经给你的数字，要在数字基础上给出解读。
2. 集中度：如果最大单一持仓或前三大合计占比明显过高（比如单支超过30%、
   前三合计超过60%这个量级，具体门槛你自己判断但要讲清楚为什么觉得高/
   不高），要明确指出这是风险，不能因为"这几支基本面都不错"就忽略集中度
   本身的风险——基本面好和分散度不够是两个独立的问题。
3. 逐支过一遍：结合这支的价格位置（离52周高低点多远）、技术面信号、
   最近新闻里有没有值得注意的异动，明确给出"补仓/减仓/清仓/不动"里的
   一个倾向，并说明是价格位置驱动、新闻/基本面驱动、还是纯粹仓位过重
   驱动——这三种性质不同，不能混着说。没有相关新闻或数据不足的持仓要
   如实说"暂无重要异动"，不能硬编一个理由。
4. 必须给出具体的操作触发条件，每条都要写成"如果/当XXX，应该XXX"的
   形式，引用真实数字（仓位占比、价格点位），不能是"注意风险""保持
   关注"这类空话。这是这份报告存在的核心价值，不能潦草带过。
5. 如果某支持仓单独判断（会在下面给你）已经是"卖出"，但这份组合分析
   发现它占比很低不影响大局，或者占比很高值得优先处理，要明确点出这个
   落差——这是组合层面才能提供的信息，单支判断本身看不到。
6. 不要求"买入/卖出/持有/观望"四选一的单一结论——这是组合层面的多维度
   建议，不是单支操作指令，允许同时提到多支持仓的不同处理方式。
7. 最后必须附一句："仅供参考，不构成投资建议，请自行判断。"

严格按以下格式输出（不要多余寒暄）：
总体评估：<两三句话，这个组合现在处于什么状态，健康还是有明显问题>
集中度风险：<引用真实的占比数字，明确指出是否过于集中>
币种/市场敞口：<引用真实的敞口数字，指出有没有明显失衡>
逐支跟踪：<对每一支持仓，结合价格位置/技术面/新闻给出补仓/减仓/清仓/
不动的倾向和理由，每支单独一行>
操作建议：<具体的补仓/减仓/清仓/调整仓位触发条件，每条一行，必须可执行>
"""


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

    by_currency: dict[str, float] = {}
    by_market: dict[str, float] = {}
    for r in rows:
        by_currency[r["currency"]] = by_currency.get(r["currency"], 0) + r["weight_pct"]
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
            "news": _news_summary_text(r["name"] or r["symbol"]),
        }

    ctx_results = _run_concurrent_with_deadline(rows, _fetch_holding_context, timeout=90, max_workers=5)

    holdings_lines = []
    for i, r in enumerate(rows):
        ctx = ctx_results.get(i) or {}
        adv = single_advice.get(r["symbol"])
        adv_action = adv.get("action") if adv else "（尚无单支判断）"
        holdings_lines.append(
            f"- {r['name']}（{r['symbol']}·{r['market']}）：仓位占比{r['weight_pct']:.1f}%，"
            f"浮动盈亏{r['pnl_pct']:+.1f}%，单支AI最近判断：{adv_action}\n"
            f"  价格位置：{ctx.get('price_position') or '（数据不足）'}\n"
            f"  技术面：{ctx.get('technical') or '（数据不足）'}\n"
            f"  近期新闻：{ctx.get('news') or '（暂无相关新闻）'}"
        )

    user_content = (
        f"组合总资产（折人民币）：¥{total_value_cny:,.0f}，共{len(rows)}支持仓"
        + (f"（另有{skipped}支因行情/汇率暂时获取不到未计入）" if skipped else "") + "\n\n"
        f"集中度：最大单一持仓占比{top1_pct:.1f}%，前三大合计占比{top3_pct:.1f}%，"
        f"HHI指数{hhi:.3f}（0-1，越接近1越集中，0.15以下通常认为分散度尚可）\n\n"
        f"币种敞口：{'，'.join(f'{c} {w:.1f}%' for c, w in sorted(by_currency.items(), key=lambda x: -x[1]))}\n"
        f"市场敞口：{'，'.join(f'{m} {w:.1f}%' for m, w in sorted(by_market.items(), key=lambda x: -x[1]))}\n\n"
        f"各持仓明细：\n" + "\n".join(holdings_lines)
    )

    resp = _client().chat.completions.create(
        model=_MODEL,
        messages=[
            {"role": "system", "content": _PORTFOLIO_SYSTEM},
            {"role": "user", "content": user_content},
        ],
        # 同judge_stock的踩坑记录：DeepSeek隐藏思考链跟正式输出共用预算。
        # 这个prompt比judge_stock更复杂（5段结构化输出+要求可执行的触发
        # 条件），实测8000时finish_reason=length、空内容，调到12000验证通过。
        max_tokens=12000,
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
    tracker.log_portfolio_advice(email, total_value_cny, holdings_json, text)
    return {"email": email, "total_value_cny": total_value_cny, "analysis_text": text}


def _fmt_entry(e: dict) -> str:
    price = f"{e['price']:.2f}" if e.get("price") else "—"
    return f"【{e['action']}】{e['name']}（{e['symbol']}·{e['market']}） 现价{price}\n{e['fundamental_verdict']}\n"


def main():
    _load_secrets_into_env()

    backfilled = _backfill_due_advice()
    print(f"（已回填 {backfilled} 条到期的历史建议价格）\n")

    position_results = advise_positions()
    for e in position_results:
        tracker.log_advice(
            _EMAIL, e["symbol"], e.get("price"), e["fundamental_verdict"],
            e["technical_signal"], e["action"], e["market"], e["name"], source="position",
        )
    print(f"（持仓判断：{len(position_results)} 只已更新，结果在网站持仓页面查看，不进本条简报正文）\n")

    # 组合分析要覆盖所有注册用户（不是只给_EMAIL这一个固定账号算）——
    # 用户明确要求过，跟上面单支持仓判断/screen候选只服务_EMAIL这个私人
    # 脚本账号是两条不同的规则，不要改混了。每个用户各自一次AI调用，
    # 互相独立，一个失败不影响其他人。
    portfolio_emails = tracker.get_all_position_emails()
    portfolio_done = 0
    for pe in portfolio_emails:
        try:
            if advise_portfolio(pe):
                portfolio_done += 1
        except Exception as e:
            print(f"（{pe} 的组合分析失败：{e}）")
    print(f"（组合分析：{portfolio_done}/{len(portfolio_emails)} 个用户已更新，结果在网站持仓页面查看，不进本条简报正文）\n")

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
        )

    us_top = _top_picks(judged, "US")
    hk_top = _top_picks(judged, "HK")
    a_top = _top_picks(judged, "A")

    print(f"==================== 最值得关注：美股 Top {len(us_top)} ====================")
    for e in us_top:
        print(_fmt_entry(e))

    print(f"==================== 最值得关注：港股 Top {len(hk_top)} ====================")
    for e in hk_top:
        print(_fmt_entry(e))

    print(f"==================== 最值得关注：A股 Top {len(a_top)} ====================")
    for e in a_top:
        print(_fmt_entry(e))

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
