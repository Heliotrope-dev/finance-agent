"""投研顾问 —— 每日收盘后跑一遍自选股 + A股候选池，基本面为主、技术面辅助，
给买卖参考。私有工具，不在 Streamlit 页面里，由 OpenClaw 的 stock-advisor cron
每个工作日 17:30 触发，走 `venv/bin/python3 advisor.py`，结果打印到 stdout 给
agent 读了转成微信消息。

跟 analysis.py 刻意"只讲事实不下结论"的公开页面定位不同——这里明确要给买卖
参考，所以判断函数（judge_stock）单独新写，不改 analysis.py 里现成的那几个。
"""

import os
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as _futures_wait
from datetime import datetime, timedelta

import akshare as ak
import toml
from openai import OpenAI

import charts
import data_sources as ds
import tracker

_EMAIL = "a13989358483@gmail.com"  # 私人工具，固定单用户，不做多用户
_SECRETS_PATH = os.path.join(os.path.dirname(__file__), ".streamlit", "secrets.toml")
_MODEL = "deepseek-v4-flash"
_DEEPSEEK_BASE = "https://api.deepseek.com"
_CANDIDATE_CAP = 25  # A股全市场初筛候选上限，控制AI调用次数和运行时长


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


def is_a_share_trading_day(date=None) -> bool:
    """用 akshare 官方交易日历接口判断，不手工维护节假日表——每年不用改代码，
    这点比 OpenClaw 现成的 trading_cal.py（手工维护的年度节假日 set，只支持
    HK/US）更省心，A 股节假日跟港股/美股也不是同一套。"""
    d = date or datetime.now().date()
    try:
        cal = ak.tool_trade_date_hist_sina()
        trade_dates = set(cal["trade_date"].astype(str))
        return d.isoformat() in trade_dates or d.strftime("%Y-%m-%d") in trade_dates
    except Exception:
        # 接口挂了的兜底：至少排除周末，避免完全跑不起来
        return d.weekday() < 5


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
        # 800太小——DeepSeek隐藏的reasoning_content跟正式回答共用同一个max_tokens
        # 预算，这个项目自己就反复踩过这个坑（README"AI分析概率性返回空内容"），
        # 连2000都不够，最复杂的cross_validate最后调到4000。实测这个800确实
        # 复现了同一个bug：全部22条判断fundamental_verdict都是空字符串。
        max_tokens=4000,
        temperature=0.3,
        stream=False,
    )
    text = resp.choices[0].message.content or ""
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
    symbol, market, name = item["symbol"], item.get("market", "A"), item.get("name", "")
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


def advise_watchlist() -> list[dict]:
    items = tracker.get_watchlist(_EMAIL)
    if not items:
        return []
    results = _run_concurrent_with_deadline(
        items, lambda it: _judge_one(it, "watchlist"), timeout=180, max_workers=4
    )
    return [results[i] for i in sorted(results) if results[i] and "error" not in results[i]]


def _st_filtered(df):
    if df is None or df.empty or "名称" not in df.columns:
        return df
    return df[~df["名称"].str.contains("ST", case=False, na=False)]


def screen_market_candidates() -> list[dict]:
    """v1 只做 A 股：热门板块成分股 + 涨停股池拼一个候选池，本地粗筛（剔除
    ST/*ST）后限量再跑 AI，不对全市场几千只股票跑判断。港股/美股全市场筛选
    留到 v2——现有全市场扫描函数覆盖面撑不起"挖掘潜力股"这个目标（见
    data_sources.py 里 get_index_top_movers 的实现：HK 靠人气榜、US 靠硬编码
    名单），等有更好的数据源再做。
    """
    candidates: dict[str, dict] = {}

    try:
        sectors = ds.get_hot_sectors("A", limit=8)
        for _, row in sectors.iterrows():
            try:
                cons = _st_filtered(ds.get_sector_constituents("A", row["板块"], limit=6))
                if cons is None:
                    continue
                for _, c in cons.iterrows():
                    candidates[c["代码"]] = {"symbol": c["代码"], "name": c["名称"], "market": "A"}
            except Exception:
                continue
    except Exception:
        pass

    try:
        pool = _st_filtered(ds.get_limit_pool("up", limit=15))
        if pool is not None:
            for _, c in pool.iterrows():
                candidates.setdefault(c["代码"], {"symbol": c["代码"], "name": c["名称"], "market": "A"})
    except Exception:
        pass

    items = list(candidates.values())[:_CANDIDATE_CAP]
    if not items:
        return []
    results = _run_concurrent_with_deadline(
        items, lambda it: _judge_one(it, "screen"), timeout=400, max_workers=5
    )
    return [results[i] for i in sorted(results) if results[i] and "error" not in results[i]]


def _backfill_due_advice() -> int:
    due = tracker.get_due_for_advice_review(_EMAIL)
    if not due:
        return 0

    def _fetch_price(row):
        try:
            return ds.get_stock_realtime(row["symbol"], row.get("market", "A")).get("最新价")
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

    if not is_a_share_trading_day():
        print("今天不是A股交易日，跳过本次投研建议。")
        return

    backfilled = _backfill_due_advice()
    print(f"（已回填 {backfilled} 条到期的历史建议价格）\n")

    watch_results = advise_watchlist()
    screen_results = screen_market_candidates()

    if not watch_results and not screen_results:
        print("今天没有拿到任何有效判断（可能是数据源全部失败），本次不生成建议。")
        return

    print("=" * 20 + " 自选股 " + "=" * 20)
    if watch_results:
        for e in watch_results:
            print(_fmt_entry(e))
            tracker.log_advice(
                _EMAIL, e["symbol"], e.get("price"), e["fundamental_verdict"],
                e["technical_signal"], e["action"], e["market"], e["name"], source="watchlist",
            )
    else:
        print("（自选股判断全部失败或列表为空）")

    print("=" * 20 + " A股潜力候选 " + "=" * 20)
    if screen_results:
        for e in screen_results:
            print(_fmt_entry(e))
            tracker.log_advice(
                _EMAIL, e["symbol"], e.get("price"), e["fundamental_verdict"],
                e["technical_signal"], e["action"], e["market"], e["name"], source="screen",
            )
    else:
        print("（候选池筛选全部失败或没有找到符合条件的候选）")

    total = len(watch_results) + len(screen_results)
    print(f"\n共 {total} 条判断已记录。以上为数据驱动的参考意见，不构成投资建议，请自行判断。")


if __name__ == "__main__":
    main()
