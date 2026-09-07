# -*- coding: utf-8 -*-
"""盘中盯盘：清单标的和持仓的实时看护，触发条件才推微信。

用户的原话是"我就怕开盘买进去了突然利好利空我会不知所措，因为AI只会在
午间收盘的时候告诉我下步怎么执行"。这是个真实的缺口：午盘 12:00 和收盘
16:10 之间隔着三个多小时，期间跌破止损也没人告诉他。

项目里已经有一个盯盘器 sim_watch.py，但那是给 AI 模拟盘用的，看的是模拟
账户的仓位。这个模块把同一套思路用到用户自己的清单和持仓上。

设计上最要紧的一条：**只在真正需要动的时候出声**。每三分钟推一条"还没到
止损"是灾难——用户会关掉通知，然后真出事的那条也被埋掉了。所以：

  1. 只有触发明确条件才推，平静时完全静默
  2. 同一支票同一类事件每天最多推一次（去重靠落盘的状态文件）
  3. 止损和目标价直接用早上清单里给过的那两个数，不重新算——用户已经
     照着那个数在汇丰挂了单，这里再算出一个不一样的数只会让他困惑

触发条件按紧急程度排：

  跌破止损     持仓，最紧急，说明早上的判断已经被证伪
  触及目标价   持仓，该考虑止盈
  当日急跌     持仓跌超阈值但还没到止损，提前预警而不是等它跌到
  当日急涨     持仓涨超阈值，可能是消息面，值得看一眼
  回到买点     候选股跌到清单给的止损位附近，是更好的介入位置

不判断"该不该买卖"，只报"发生了什么、你早上设的线到了"。真正的决策留给
用户——他人在盘面前，掌握的信息比这个脚本多。
"""
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import advisor
import data_sources as ds
import tracker

_WECHAT_TARGET = "o9cq80_APBq3j8dLdECzrOB0opJs@im.wechat"
_WECHAT_ACCOUNT = "b329c51975ab-im-bot"
_CHANNEL = "openclaw-weixin"

_STATE = Path(__file__).resolve().parent / "data" / "intraday_watch_state.json"
_PLAN = Path(__file__).resolve().parent / "data" / "daily_plan.json"

# 持仓当日跌幅超过这个数就预警，即使还没到止损。止损通常在 -6% 到 -20%
# （按 ATR 定），等跌到那里再说往往已经晚了；-4% 是个"该看一眼"的位置。
_DROP_ALERT_PCT = 4.0
# 当日涨幅超过这个数也提醒——急涨常常是消息面，可能是兑现机会，也可能
# 是该重新评估的信号。
_RISE_ALERT_PCT = 5.0
# 候选股跌到距止损位这个比例以内，算"回到更好的买点"。
_ENTRY_NEAR_PCT = 3.0


def _send(msg: str) -> bool:
    try:
        r = subprocess.run(
            ["openclaw", "message", "send", "--channel", _CHANNEL,
             "--target", _WECHAT_TARGET, "--account", _WECHAT_ACCOUNT,
             "--message", msg],
            capture_output=True, text=True, timeout=90)
        return r.returncode == 0
    except Exception as e:
        print(f"发送异常: {e}")
        return False


def _load_state() -> dict:
    try:
        d = json.loads(_STATE.read_text(encoding="utf-8"))
        # 换天就清空。去重是"每天最多一次"，不是"永远只推一次"——
        # 昨天跌破过止损，今天再跌破仍然要提醒。
        if d.get("date") == dt.date.today().isoformat():
            return d
    except Exception:
        pass
    return {"date": dt.date.today().isoformat(), "fired": {}}


def _save_state(st: dict) -> None:
    try:
        _STATE.parent.mkdir(parents=True, exist_ok=True)
        _STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"状态写入失败: {e}")


def _plan_levels() -> dict:
    """从早上那份清单里取每支票的止损位和目标价。

    用清单里的数而不是现算：用户已经照着那个数在券商挂了止损单，这里
    再算出一个略有出入的数（ATR 每天都在变）只会让两边对不上。
    """
    out = {}
    try:
        plan = json.loads(_PLAN.read_text(encoding="utf-8"))
    except Exception:
        return out
    for grp in ("持仓处理", "关注候选"):
        for x in (plan.get(grp) or []):
            key = f"{x.get('市场')}:{x.get('代码')}"
            out[key] = {
                "名称": x.get("名称"), "止损": x.get("止损参考"),
                "目标": x.get("目标价"), "是候选": grp == "关注候选",
            }
    return out


def check() -> dict:
    email = advisor._EMAIL
    st = _load_state()
    fired = st.get("fired") or {}
    levels = _plan_levels()

    # 要盯的：真实持仓 + 清单里的候选 + 用户自选
    watch: list[tuple[str, str, bool]] = []
    seen = set()
    # positions 表里 shares=0 的行就是"只关注不持仓"的自选（见 tracker.add_watch_only），
    # 原来这里写的是 if shares > 0，正好把自选整个过滤掉了。
    #
    # 2026-09-07 用户说"关注一下智谱、小米、思格新能这几支票到大动作"，查下来
    # 他点名的三支里只有小米在当日清单里，智谱和思格新能根本不在监控范围内。
    # 而当天思格新能盘中一度 -9.75%、智谱从 +0.65% 走到 -2.88%，正是他想第一
    # 时间知道的那种动作。
    #
    # 持仓和自选都要盯，但性质不同：持仓关心"要不要减/清"（第三个字段 True），
    # 自选关心"要不要进"（False），下游按这个区分告警措辞。
    for p in tracker.get_positions(email):
        k = (str(p["symbol"]), p["market"])
        if k in seen:
            continue
        seen.add(k)
        watch.append((k[0], k[1], (p.get("shares") or 0) > 0))
    for key, v in levels.items():
        mkt, sym = key.split(":", 1)
        if (sym, mkt) not in seen:
            seen.add((sym, mkt))
            watch.append((sym, mkt, False))

    if not watch:
        return {"状态": "跳过", "说明": "没有持仓、清单候选，也没有自选"}

    # 只盯正在交易的市场。港股收盘后还在拉美股行情没有意义，反过来也一样。
    import sim_agent
    open_markets = sim_agent._open_markets()
    watch = [w for w in watch if w[1] in open_markets]
    if not watch:
        return {"状态": "跳过", "说明": "关注的标的所在市场都没开盘"}

    quotes = ds.get_stock_realtime_futu_batch([(s, m) for s, m, _ in watch])
    if not quotes:
        return {"状态": "跳过", "说明": "行情取不到"}

    alerts = []
    for sym, mkt, is_held in watch:
        q = quotes.get((sym, mkt)) or {}
        last = q.get("最新价")
        prev = q.get("昨收")
        if not last or not prev:
            continue
        day_pct = (last - prev) / prev * 100
        key = f"{mkt}:{sym}"
        lv = levels.get(key) or {}
        name = lv.get("名称") or sym
        stop, target = lv.get("止损"), lv.get("目标")

        def once(kind: str) -> bool:
            """同一支票同一类事件每天只推一次。"""
            k = f"{key}:{kind}"
            if fired.get(k):
                return False
            fired[k] = dt.datetime.now().strftime("%H:%M")
            return True

        if is_held:
            if stop and last <= stop and once("止损"):
                alerts.append(("紧急", f"{name}（{sym}）跌破止损 {stop}，现价 {last}"
                                      f"（当日{day_pct:+.1f}%）。早上设的线到了。"))
            elif target and last >= target and once("目标"):
                alerts.append(("止盈", f"{name}（{sym}）触及目标价 {target}，现价 {last}"
                                      f"（当日{day_pct:+.1f}%）。可以考虑分批兑现。"))
            elif day_pct <= -_DROP_ALERT_PCT and once("急跌"):
                alerts.append(("预警", f"{name}（{sym}）当日{day_pct:+.1f}%，现价 {last}"
                                      + (f"，止损位 {stop}" if stop else "")
                                      + "。还没到止损，但值得看一眼有没有消息。"))
            elif day_pct >= _RISE_ALERT_PCT and once("急涨"):
                alerts.append(("异动", f"{name}（{sym}）当日{day_pct:+.1f}%，现价 {last}"
                                      + (f"，目标价 {target}" if target else "")
                                      + "。急涨常有消息面，看一眼再决定拿不拿。"))
        else:
            # 候选股：跌到止损位附近意味着更好的介入价，但也可能是逻辑变了。
            # 只报"到位置了"，不说"可以买"。
            if stop and last <= stop * (1 + _ENTRY_NEAR_PCT / 100) and once("买点"):
                alerts.append(("机会", f"{name}（{sym}）跌到 {last}，接近早上清单里的"
                                      f"止损位 {stop}（当日{day_pct:+.1f}%）。"
                                      "这个位置进场性价比更高，但先确认跌的原因。"))

    st["fired"] = fired
    _save_state(st)

    if not alerts:
        return {"状态": "静默", "盯盘": len(watch), "说明": "没有触发任何条件"}

    order = {"紧急": 0, "止盈": 1, "预警": 2, "异动": 3, "机会": 4}
    alerts.sort(key=lambda x: order.get(x[0], 9))
    now = dt.datetime.now(dt.timezone.utc).astimezone().strftime("%H:%M")
    lines = [f"投研站 · 盘中提醒 {now}", ""]
    for tag, text in alerts:
        lines.append(f"[{tag}] {text}")
    lines.append("")
    lines.append("这条只报你早上设的线到了，怎么做由你定。")
    msg = "\n".join(lines)
    ok = _send(msg)
    print(msg)
    return {"状态": "已推送" if ok else "推送失败", "条数": len(alerts)}


if __name__ == "__main__":
    advisor._load_secrets_into_env()
    out = check()
    print(json.dumps(out, ensure_ascii=False))
    # 富途SDK线程非daemon，必须强制退出；os._exit 跳过stdout刷新，先flush。
    sys.stdout.flush()
    __import__("os")._exit(0)
