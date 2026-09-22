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

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import advisor
import auth
import daily_plan  # noqa: F401  （确保它的数据文件路径常量被初始化）
import data_sources as ds
import sim_agent
import sim_trader
import tracker

app = FastAPI(title="Invest Agent API", version="1.0")

# 开发期前端跑在 localhost:3000，静态导出之后是同源。
# allow_credentials 打开是因为 token 走 cookie（跟 Streamlit 版同一个
# fa_auth_tok），浏览器只有在这个开关下才会把 cookie 带上跨源请求；
# 开了它就不能再用 "*" 通配来源，本来也没用。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# 这是 **AI 自己的账号**（advisor.py 里 ADVISOR_EMAIL），不是登录用户。
# 投研判断、模拟盘、战绩墙都是这个账号产出的全站公开数据，每个登录用户看到
# 的是同一份——别把它跟下面 require_user 拿到的当前用户混为一谈。用户私有
# 的数据（持仓、自选、风险偏好）一律以 require_user 的返回值为准。
_EMAIL = advisor._EMAIL

# ── 认证 ────────────────────────────────────────────────────────────────
# 沿用 auth.py 那一套（Supabase users/sessions 表，7 天 token），跟
# math-agent 共用账号体系，也跟 Streamlit 版共用——同一个 token 在两边都
# 认，迁移期用户不用在两个前端各登一次。
#
# token 从两个地方取，优先 Authorization 头：
# - Authorization: Bearer <token>：新前端 fetch 时显式带上，跨源也能用；
# - Cookie fa_auth_tok：Streamlit 版写的那个，同源访问时浏览器自动带上，
#   于是从 Streamlit 登录过的浏览器打开新前端是直接登录状态，不用重登。
_COOKIE_NAME = "fa_auth_tok"
_TOKEN_MAX_AGE = auth._TOKEN_DAYS * 24 * 3600


def _token_from(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.cookies.get(_COOKIE_NAME, "")


def require_user(request: Request) -> str:
    """登录墙。返回当前用户邮箱，未登录直接 401。

    Streamlit 版的行为是"不登录什么都看不到"（游客模式 2026-08-25 关掉了），
    新前端保持一致：除了 /api/health 和 /api/auth/* 之外所有接口都挂这个。
    """
    token = _token_from(request)
    email = auth._validate_token(token) if token else None
    if not email:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return email


class LoginBody(BaseModel):
    email: str
    password: str


class RegisterBody(BaseModel):
    email: str
    password: str


@app.post("/api/auth/login")
def login(body: LoginBody, response: Response) -> dict:
    ok, msg = auth._check_user(body.email, body.password)
    if not ok:
        # 401 而不是 400：前端靠状态码区分"密码错"和"参数不合法"。
        # 文案原样透传 auth.py 的——那边刻意把"邮箱不存在"和"密码错"统一
        # 成同一句来防账号枚举，这里不要好心拆开。
        raise HTTPException(status_code=401, detail=msg)
    token = auth._create_token(body.email)
    # 同时下发 cookie：静态导出的前端没有服务端，cookie 是"下次打开还在
    # 登录态"最省事的一条路，而且跟 Streamlit 版写的是同一个名字，两边
    # 互认。Secure 只在 https 下有意义，本地 http 开发时浏览器会忽略它，
    # 所以本地登录靠的是前端存的那份 localStorage。
    response.set_cookie(
        _COOKIE_NAME, token, max_age=_TOKEN_MAX_AGE,
        httponly=False, samesite="lax", secure=True, path="/",
    )
    return {"token": token, "email": body.email}


@app.post("/api/auth/register")
def register(body: RegisterBody) -> dict:
    # 校验规则跟 Streamlit 版登录页一字不差，否则同一个密码在两边一个能注
    # 册一个不能，用户只会觉得是 bug。
    if not body.email or "@" not in body.email:
        raise HTTPException(status_code=400, detail="请输入有效邮箱")
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="密码至少6位")
    if auth._user_exists(body.email):
        raise HTTPException(
            status_code=409,
            detail="该邮箱已注册（跟 math-agent 共用同一套账号，那边注册过这里也能直接登）",
        )
    try:
        auth._register_user(body.email, auth._hash_pw(body.password))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"注册失败：{exc}") from exc
    return {"ok": True}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response) -> dict:
    token = _token_from(request)
    if token:
        auth._invalidate_token(token)
    response.delete_cookie(_COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def me(email: str = Depends(require_user)) -> dict:
    return {"email": email}


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
def indices(_: str = Depends(require_user)) -> dict:
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
def leaderboard(limit: int = 5, _: str = Depends(require_user)) -> dict:
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
def sim(_: str = Depends(require_user)) -> dict:
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
def track_record(limit: int = 20, _: str = Depends(require_user)) -> dict:
    """AI 战绩墙：全样本的方向判断胜率 + 最近 N 条逐条结果（亏的不藏）。"""
    def _load() -> dict:
        return {
            "summary": tracker.get_advice_outcome_summary(),
            "recent": tracker.get_recent_advice_outcomes(limit=limit),
        }

    return _cached(f"track_{limit}", 600, _load, default={"summary": {}, "recent": []})


@app.get("/api/plan/{market}")
def plan(market: str, _: str = Depends(require_user)) -> dict:
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


# ── 行情分区 ────────────────────────────────────────────────────────────
# Streamlit 版这一块是三个独立 fragment（指数快照 / 涨跌排行 / 热门板块），
# 各自刷新互不牵连。接口也照着拆成三个，前端可以分别加载：其中板块和排行
# 在某些市场是出了名的慢（data_sources 的注释里记着港股热门板块实测近 10 秒、
# 沪深全市场快照曾经要 2 分钟），一个慢接口不该拖住整页。
_MARKETS = ("A", "HK", "US", "CRYPTO")


def _check_market(market: str) -> str:
    mk = market.upper()
    if mk not in _MARKETS:
        raise HTTPException(status_code=404, detail=f"未知市场：{market}")
    return mk


def _df_records(df, rename: dict[str, str] | None = None) -> list[dict]:
    """DataFrame → JSON 安全的 records。

    必须过一遍 NaN：pandas 缺值是 float('nan')，而 JSON 没有 NaN 这个字面量，
    直接序列化出去浏览器 JSON.parse 会整个失败——不是某一格显示成空，是整个
    接口的响应都解析不了。统一换成 None，前端的 fmt* 函数本来就认 null。
    """
    if df is None or getattr(df, "empty", True):
        return []
    if rename:
        df = df.rename(columns=rename)
    out = []
    for rec in df.to_dict("records"):
        out.append({k: (None if (isinstance(v, float) and v != v) else v)
                    for k, v in rec.items()})
    return out


@app.get("/api/market/{market}/indices")
def market_indices(market: str, _: str = Depends(require_user)) -> dict:
    """指数快照。跟首页指数条同一份预热缓存（warm_home_cache.py 每分钟写），
    缓存没有或太旧才退回实时查询——这正是 Streamlit 版把这块从"偶尔卡 10 秒"
    改成秒开的做法，不要改回去各自开一条查询路径。"""
    mk = _check_market(market)

    def _load() -> list[dict]:
        cached = ds.load_home_map_cache(max_age_sec=90)
        if cached:
            rows = cached["snaps"].get(mk, [])
            if rows:
                return rows
        return ds.get_multi_index_snapshot(mk)

    rows = _cached(f"mkt_idx:{mk}", 60, _load, default=[]) or []
    items = []
    for r in rows:
        last, chg = r.get("最新"), r.get("涨跌")
        prev = (last - chg) if (last is not None and chg is not None) else None
        items.append({
            "name": r.get("名称"),
            "code": r.get("代码"),
            "last": last,
            "change": chg,
            "change_pct": (chg / prev * 100) if prev else None,
        })
    return {"market": mk, "items": items}


@app.get("/api/market/{market}/movers")
def market_movers(market: str, limit: int = 15, _: str = Depends(require_user)) -> dict:
    """个股涨跌排行。各市场取数路径不同（沪深走涨停池、港股美股走各自的核心股
    榜），口径差异在 data_sources 里已经解释过，这里只负责把它们统一成同一种
    形状给前端，不去抹平差异本身——页面上要如实标注这不是官方成分股名单。"""
    mk = _check_market(market)

    def _load() -> list[dict]:
        if mk == "CRYPTO":
            return _df_records(ds.get_crypto_quotes())
        if mk == "HK":
            return _df_records(ds.get_hk_famous_movers(limit=limit))
        if mk == "US":
            return _df_records(ds.get_us_famous_movers(limit=limit))
        return _df_records(ds.get_index_top_movers(mk, limit=limit))

    # TTL 按市场分开，因为取数耗时差一个数量级（本地实测：沪深/美股 2 秒，
    # 港股 81 秒——data_sources 里早就记着港股这条路慢）。港股用短 TTL 的
    # 后果是用户频繁撞上那 81 秒的冷启动，而这类"核心股涨跌榜"本来就不需要
    # 秒级新鲜，拉长到 10 分钟是稳赚的交换。
    # CRYPTO 走 Futu，24 小时交易，取中间值。
    _ttl = {"HK": 600, "CRYPTO": 300}.get(mk, 120)
    rows = _cached(f"mkt_movers:{mk}:{limit}", _ttl, _load, default=[]) or []
    items = [{
        "symbol": r.get("代码"),
        "name": r.get("名称"),
        "last": r.get("最新价"),
        "change_pct": r.get("涨跌幅"),
    } for r in rows]
    return {"market": mk, "items": items[:limit]}


@app.get("/api/market/{market}/sectors")
def market_sectors(market: str, limit: int = 12, _: str = Depends(require_user)) -> dict:
    """热门板块。"热度"是成交额/成交量的代理指标，不是官方人气榜——
    data_sources 里说明了为什么用它，前端必须如实标注，别写成"热度指数"。"""
    mk = _check_market(market)
    # CRYPTO：没有行业板块这个概念。
    # US：get_hot_sectors("US") 本地实测跑满 180 秒仍未返回（跟 data_sources
    #     里记的"全市场快照类接口能到 2 分钟"是同一类问题）。让页面为了一块
    #     补充信息干等三分钟是不可接受的交换，这里直接标成不支持、前端不渲染
    #     这一区。要恢复的话得先换一条真正快的数据源，而不是把超时调大。
    if mk in ("CRYPTO", "US"):
        return {"market": mk, "items": [], "supported": False}

    rows = _cached(
        f"mkt_sectors:{mk}:{limit}", 300,
        lambda: _df_records(ds.get_hot_sectors(mk, limit=limit)),
        default=[],
    ) or []
    items = [{
        "name": r.get("板块"),
        "change_pct": r.get("涨跌幅"),
        "heat": r.get("热度"),
    } for r in rows]
    return {"market": mk, "items": items[:limit], "supported": True}


@app.get("/api/news")
def news(limit: int = 12, _: str = Depends(require_user)) -> dict:
    """今日重磅消息。带上每条是按哪只异动股搜到的（related），跟页面一致。"""
    def _load() -> list[dict]:
        df = ds.get_hot_market_news(limit=limit)
        if df is None or df.empty:
            return []
        return df.to_dict("records")

    return {"items": _cached(f"news_{limit}", 900, _load, default=[])}
