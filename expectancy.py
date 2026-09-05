# -*- coding: utf-8 -*-
"""数学期望追踪：这套打分到底有没有优势，用真实复核数据说话。

用户的初心是"通过AI的高数学期望实现资产增值"。数学期望不是一个形容词，
它有确定的算法：

    期望 = 胜率 × 平均盈利 + 败率 × 平均亏损

只要这个数长期为正，重复执行就会赚钱；为负则相反，判断得再"有道理"也没用。
所以这个模块存在的意义是：把"这套系统能不能赚钱"从感觉变成一个可以查的数字。

2026-09-06 审计发现的一件要紧事写在这里，免得以后再被同一个坑绊倒：

当时数据库里 786 条已复核记录显示"分数越高表现越差"（85+分期望 -1.18%，
<55分反而 -0.26%），看上去像是这套打分完全失效。但查复核间隔发现，全部
记录的判断到复核间隔是 0.97~1.43 天——而打分体系有七成权重压在基本面和
估值位置上，那是按季度起作用的信号。用一天的价格波动去检验它，测到的
不是信号质量，是噪音。

09-04 已经把窗口改成 6.9 天，所以那批旧数据是修复前的产物，口径不对，
不能用来下任何结论。真正可用的第一批数据要等 09-07 之后。

由此定下这个模块的一条硬规矩：**样本不足或窗口不对时，返回"尚未验证"，
不返回一个看起来精确的数字。** 一个基于噪音算出来的期望值，比没有数字
危险得多——它会让人误以为自己有依据。
"""
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

_DB = Path(__file__).resolve().parent / "data" / "track_record.db"

# 复核窗口必须达到这个天数，这条记录才算"检验过"。跟 advisor 里的
# _WATCHLIST_REVIEW_MIN_AGE_DAYS 对齐；比它松一点点（5天）是为了容纳
# 节假日导致的复核延迟，但仍然远离"次日噪音"那个区间。
_MIN_VALID_GAP_DAYS = 5.0

# 每个分数段至少要有这么多样本，才给出期望值。30 是统计上"小样本"的
# 常规下限，低于这个数算出来的胜率抖动太大，给出去会被当成结论。
_MIN_SAMPLE = 30


def _conn():
    return sqlite3.connect(_DB)


def _fetch_reviewed(source: str | None = None) -> list[dict]:
    """取出所有"按正确窗口复核过"的记录。

    间隔过滤是这个函数的全部意义所在：数据库里混着旧口径（次日复核）和
    新口径（一周复核）两批数据，不过滤就等于把噪音和信号搅在一起算。
    """
    sql = (
        "SELECT symbol, market, score, action, price_at_advice, review_price, "
        "created_at, review_at, source, "
        "score_fundamental, score_price_position, score_technical, "
        "score_chips, score_analyst, score_data_certainty, "
        "julianday(review_at) - julianday(created_at) AS gap "
        "FROM advice WHERE review_price IS NOT NULL AND price_at_advice > 0 "
        "AND score IS NOT NULL"
    )
    args: list = []
    if source:
        sql += " AND source = ?"
        args.append(source)
    with closing(_conn()) as c:
        c.row_factory = sqlite3.Row
        rows = [dict(r) for r in c.execute(sql, args)]
    return [r for r in rows if (r.get("gap") or 0) >= _MIN_VALID_GAP_DAYS]


def _stats(rets: list[float]) -> dict:
    """一组收益率的期望值拆解。分开返回胜率/平均盈利/平均亏损而不是只给
    一个期望值——期望相同的两个策略，"高胜率小盈利"和"低胜率大盈利"
    在实操上完全是两回事，前者容易坚持，后者需要扛住连续亏损。"""
    if not rets:
        return {}
    wins = [x for x in rets if x > 0]
    losses = [x for x in rets if x <= 0]
    wr = len(wins) / len(rets)
    aw = sum(wins) / len(wins) if wins else 0.0
    al = sum(losses) / len(losses) if losses else 0.0
    return {
        "样本": len(rets),
        "胜率": wr * 100,
        "平均盈利": aw,
        "平均亏损": al,
        "期望": wr * aw + (1 - wr) * al,
        "平均收益": sum(rets) / len(rets),
        # 盈亏比：平均盈利 / 平均亏损绝对值。它跟胜率一起决定期望的正负，
        # 单看任何一个都会误判——30%胜率配3:1盈亏比是赚钱的。
        "盈亏比": (aw / abs(al)) if al else None,
    }


def score_band_expectancy(source: str = "watchlist") -> dict:
    """按分数段算期望值。这是"高分是不是真的更值得买"的直接答案。"""
    rows = _fetch_reviewed(source)
    if len(rows) < _MIN_SAMPLE:
        return {
            "状态": "尚未验证",
            "有效样本": len(rows),
            "说明": (
                f"按正确窗口（≥{_MIN_VALID_GAP_DAYS:.0f}天）复核的样本只有 {len(rows)} 条，"
                f"不足 {_MIN_SAMPLE} 条，任何期望值都算不出统计意义。"
                "在积累够之前，打分只能当作筛选参考，不能当作交易依据。"
            ),
        }

    bands = [(85, 101, "85+"), (75, 85, "75-84"), (65, 75, "65-74"),
             (55, 65, "55-64"), (0, 55, "<55")]
    out = {}
    for lo, hi, label in bands:
        sub = [r for r in rows if lo <= r["score"] < hi]
        rets = [(r["review_price"] - r["price_at_advice"]) / r["price_at_advice"] * 100
                for r in sub]
        s = _stats(rets)
        if s and s["样本"] >= 10:
            out[label] = s
    return {"状态": "已验证", "有效样本": len(rows), "分段": out}


def action_expectancy(source: str = "watchlist") -> dict:
    """按 AI 给出的动作（买入/持有/观望/卖出）算期望值。

    比分数段更贴近实操：用户真正会照着做的是"买入"这个动作，不是"83分"
    这个数字。如果买入的期望是负的，那这个动作标签本身就是有害的。
    """
    rows = _fetch_reviewed(source)
    if len(rows) < _MIN_SAMPLE:
        return {"状态": "尚未验证", "有效样本": len(rows)}
    out = {}
    for act in ("买入", "持有", "观望", "卖出"):
        sub = [r for r in rows if r["action"] == act]
        rets = [(r["review_price"] - r["price_at_advice"]) / r["price_at_advice"] * 100
                for r in sub]
        s = _stats(rets)
        if s and s["样本"] >= 10:
            out[act] = s
    return {"状态": "已验证", "有效样本": len(rows), "分动作": out}


def dimension_edge(source: str = "watchlist") -> dict:
    """六个维度各自有没有预测力：高分组减低分组的收益差。

    这是决定权重该怎么调的依据。某个维度如果高分组反而跑输，说明它要么
    是噪音，要么方向是反的——继续给它 20 分权重就是在往判断里掺沙子。
    """
    rows = _fetch_reviewed(source)
    dims = {
        "基本面": "score_fundamental", "价格位置": "score_price_position",
        "技术面": "score_technical", "筹码面": "score_chips",
        "分析师预期": "score_analyst", "数据确定性": "score_data_certainty",
    }
    out = {}
    for label, col in dims.items():
        sub = [r for r in rows if r.get(col) is not None]
        if len(sub) < _MIN_SAMPLE:
            out[label] = {"状态": "样本不足", "样本": len(sub)}
            continue
        vals = sorted(r[col] for r in sub)
        med = vals[len(vals) // 2]
        f = lambda g: (sum((x["review_price"] - x["price_at_advice"]) / x["price_at_advice"] * 100
                           for x in g) / len(g)) if g else 0.0
        hi = [r for r in sub if r[col] > med]
        lo = [r for r in sub if r[col] <= med]
        out[label] = {
            "状态": "已验证", "样本": len(sub),
            "高分组": f(hi), "低分组": f(lo), "差值": f(hi) - f(lo),
        }
    return out


def pending_review_eta() -> dict:
    """还有多少判断在等复核、最早什么时候能凑够样本。

    用户问"这系统到底行不行"的时候，如果答案是"还不知道"，至少要能说清楚
    "什么时候能知道"。
    """
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    with closing(_conn()) as c:
        rows = list(c.execute(
            "SELECT created_at FROM advice WHERE review_price IS NULL "
            "AND score IS NOT NULL ORDER BY created_at"))
    if not rows:
        return {"待复核": 0}
    dues = []
    for (ca,) in rows:
        try:
            dues.append(dt.datetime.fromisoformat(ca) + dt.timedelta(days=_MIN_VALID_GAP_DAYS))
        except Exception:
            pass
    valid_now = len(_fetch_reviewed())
    need = max(0, _MIN_SAMPLE - valid_now)
    eta = None
    if need and len(dues) >= need:
        # 到期只是"可以被复核了"，真正写进数据库要等回填任务跑。回填挂在
        # advisor.py 的每日 17:30（北京时间，UTC 09:30）那一轮里，所以实际
        # 拿到数据的时间是"到期时刻之后的第一个 09:30 UTC"。不加这一步会
        # 算出一个已经过去的时间，看着像"早该有数据了"，其实还没跑到。
        raw = sorted(dues)[need - 1]
        run = raw.replace(hour=9, minute=30, second=0, microsecond=0)
        if run < raw:
            run += dt.timedelta(days=1)
        eta = run
    return {
        "待复核": len(rows),
        "当前有效样本": valid_now,
        "还需样本": need,
        "预计凑够时间": eta.strftime("%Y-%m-%d %H:%M") if eta else ("已够" if not need else "未知"),
    }


def summary_text() -> str:
    """一段可以直接推给用户/喂给AI的现状说明。"""
    band = score_band_expectancy()
    lines = ["【策略数学期望现状】"]
    if band.get("状态") != "已验证":
        p = pending_review_eta()
        lines.append(f"状态：尚未验证。{band.get('说明', '')}")
        lines.append(
            f"待复核 {p.get('待复核', 0)} 条，当前有效样本 {p.get('当前有效样本', 0)} 条，"
            f"还需 {p.get('还需样本', 0)} 条，预计 {p.get('预计凑够时间')} 凑够。"
        )
        lines.append(
            "在此之前，系统给出的分数和动作只能当筛选线索，"
            "不构成任何可以照着下单的依据。"
        )
        return "\n".join(lines)

    lines.append(f"有效样本 {band['有效样本']} 条（均按 ≥5 天窗口复核）")
    for label, s in band.get("分段", {}).items():
        pl = f"{s['盈亏比']:.2f}" if s.get("盈亏比") else "-"
        lines.append(
            f"  {label:6} 样本{s['样本']:4} 胜率{s['胜率']:5.1f}% "
            f"盈亏比{pl:>5} 期望{s['期望']:+.2f}%"
        )
    act = action_expectancy()
    if act.get("状态") == "已验证":
        lines.append("按动作：")
        for a, s in act.get("分动作", {}).items():
            lines.append(f"  {a:4} 样本{s['样本']:4} 胜率{s['胜率']:5.1f}% 期望{s['期望']:+.2f}%")
    return "\n".join(lines)


if __name__ == "__main__":
    import os
    print(summary_text())
    print()
    print("=== 维度预测力 ===")
    for k, v in dimension_edge().items():
        if v.get("状态") == "已验证":
            print(f"  {k:8} 样本{v['样本']:4} 高分组{v['高分组']:+.2f}% "
                  f"低分组{v['低分组']:+.2f}% 差{v['差值']:+.2f}pct")
        else:
            print(f"  {k:8} {v.get('状态')}（样本 {v.get('样本', 0)}）")
    sys.stdout.flush()
    os._exit(0)
