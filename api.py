"""JSON 接口层 —— 把现有的 Python 业务逻辑原样暴露成 HTTP 接口。

为什么先做这一层（2026-09-12）：用户批准了前端换成 React。但真正决定这件事
能不能做成的，不是前端用什么框架，而是"业务逻辑能不能脱离 Streamlit 被调用"。
现在所有数据都只在 app.py 的渲染过程里存在，任何非 Streamlit 的客户端都拿不到。
这一层把它们变成普通的 JSON 接口，之后：

- React 前端直接消费这些接口；
- 微信提醒、早报这些定时任务也不用再各自 import 一堆内部函数；
- Streamlit 那版继续照常跑，一行都不用改。

也就是说这个文件本身就有独立价值，不是只为 React 服务的中间产物——即使
最后决定不换前端，它也该留着。

内存约束（重要）：这台 VPS 只有 1.9G 内存，2026-09-11 刚因为内存打满触发过
一次 OOM（详见 sim_trader / cleanup 那边的注释）。所以：
- 这个服务只做"读"，不开后台任务、不常驻大对象；
- 所有外部数据源调用都走下面这个极小的 TTL 缓存，避免多个客户端同时打同一个
  慢接口（Futu 的行情查询在 data_sources 里本来就是串行排队的）；
- React 前端走静态导出、由 nginx 直接发文件，不在服务器上跑 Node 运行时。

用法：
    venv/bin/uvicorn api:app --host 127.0.0.1 --port 8600
"""

from __future__ import annotations

import time
from typing import Any, Callable

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import advisor
import daily_plan  # noqa: F401  （确保它的数据文件路径常量被初始化）
import data_sources as ds
import sim_agent
import sim_trader
import tracker

app = FastAPI(title="Invest Agent API", version="1.0")

# 开发期前端跑在 localhost:3000，静态导出之后是同源，不需要 CORS。
# 这里只放开本地开发用的来源，不开 "*"。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

_EMAIL = advisor._EMAIL

# ── 极小的 TTL 缓存 ──────────────────────────────────────────────────────
# 刻意不引第三方缓存库：这里要缓存的东西只有几项，而且每项的 TTL 差异很大
# （行情秒级、AI 判断天级）。用一个 dict 足够，也不会在这台内存紧张的机器上
# 多驻留一个库。
_cache: dict[str, tuple[float, Any]] = {}


def _cached(key: str, ttl: float, fn: Callable[[], Any], default: Any = None) -> Any:
    hit = _cache.get(key)
    now = time.time()
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        value = fn()
    except Exception:
        # 取数失败时，宁可把上一次的旧值继续发出去（并在下面标明数据时间），
        # 也不要让整个页面变成错误页——这是 Streamlit 那版一贯的降级方式。
        if hit:
            return hit[1]
        return default
    _cache[key] = (now, value)
    return value


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "ts": time.time()}


@app.get("/api/indices")
def indices() -> dict:
    """首页顶部指数行情条。直接读 warm_home_cache.py 每分钟写的那份缓存，
    不自己去打行情接口——那正是首页当初从 20 秒降到秒开的原因。"""
    cached = _cached("home_map", 30, lambda: ds.load_home_map_cache(max_age_sec=180))
    if not cached:
        return {"items": [], "stale": True}
    snaps, global_idx = cached["snaps"], cached["global_idx"]
    wanted = [("上证指数", "A"), ("恒生指数", "HK"), ("标普500", "US"),
              ("纳斯达克100", "US"), ("日经225", "GLOBAL"), ("德国DAX", "GLOBAL")]
    items = []
    for name, mkt in wanted:
        idx = (global_idx.get(name) if mkt == "GLOBAL"
               else next((i for i in snaps.get(mkt, []) if i["名称"] == name), None))
        if not idx:
            continue
        last, chg = idx.get("最新"), idx.get("涨跌")
        if last is None or chg is None:
            continue
        prev = last - chg
        items.append({
            "name": name, "market": mkt, "last": last, "change": chg,
            "change_pct": (chg / prev * 100) if prev else 0.0,
        })
    return {"items": items, "stale": False}


@app.get("/api/leaderboard")
def leaderboard(limit: int = 5) -> dict:
    """投研观察排行榜。港股/美股各自独立一批，跟 Streamlit 那版同一个数据源。"""
    def _load() -> dict:
        out: dict[str, list[dict]] = {}
        for mk in ("HK", "US"):
            board = tracker.get_latest_leaderboard(limit=limit, source=f"watchlist_{mk.lower()}")
            rows = board.get("rows") if isinstance(board, dict) else board
            out[mk] = [
                {
                    "rank": i,
                    "symbol": r.get("symbol"),
                    "name": r.get("name"),
                    "market": r.get("market"),
                    "action": r.get("action"),
                    "score": r.get("score"),
                    "price_at_advice": r.get("price_at_advice"),
                    "created_at": r.get("created_at"),
                    "breakdown": tracker.extract_score_breakdown(r.get("fundamental_verdict") or ""),
                }
                for i, r in enumerate(rows or [], 1)
            ]
        return out

    return {"boards": _cached("leaderboard", 300, _load, default={})}


@app.get("/api/sim")
def sim() -> dict:
    """AI 模拟盘状态。净值口径跟页面完全一致：只算 AI 自己按成交流水买入的
    仓位，账户里其他来源的持仓单独列出、不计入（见
    sim_trader.get_ledger_reconciled_holdings 的说明）。"""
    def _load() -> dict:
        snapshot = sim_trader.get_agent_snapshot()
        rec = sim_trader.get_ledger_reconciled_holdings(_EMAIL, snapshot)
        cash = tracker.get_sim_virtual_cash(_EMAIL)
        if cash is None:
            cash = sim_agent._VIRTUAL_BUDGET_HKD
        rate = sim_trader.USD_HKD_RATE
        ai_value = rec["ai_value_hkd"]
        net = cash + ai_value
        start = sim_agent._VIRTUAL_BUDGET_HKD
        return {
            "currency": "USD",
            "start_capital": start / rate,
            "cash": cash / rate,
            "ai_holdings_value": ai_value / rate,
            "net_value": net / rate,
            "return_pct": (net - start) / start * 100 if start else None,
            "ai_positions": [
                {"code": p.get("code"), "name": p.get("name"), "qty": p.get("qty"),
                 "pl": (p.get("pl_val") if p.get("currency") == "USD"
                        else (p.get("pl_val") or 0) / rate)}
                for p in rec["ai_positions"]
            ],
            "foreign_positions": [
                {"code": p.get("code"), "name": p.get("name"), "qty": p.get("qty"),
                 "market_val_hkd": p.get("market_val_hkd")}
                for p in rec["foreign_positions"]
            ],
        }

    return _cached("sim", 30, _load, default={}) or {}


@app.get("/api/track-record")
def track_record(limit: int = 20) -> dict:
    """AI 战绩墙：全样本的方向判断胜率 + 最近 N 条逐条结果（亏的不藏）。"""
    def _load() -> dict:
        return {
            "summary": tracker.get_advice_outcome_summary(),
            "recent": tracker.get_recent_advice_outcomes(limit=limit),
        }

    return _cached(f"track_{limit}", 600, _load, default={"summary": {}, "recent": []})


@app.get("/api/plan/{market}")
def plan(market: str) -> dict:
    """今日可执行清单。读 daily_plan.py 写的当天快照文件，不现场重算。"""
    from pathlib import Path
    import json as _json

    path = Path(__file__).resolve().parent / "data" / f"daily_plan_{market.lower()}.json"
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"date": None, "items": []}
    return {
        "date": data.get("日期"),
        "ai_status": data.get("AI状态"),
        "items": data.get("关注候选") or [],
    }


@app.get("/api/news")
def news(limit: int = 12) -> dict:
    """今日重磅消息。带上每条是按哪只异动股搜到的（related），跟页面一致。"""
    def _load() -> list[dict]:
        df = ds.get_hot_market_news(limit=limit)
        if df is None or df.empty:
            return []
        return df.to_dict("records")

    return {"items": _cached(f"news_{limit}", 900, _load, default=[])}
