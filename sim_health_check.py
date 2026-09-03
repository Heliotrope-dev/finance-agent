"""AI模拟盘静默异常体检——2026-09-04三端对照时的直接产物。

这次排查暴露出来的两个真bug有一个共同点：都是"静默"的。一个是虚拟现金结算
被跳过（富途那边真成交了、持仓真变了，只有台账没动，账面收益率一点点飘走），
一个是AI每5分钟开出同一笔买入、每5分钟被同一道闸拦掉，连续十几轮空转。两个
都跑了不止一天，systemd是active的、cron状态是ok的、页面也照常打开——没有
任何一层监控会喊一声，全靠人肉翻数据库才发现。

所以这个脚本盯的不是"服务活没活着"（那个systemd和OpenClaw的cron status已经
在管了），而是"服务活着但在干错事"。判据全部来自这次真实踩到的坑：

  1. 结算漏账：某轮说"执行成功N条"却没扣手续费。_settle_virtual_cash只要
     按symbol匹配上了就必然产生手续费，一分钱手续费都没有=这轮结算被静默
     跳过了，正是这次symbol带市场前缀那个bug的精确特征。
  2. 同一标的反复被拦：同一支票连续多轮因为同一个原因被拦截，说明喂给AI的
     额度口径跟代码实际执行的口径对不上——AI在按一个不成立的数字反复下注。
  3. AI连续失败：连续多轮超时/报错，决策链路实质上已经停摆。
  4. 决策循环停摆：最近一次运行距今太久（开盘时段内还这样就是真出事了）。
  5. 台账对不上：user_settings里的虚拟现金跟最新资产快照里记的对不上。
  6. 长时间零成交：连续多轮一笔都没成交，可能是闸门卡死或候选池出了问题。

刻意只用标准库的sqlite3直接读data/track_record.db，不import项目里任何模块
——tracker/advisor/sim_trader那条链路会把futu SDK也拖进来，那样既慢、又要
建富途连接、还得处理"futu的线程不是daemon、跑完不os._exit(0)就变僵尸进程"
这个老坑。一个每20分钟跑一次的体检脚本不值得付这些代价，这台VPS只有1.9GB
内存也经不起。纯读，不写任何表，不下单，不碰锁。

用法：
    python3 sim_health_check.py                 # 总是输出（健康时也报一句平安）
    python3 sim_health_check.py --only-problems # 没问题就什么都不输出
"""
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_DB = Path(__file__).parent / "data" / "track_record.db"
_CN_TZ = timezone(timedelta(hours=8))

# 检查窗口。sim_agent_runs这张表本身只保留最近30条（见tracker.log_sim_agent_run
# 里的清理逻辑），所以再往前也读不到，取30就是取全部。
_WINDOW = 30
# 同一标的连续被拦多少轮算异常。5分钟一轮，3轮=15分钟——这次那个bug连续13轮，
# 阈值定3既能早发现，又不至于把"偶尔撞一次上限"这种正常情况报出来。
_REPEAT_BLOCK_LIMIT = 3
# 连续失败多少轮算异常。AI偶发超时是常态（这次统计30轮里有8轮超时），单次不该
# 报警；连着3轮说明不是抖动。
_CONSEC_FAIL_LIMIT = 3
# 连续多少轮零成交算可疑。开盘时段一个多小时一笔没成交，值得看一眼。
_NO_TRADE_LIMIT = 12
# 最近一次运行距今多久算停摆（分钟）。cron是5分钟一轮，非开盘时段会记"跳过"
# 但仍然会留下记录，所以正常情况下任何时候都不该超过这个数太多。
_STALE_MINUTES = 25
# 虚拟现金台账跟最新快照差多少算对不上（港币）。快照和结算是两个时点写的，
# 有小额差异正常，几十块以上就不对劲了。
_CASH_DRIFT_HKD = 50.0


def _rows(conn, sql, args=()):
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, args)]


def _to_cn(iso: str):
    try:
        return datetime.fromisoformat(iso).astimezone(_CN_TZ)
    except Exception:
        return None


def _fmt(iso: str) -> str:
    t = _to_cn(iso)
    return t.strftime("%m-%d %H:%M") if t else "??"


def check(db_path: Path = _DB) -> list[str]:
    """返回问题列表，空列表=健康。"""
    if not db_path.exists():
        return [f"数据库不存在：{db_path}"]

    problems: list[str] = []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        runs = _rows(
            conn,
            "SELECT run_at, status, note, signals_json FROM sim_agent_runs ORDER BY run_at DESC LIMIT ?",
            (_WINDOW,),
        )
        if not runs:
            return ["sim_agent_runs表里一条记录都没有——决策循环可能从来没成功跑过"]

        # 1) 结算漏账：说了"执行成功N条"(N>=1)却没有"本轮手续费"
        leaked = [
            r for r in runs
            if "执行成功" in (r["note"] or "")
            and "执行成功0条" not in r["note"]
            and "手续费" not in r["note"]
        ]
        if leaked:
            problems.append(
                f"[结算漏账] {len(leaked)}轮显示有成交却没扣手续费，说明虚拟现金结算被静默跳过了"
                f"（富途那边可能真成交了，账面现金/收益率会飘）：\n"
                + "\n".join(f"    {_fmt(r['run_at'])} {r['note']}" for r in leaked[:5])
            )

        # 2) 同一标的因同一原因连续被拦
        streak_key, streak = None, 0
        worst = None
        for r in runs:  # runs是倒序，从最近往前数
            try:
                sigs = json.loads(r["signals_json"] or "[]")
            except Exception:
                sigs = []
            blocked = {(s.get("symbol"), s.get("_drop_reason")) for s in sigs if s.get("_drop_reason")}
            key = sorted(blocked)[0] if len(blocked) == 1 else None
            if key and key == streak_key:
                streak += 1
            elif key:
                streak_key, streak = key, 1
            else:
                streak_key, streak = None, 0
            if streak >= _REPEAT_BLOCK_LIMIT and (worst is None or streak > worst[1]):
                worst = (streak_key, streak)
        if worst:
            (sym, reason), n = worst
            problems.append(
                f"[反复空转] {sym} 连续{n}轮因为「{reason}」被拦截未成交——"
                f"喂给AI的可用额度跟代码实际拦截的口径很可能对不上，AI在按一个不成立的数字反复下注，"
                f"每轮都白烧一次AI调用"
            )

        # 3) 连续失败
        consec = 0
        for r in runs:
            if r["status"] == "失败":
                consec += 1
            else:
                break
        if consec >= _CONSEC_FAIL_LIMIT:
            problems.append(
                f"[决策停摆] 最近连续{consec}轮失败（最新一条：{runs[0]['note']}）——决策链路实质已经停了"
            )

        # 4) 停摆：最近一次运行距今太久
        latest = _to_cn(runs[0]["run_at"])
        if latest:
            gap = (datetime.now(_CN_TZ) - latest).total_seconds() / 60
            if gap > _STALE_MINUTES:
                problems.append(
                    f"[心跳丢失] 最近一次决策是{_fmt(runs[0]['run_at'])}，距今{gap:.0f}分钟——"
                    f"cron是5分钟一轮，超过{_STALE_MINUTES}分钟说明脚本没被触发或每次都卡死"
                )

        # 5) 长时间零成交
        no_trade = 0
        for r in runs:
            if "执行成功0条" in (r["note"] or "") or r["status"] == "跳过":
                no_trade += 1
            else:
                break
        if no_trade >= _NO_TRADE_LIMIT:
            problems.append(f"[零成交] 最近连续{no_trade}轮一笔都没成交，值得看一眼是不是闸门卡死或候选池出了问题")

        # 6) 台账对不上
        cash_row = _rows(conn, "SELECT sim_virtual_cash_hkd FROM user_settings WHERE sim_virtual_cash_hkd IS NOT NULL LIMIT 1")
        snap_row = _rows(conn, "SELECT snapshot_at, virtual_cash_hkd FROM sim_equity_snapshots ORDER BY snapshot_at DESC LIMIT 1")
        if cash_row and snap_row:
            ledger = cash_row[0]["sim_virtual_cash_hkd"]
            snapped = snap_row[0]["virtual_cash_hkd"]
            snap_at = snap_row[0]["snapshot_at"]
            # 快照(sim_snapshot.py)和结算(sim_agent.py)是两条独立的5分钟cron，
            # 快照之后又成交了一笔的话，台账本来就该领先快照——这不是异常。
            # 第一版没考虑这点，实测直接误报了一次（17:00快照2355，17:04成交
            # 一笔1425的买入，台账变923，被当成"差了1432"报出来）。会对正常
            # 运行报警的监控比没有监控更糟，所以这里先确认"最近一次快照之后
            # 没有发生过成交"，只在两边本该相等的时候才比。
            traded_after = _rows(
                conn,
                "SELECT 1 FROM sim_agent_runs WHERE run_at > ? AND note LIKE '%手续费共HK$%' LIMIT 1",
                (snap_at,),
            )
            if (
                not traded_after
                and ledger is not None and snapped is not None
                and abs(ledger - snapped) > _CASH_DRIFT_HKD
            ):
                problems.append(
                    f"[台账对不上] 最近一次快照({_fmt(snap_at)})之后没有任何成交，"
                    f"但user_settings记的虚拟现金 HK${ledger:,.2f} 跟快照记的 HK${snapped:,.2f} "
                    f"差了 HK${abs(ledger - snapped):,.2f}"
                )
    finally:
        conn.close()
    return problems


def main():
    only_problems = "--only-problems" in sys.argv
    problems = check()
    if problems:
        print("AI模拟盘体检发现问题：")
        for p in problems:
            print(f"  - {p}")
    elif not only_problems:
        print("AI模拟盘体检：未发现异常")
    # 永远返回0——这是个报告脚本，不是断言脚本，非零退出码会让cron记成失败，
    # 反而掩盖了"脚本本身跑通了、只是发现了问题"这个事实。
    return 0


if __name__ == "__main__":
    sys.exit(main())
