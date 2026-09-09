import daily_plan


def _item(**changes):
    base = {
        "方向": "买入",
        "新开仓状态": "可执行",
        "建议股数": 100,
        "盈亏比": 3.0,
        "评分": 70,
    }
    base.update(changes)
    return base


def test_only_order_ready_buy_can_be_recommended():
    assert daily_plan._is_executable_new_buy(_item(), 3.0) is True
    assert daily_plan._is_executable_new_buy(_item(方向="观望"), 3.0) is False
    assert daily_plan._is_executable_new_buy(_item(建议股数=None), 3.0) is False
    assert daily_plan._is_executable_new_buy(_item(盈亏比=2.99), 3.0) is False


def test_ranking_keeps_watch_candidates_out_of_top_group():
    executable = _item(评分=70, 盈亏比=3.0)
    watch_only = _item(方向="观望", 评分=99, 盈亏比=5.0)
    ranked = daily_plan._rank_candidates([watch_only, executable], 3.0)
    assert ranked[0] is executable
