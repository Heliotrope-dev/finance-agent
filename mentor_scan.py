# -*- coding: utf-8 -*-
"""盘中导师式推荐——机械盯盘层，不用AI。

2026-09-08新增，跟intraday_watch.py（现有的、每3分钟跑一次保护真实持仓
的盯盘脚本）是平行系统，不共用状态文件、不改老脚本一个字——那条链路
现在正在真实保护用户持仓，改造风险由新脚本自己承担，不牵连老的。

这一层只负责"发现触发了"，不负责解释、不调AI、不发消息。有事件就返回
结构化事件列表，交给mentor_interpret.py去调AI生成解读+推送；没事件返回
"静默"，上层直接NO_REPLY，完全不产生AI调用和微信消息。

监控范围是当天该市场的完整候选池（daily_plan_{market}.json里的"候选池
明细"，不是只看Top3/展示用的"关注候选"）加上该市场的真实持仓——候选池
被截断到展示条数之外的标的一样可能在盘中冒出机会，不能因为它没排进
Top3就不管。
"""
import datetime as dt
import json
from pathlib import Path

import advisor
import data_sources as ds
import tracker

_DATA_DIR = Path(__file__).resolve().parent / "data"

# 持仓当日跌幅超过这个数就预警，即使还没到止损——跟intraday_watch.py
# 用同样的阈值，两套系统对同一件事的判断标准要一致，不然用户会疑惑
# 为什么老系统报了新系统没报（或者反过来）。
_DROP_ALERT_PCT = 4.0
_RISE_ALERT_PCT = 5.0
# 放量异动阈值：量比(Futu字段，含义是"今日成交量/过去N日平均"，不是精确
# 的"过去20个交易日同一时段累计量"口径，那种口径需要历史分时数据，这个
# 项目现在没有这个数据源)达到这个倍数才触发。先用能拿到的数据兜底，
# 事件里明确标注口径，不假装精确度比实际更高。
_VOLUME_SPIKE_RATIO = 3.0


def _state_path(market: str) -> Path:
    return _DATA_DIR / f"mentor_state_{market.lower()}.json"


def _load_state(market: str) -> dict:
    try:
        d = json.loads(_state_path(market).read_text(encoding="utf-8"))
        # 换天清空，去重是"每天每支每类事件最多一次"，不是"永远只一次"。
        if d.get("date") == dt.date.today().isoformat():
            return d
    except Exception:
        pass
    return {"date": dt.date.today().isoformat(), "fired": {}}


def _save_state(market: str, st: dict) -> None:
    try:
        _state_path(market).parent.mkdir(parents=True, exist_ok=True)
        _state_path(market).write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[mentor_scan/{market}] 状态写入失败: {e}")


def _load_plan(market: str) -> dict:
    path = _DATA_DIR / f"daily_plan_{market.lower()}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


_TRADING_CAL = Path("/root/.openclaw/workspace/scripts/trading_cal.py")


def _is_trading_day(market: str) -> bool:
    """复用项目里已经在用的trading_cal.py（港交所/纽交所2026官方节假日表），
    不新建一份日历——cron本身的星期几过滤（1-5）已经挡掉周末，这里补上
    法定假日落在工作日的情况（比如国庆、圣诞）。trading_cal.py不在
    finance-agent仓库里，是OpenClaw workspace那边的脚本，跑不通/找不到
    时保守当成交易日处理，不要因为这一步失败就整天不盯盘。
    """
    if not _TRADING_CAL.exists():
        return True
    try:
        import subprocess
        r = subprocess.run(
            ["python3", str(_TRADING_CAL), market], capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() != "False"
    except Exception:
        return True


def scan(market: str) -> dict:
    """单次扫描，返回结构化结果，不发消息、不调AI。

    返回格式：
      {"状态": "跳过"|"静默"|"有事件", "说明": ..., "事件": [...]}
    事件字段：类型/紧急度/代码/名称/现价/当日涨跌 + 该类型专属字段
    （止损/目标/买入区间/量比等），供mentor_interpret.py组织AI prompt用。
    """
    if not _is_trading_day(market):
        return {"状态": "跳过", "说明": f"今天不是{market}交易日（周末或法定假日），不盯盘"}

    email = advisor._EMAIL
    st = _load_state(market)
    fired = st.get("fired") or {}
    plan = _load_plan(market)
    if not plan:
        return {"状态": "跳过", "说明": f"{market}今天还没有盘前快照（09:00/21:00那条），先等它跑完"}

    pool = plan.get("候选池明细") or []
    levels: dict[str, dict] = {}
    for x in pool:
        code = x.get("代码")
        if code:
            levels[code] = x

    watch: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for p in tracker.get_positions(email):
        if p.get("market") != market:
            continue
        sym = str(p.get("symbol") or "")
        if not sym or sym in seen:
            continue
        seen.add(sym)
        watch.append((sym, (p.get("shares") or 0) > 0))
    for x in pool:
        sym = x.get("代码")
        if sym and sym not in seen:
            seen.add(sym)
            watch.append((sym, False))

    if not watch:
        return {"状态": "跳过", "说明": "候选池和持仓都是空的"}

    quotes = ds.get_stock_realtime_futu_batch([(s, market) for s, _ in watch])
    if not quotes:
        return {"状态": "跳过", "说明": "行情取不到"}

    events: list[dict] = []

    def once(sym: str, kind: str) -> bool:
        k = f"{sym}:{kind}"
        if fired.get(k):
            return False
        fired[k] = dt.datetime.now().strftime("%H:%M")
        return True

    for sym, is_held in watch:
        q = quotes.get((sym, market)) or {}
        last = q.get("最新价")
        prev = q.get("昨收")
        if not last or not prev:
            continue
        day_pct = (last - prev) / prev * 100
        lv = levels.get(sym) or {}
        name = lv.get("名称") or sym
        stop = lv.get("止损参考")
        target = lv.get("目标价")
        buy_lo = lv.get("买入下沿")
        buy_hi = lv.get("买入上限")

        if is_held:
            if stop and last <= stop and once(sym, "止损"):
                events.append({"类型": "止损触发", "紧急度": "紧急", "代码": sym, "名称": name,
                               "市场": market, "现价": last, "止损": stop,
                               "当日涨跌": round(day_pct, 1)})
            elif target and last >= target and once(sym, "目标"):
                events.append({"类型": "目标触及", "紧急度": "止盈", "代码": sym, "名称": name,
                               "市场": market, "现价": last, "目标": target,
                               "当日涨跌": round(day_pct, 1)})
            elif day_pct <= -_DROP_ALERT_PCT and once(sym, "急跌"):
                events.append({"类型": "持仓急跌", "紧急度": "预警", "代码": sym, "名称": name,
                               "市场": market, "现价": last, "止损": stop,
                               "当日涨跌": round(day_pct, 1)})
            elif day_pct >= _RISE_ALERT_PCT and once(sym, "急涨"):
                events.append({"类型": "持仓急涨", "紧急度": "异动", "代码": sym, "名称": name,
                               "市场": market, "现价": last, "目标": target,
                               "当日涨跌": round(day_pct, 1)})
        else:
            # 重新回到买入区间——跟"止损位"不是同一件事：买入区间是早盘算好
            # 的合理进场价位，止损是风险边界，数值上通常不一样（买入区间
            # 上沿明显高于止损）。只在当天第一次落入区间时触发一次。
            if buy_lo and buy_hi and buy_lo <= last <= buy_hi and once(sym, "回到买入区间"):
                events.append({"类型": "回到买入区间", "紧急度": "机会", "代码": sym, "名称": name,
                               "市场": market, "现价": last,
                               "买入区间": f"{buy_lo}~{buy_hi}",
                               "当日涨跌": round(day_pct, 1)})

        # 放量异动，持仓和候选都检查。
        vr = q.get("量比")
        if vr is not None and vr >= _VOLUME_SPIKE_RATIO and once(sym, "放量异动"):
            events.append({"类型": "放量异动", "紧急度": "异动", "代码": sym, "名称": name,
                           "市场": market, "现价": last, "量比": vr,
                           "口径说明": "量比是Futu的成交量/过去若干日均量字段，不是严格的20日同时段累计量对比",
                           "当日涨跌": round(day_pct, 1)})

    st["fired"] = fired
    _save_state(market, st)

    if not events:
        return {"状态": "静默", "盯盘": len(watch), "说明": "没有触发任何条件"}
    return {"状态": "有事件", "盯盘": len(watch), "事件": events}


if __name__ == "__main__":
    import sys
    advisor._load_secrets_into_env()
    _market = sys.argv[sys.argv.index("--market") + 1].upper() if "--market" in sys.argv else "HK"
    out = scan(_market)
    print(json.dumps(out, ensure_ascii=False))
    sys.stdout.flush()
    __import__("os")._exit(0)
