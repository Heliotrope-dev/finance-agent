from __future__ import annotations

import pandas as pd

import api


def setup_function():
    api._cache.clear()


def test_macro_calendar_and_ipo_are_read_only_cached_views(monkeypatch):
    monkeypatch.setattr(api.tracker, "get_latest_macro_briefs", lambda: [{"id": 1}])
    monkeypatch.setattr(
        api.ds, "get_ipo_calendar",
        lambda market, limit=12: [{"symbol": f"{market}1", "list_date": "2026-10-01"}],
    )
    monkeypatch.setattr(
        api.tracker, "get_latest_ipo_briefs",
        lambda limit=12, market="HK": [{"symbol": f"{market}1", "brief_text": "done"}],
    )

    assert api.macro("user@example.com")["items"] == [{"id": 1}]
    assert {item["market"] for item in api.calendar("user@example.com")["items"]} == {"A", "HK", "US"}
    assert api.ipo("user@example.com")["markets"]["HK"]["briefs"][0]["brief_text"] == "done"


def test_quote_kline_and_analysis_shapes(monkeypatch):
    monkeypatch.setattr(api.ds, "get_stock_realtime", lambda symbol, market: {"最新价": 123.4})
    monkeypatch.setattr(
        api.ds, "get_stock_history",
        lambda *args, **kwargs: pd.DataFrame([{
            "日期": "2026-09-21", "开盘": 120, "最高": 125,
            "最低": 119, "收盘": 123.4, "成交量": 1000,
        }]),
    )
    monkeypatch.setattr(
        api.tracker, "get_watchlist_verdict_for_symbol",
        lambda symbol: {"in_pool": True, "verdict_excerpt": "test"},
    )

    assert api.quote("US", "AAPL", "user@example.com")["quote"]["最新价"] == 123.4
    assert api.kline("US", "AAPL", 180, "user@example.com")["items"][0]["close"] == 123.4
    assert api.stock_analysis("US", "AAPL", "user@example.com")["analysis"]["in_pool"] is True
