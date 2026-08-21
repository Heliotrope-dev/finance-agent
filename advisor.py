"""投研顾问 —— 每个工作日收盘后扫一遍港美股全市场，用 Futu 的股票筛选器按市值/
估值/盈利增长挑出候选，基本面为主、技术面辅助，给买卖参考。私有工具，不在
Streamlit 页面里，由 OpenClaw 的 stock-advisor cron 触发，走
`venv/bin/python3 advisor.py`，结果打印到 stdout 给 agent 读了转成微信消息。

跟 analysis.py 刻意"只讲事实不下结论"的公开页面定位不同——这里明确要给买卖
参考，所以判断函数（judge_stock）单独新写，不改 analysis.py 里现成的那几个。

第一版做过自选股+A股候选池，用户反馈不需要自选股这块，目标就是"扫几乎全部
港美股，挑几个有潜力有机遇的股票参考"——data_sources.py 里原有的港美股"候选池"
函数（get_index_top_movers 之类）靠的是人气榜/硬编码知名股名单，覆盖面完全
撑不起"全市场筛选"这个目标，改用 Futu SDK 自带的 get_stock_filter（按市值/PE/
盈利增长这些真实指标在全市场服务端筛选，不是本地维护的名单），实测 US 市场
一次筛选能命中一千多只符合条件的股票，HK 三百多只，是真正的全市场覆盖。
"""

import os

# 必须在任何可能引入 tqdm/streamlit 的 import 之前设置，减少 exec 工具读输出时
# 的噪音（详见下方 __main__ 里的说明）。
os.environ.setdefault("TQDM_DISABLE", "1")
import logging as _logging
_logging.getLogger("streamlit").setLevel(_logging.ERROR)

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

# 每个市场各筛多少只候选交给AI判断——控制AI调用次数和运行时长，不是"候选只有
# 这么多"，Futu那边符合条件的往往有几百上千只，这里只取排序后最靠前的一批。
_US_CANDIDATE_CAP = 15
_HK_CANDIDATE_CAP = 15


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
    return OpenAI(api_key=key, base_url=_DEEPSEEK_BASE, max_retries=2)


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


def _futu_screen(market, quarter, cap_threshold: float, num: int) -> list[dict]:
    """在 market 全市场范围内按"市值下限 + PE在合理区间(0-50，排除亏损股和
    极端高估值) + 最近一期净利润同比增速>10%"筛选，按增速降序排，取前 num 只。
    这三个字段是实测验证过 Futu 账号权限下能用的（VOLUME/换手率这类字段测试
    时报"不支持该过滤字段"，权限或字段类型不对，没有踩着编）。市值字段/PE
    走 SimpleFilter，净利润增速走 FinancialFilter——两者要分开建，混用会报
    "不支持该过滤字段"（实测确认过）。

    港股和A股的FinancialFilter不支持MOST_RECENT_QUARTER这个quarter选项（实测
    报错"港股和A股不支持最近季报选项"），只能用美股。这里quarter参数由调用方
    按市场传对应支持的枚举。

    单独开一个短连接跑完就关，不复用app.py里_futu_call那条常驻worker路径——
    这个脚本是独立进程，跟app.py是完全不同的Python进程，天然没法共享同一个
    连接对象。
    """
    try:
        ctx = ft.OpenQuoteContext(host="127.0.0.1", port=11111)
    except Exception:
        return []
    try:
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

        ret, data = ctx.get_stock_filter(
            market=market, filter_list=[f_cap, f_pe, f_growth], begin=0, num=num
        )
        if ret != ft.RET_OK:
            return []
        _last_page, _all_count, ret_list = data
        market_code = "HK" if market == ft.Market.HK else "US"
        return [
            {"symbol": item.stock_code.split(".", 1)[-1], "name": item.stock_name, "market": market_code}
            for item in ret_list
        ]
    except Exception:
        return []
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def screen_hk_us_candidates() -> list[dict]:
    us = _futu_screen(
        ft.Market.US, ft.FinancialQuarter.MOST_RECENT_QUARTER,
        cap_threshold=2_000_000_000, num=_US_CANDIDATE_CAP,
    )
    hk = _futu_screen(
        ft.Market.HK, ft.FinancialQuarter.ANNUAL,
        cap_threshold=5_000_000_000, num=_HK_CANDIDATE_CAP,
    )
    items = us + hk
    if not items:
        return []
    results = _run_concurrent_with_deadline(
        items, lambda it: _judge_one(it, "screen"), timeout=400, max_workers=5
    )
    return [results[i] for i in sorted(results) if results[i] and "error" not in results[i]]


_JUDGE_SYSTEM = """你是一位理性、保守的投研助理，服务对象是一位会自己做最终决策的
个人投资者——你的任务是基于给定的真实数据给出参考判断，不是替他下单。

要求：
1. 先看基本面（盈利能力、营收/利润增长趋势、负债水平、估值是否处于合理区间），
   这是判断的主要依据。
2. 技术面信号只作为买卖时机的辅助确认，不能单独作为理由。
3. 结论必须是以下四选一：买入 / 卖出 / 持有 / 观望。基本面不过关的，即使技术面
   好看也不能给买入；数据不足以支撑判断时选观望，不要勉强给结论。
4. 明确写清楚为什么——依据是财报里的哪一点、技术面的哪个信号，不要空泛地说
   "综合来看""值得关注"这类没有信息量的话。
5. 只能用给定的数据做判断，不能编造任何数字或消息。
6. 最后必须附一句："仅供参考，不构成投资建议，请自行判断。"

严格按以下格式输出（不要多余寒暄）：
结论：[买入/卖出/持有/观望]
置信度：[高/中/低]
基本面：<一到两句话>
技术面：<一句话>
理由：<两到三句话，具体点出依据>
"""


def judge_stock(symbol: str, market: str, name: str, financial_summary: str,
                 technical_summary: str, news_summary: str) -> dict:
    user_content = (
        f"股票：{name}（{symbol}，{market}股）\n\n"
        f"财务摘要：\n{financial_summary or '（暂无财务数据）'}\n\n"
        f"技术面信号：{technical_summary or '（数据不足）'}\n\n"
        f"近期新闻：\n{news_summary or '（暂无相关新闻）'}"
    )
    resp = _client().chat.completions.create(
        model=_MODEL,
        messages=[
            {"role": "system", "content": _JUDGE_SYSTEM},
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


def _judge_one(item: dict, source: str) -> dict | None:
    symbol, market, name = item["symbol"], item.get("market", "US"), item.get("name", "")
    try:
        price = ds.get_stock_realtime(symbol, market).get("最新价")
    except Exception:
        price = None
    fin = _financial_summary_text(symbol, market)
    tech = _technical_summary_text(symbol, market)
    news = _news_summary_text(name or symbol)
    try:
        verdict = judge_stock(symbol, market, name, fin, tech, news)
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


def _fmt_entry(e: dict) -> str:
    price = f"{e['price']:.2f}" if e.get("price") else "—"
    return f"【{e['action']}】{e['name']}（{e['symbol']}·{e['market']}） 现价{price}\n{e['fundamental_verdict']}\n"


def main():
    _load_secrets_into_env()

    backfilled = _backfill_due_advice()
    print(f"（已回填 {backfilled} 条到期的历史建议价格）\n")

    results = screen_hk_us_candidates()
    if not results:
        print("今天没有拿到任何有效判断（可能是Futu筛选失败或数据源全部失败），本次不生成建议。")
        return

    print(f"==================== 港美股潜力候选（共{len(results)}只） ====================")
    for e in results:
        print(_fmt_entry(e))
        tracker.log_advice(
            _EMAIL, e["symbol"], e.get("price"), e["fundamental_verdict"],
            e["technical_signal"], e["action"], e["market"], e["name"], source="screen",
        )

    print(f"\n共 {len(results)} 条判断已记录。以上为数据驱动的参考意见，不构成投资建议，请自行判断。")


if __name__ == "__main__":
    main()
    # get_stock_realtime_futu 建立的 Futu SDK 连接会开一个非 daemon 线程，main()
    # 跑完所有逻辑之后进程并不会自己退出——实测复现过：日志打印完"共N条判断已
    # 记录"，进程还是挂着一直到外层 timeout 才被杀掉。这里所有该做的事（DB写入/
    # 打印结果）都已经在 main() 里同步完成了，直接强制退出，不等这些残留线程。
    import os as _os
    _os._exit(0)
