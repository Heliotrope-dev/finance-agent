"""Keep unsupported Futu contracts out of the stock quote batch."""


def test_monitored_excludes_futures_without_excluding_stock_main(monkeypatch):
    import sim_watch

    monkeypatch.setattr(sim_watch.tracker, "get_simulated_orders", lambda *a, **k: [
        {"symbol": "BZmain", "market": "US", "status": "成功"},
        {"symbol": "AAPL", "market": "US", "status": "成功"},
    ])
    monkeypatch.setattr(sim_watch.tracker, "get_positions", lambda *a: [
        {"symbol": "MGCmain", "market": "US", "shares": 0},
        {"symbol": "MAIN", "market": "US", "shares": 0},
        {"symbol": "AAPL", "market": "US", "shares": 1},
        {"symbol": "00700", "market": "HK", "shares": 1},
    ])

    assert set(sim_watch._monitored("user@example.com", ["US"])) == {
        ("AAPL", "US", True),
        ("MAIN", "US", False),
    }
