"""把历史 advice 记录的六个维度分从判断原文里重新解析出来、补进独立列。

为什么需要这个脚本：维度分是 log_advice 写库那一刻用
tracker.extract_score_breakdown 从 fundamental_verdict 里现解析的，解析器
一旦有缺陷，那一刻起写进去的就全是 NULL，而且总分照常落库、页面完全看不
出异常。这类失败已经发生过两次：

  2026-09-05  打分从四维扩到六维、分母改了，正则里的分母还写死着旧数字，
              六列全部落空（1749条）。
  2026-09-06  解析窗口写死在"维度打分"那一行以内，模型换行写就取不到，
              当天642条观察池判断里390条落空。

原文一直好好存在 fundamental_verdict 里，所以每次修完解析器都能重新跑一遍
把历史补回来——维度分是 get_dimension_predictive_value（回答"六个维度里
到底哪个真有预测力"）唯一的输入，缺一段就等于那段时间的判断白记了。

用法：
    python3 backfill_dimensions.py            # 只报告会改多少条，不写库
    python3 backfill_dimensions.py --write    # 真正写库
"""

import sqlite3
import sys
from contextlib import closing

import tracker

_COLUMNS = (
    ("score_fundamental", "fundamental"),
    ("score_price_position", "price_position"),
    ("score_technical", "technical"),
    ("score_chips", "chips"),
    ("score_analyst", "analyst"),
    ("score_data_certainty", "data_certainty"),
)


def backfill(write: bool = False) -> dict:
    """重新解析所有"原文里有维度打分、但至少有一列是空"的记录。

    只补空列，不覆盖已有值——已经解析对的没有理由重算，万一新解析器在某种
    写法上更差，也不会把好数据冲掉。
    """
    tracker.init_db()
    with closing(tracker._conn()) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT id, fundamental_verdict, "
            + ", ".join(col for col, _ in _COLUMNS)
            + " FROM advice WHERE fundamental_verdict LIKE '%维度打分%'"
        ).fetchall()

        scanned = len(rows)
        updates, filled_cells = [], 0
        for r in rows:
            breakdown = tracker.extract_score_breakdown(r["fundamental_verdict"] or "")
            sets, vals = [], []
            for col, key in _COLUMNS:
                if r[col] is None and breakdown[key] is not None:
                    sets.append(f"{col} = ?")
                    vals.append(breakdown[key])
            if sets:
                updates.append((sets, vals, r["id"]))
                filled_cells += len(sets)

        if write:
            for sets, vals, rid in updates:
                c.execute(f"UPDATE advice SET {', '.join(sets)} WHERE id = ?", vals + [rid])
            c.commit()

    return {"扫描": scanned, "待补记录": len(updates), "待补单元格": filled_cells,
            "已写库": write}


if __name__ == "__main__":
    result = backfill(write="--write" in sys.argv)
    print(f"扫描含维度打分的记录 {result['扫描']} 条")
    print(f"其中有空列可补的 {result['待补记录']} 条，共 {result['待补单元格']} 个单元格")
    if not result["已写库"]:
        print("（这是预演，没有写库。加 --write 真正执行）")
    else:
        print("已写库。")
