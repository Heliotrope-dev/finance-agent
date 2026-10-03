"""2026-10-03 review: scoring contract, AI usage cap, judgment reuse, short-reason fallback, lot size."""
from types import SimpleNamespace

import pytest

import scoring

EQUITY = """结论：买入
短线3-5天：站上MA20后量价配合，财报前有催化。
维度打分：
- 基本面 18/22
- 价格位置 14/20
- 技术面 15/20
- 筹码面 12/20
- 分析师预期 6/8
- 数据确定性 8/10
综合得分：99/100
置信度：中"""


def test_score_is_the_sum_of_dimensions_not_the_models_total():
    r = scoring.parse_score(EQUITY, "equity")
    assert r["valid"] and r["score"] == 73


@pytest.mark.parametrize("bad", [
    EQUITY.replace("- 筹码面 12/20\n", ""),                 # missing dimension
    EQUITY.replace("基本面 18/22", "基本面 18/40"),           # wrong denominator
    EQUITY.replace("技术面 15/20", "技术面 25/20"),           # out of range
    EQUITY.replace("技术面 15/20", "技术面 15/20\n- 技术面 10/20"),  # duplicate
])
def test_invalid_breakdowns_never_produce_a_score(bad):
    assert scoring.parse_score(bad, "equity")["score"] is None


def test_invalid_score_downgrades_a_buy_to_watch():
    out = scoring.finalize(EQUITY.replace("- 筹码面 12/20\n", ""), "equity", "买入")
    assert out["action"] == "观望" and out["score"] is None and "评分校验" in out["fundamental_verdict"]
    ok = scoring.finalize(EQUITY, "equity", "买入")
    assert ok["action"] == "买入" and ok["score"] == 73
    assert scoring.parse_score(ok["fundamental_verdict"], "equity")["score"] == 73   # persisted text re-parses


def test_leveraged_products_are_not_scored_as_equities():
    assert scoring.asset_kind("US", "2倍做多ORCL ETF", "US.ORCX") == "leveraged_inverse"
    assert scoring.asset_kind("US", "Apple", "AAPL") == "equity"
    assert scoring.asset_kind("CC", "Bitcoin", "BTC") == "crypto"


def test_usage_cap_blocks_paid_provider_after_limit(tmp_path, monkeypatch):
    import ai_usage
    monkeypatch.setattr(ai_usage, "DB_PATH", tmp_path / "u.db")
    monkeypatch.setitem(ai_usage.DAILY_CALL_CAPS, "DeepSeek", 3)
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    for _ in range(2):
        ai_usage.record("DeepSeek", "m", "judge/AAPL", True, usage)
    assert not ai_usage.over_cap("DeepSeek")
    ai_usage.record("DeepSeek", "m", "debate/MU", False, error="x")       # failures count too: they cost a try
    assert ai_usage.over_cap("DeepSeek")
    assert not ai_usage.over_cap("Gemini-Free")                          # free tier: no cap
    rows = {(r["provider"], r["tag"]): r for r in ai_usage.summary(1)}
    assert rows[("DeepSeek", "judge")]["tokens"] == 30 and rows[("DeepSeek", "debate")]["calls"] == 1


def test_failover_skips_a_capped_provider_without_calling_it(tmp_path, monkeypatch):
    import advisor
    import ai_usage
    monkeypatch.setattr(ai_usage, "DB_PATH", tmp_path / "u.db")
    called = []

    def client(name, text):
        def create(**k):
            called.append(name)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason="stop")],
                                   usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2))
        return lambda: SimpleNamespace(with_options=lambda **k: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(advisor, "_client_free", lambda: None)            # free tier unavailable
    monkeypatch.setattr(advisor, "_deepseek_client", client("DeepSeek", "ok"))
    monkeypatch.setitem(ai_usage.DAILY_CALL_CAPS, "DeepSeek", 1)
    assert advisor.chat_with_failover([{"role": "user", "content": "x"}], max_tokens=10, tag="t") == "ok"
    with pytest.raises(RuntimeError, match="上限"):
        advisor.chat_with_failover([{"role": "user", "content": "x"}], max_tokens=10, tag="t")
    assert called == ["DeepSeek"]


def test_recent_judgment_is_reused_when_price_barely_moved(tmp_path, monkeypatch):
    import advisor
    import tracker
    monkeypatch.setattr(tracker, "_DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(advisor, "_EMAIL", "u@x")
    text = scoring.finalize(EQUITY, "equity", "买入")["fundamental_verdict"]
    rid = tracker.log_advice("u@x", "AAPL", 100.0, text, "tech", "买入", "US", "Apple", source="position")
    assert advisor._reusable("AAPL", "US", "position", 101.5)["id"] == rid
    assert advisor._reusable("AAPL", "US", "position", 103.0) is None        # moved > 2%: judge again
    assert advisor._reusable("AAPL", "US", "watchlist", 100.0)["id"] == rid   # pool may reuse a position call
    rid2 = tracker.log_advice("u@x", "MSFT", 100.0, text, "tech", "买入", "US", "Microsoft", source="watchlist")
    assert advisor._reusable("MSFT", "US", "position", 100.0) is None        # positions only reuse debated calls
    assert tracker.log_advice("u@x", "AAPL", 101.0, text, "tech", "买入", "US", "Apple", reused_from=rid) == rid
    n = tracker._conn().execute("SELECT COUNT(*) FROM advice").fetchone()[0]
    assert n == 2 and rid2                                                   # reuse inserted no duplicate row


def test_short_reason_reads_the_new_format():
    import advisor
    text = scoring.finalize(EQUITY, "equity", "买入")["fundamental_verdict"]
    assert advisor._extract_short_reason(text).startswith("站上MA20后量价配合")
    assert advisor._extract_short_reason("理由：旧格式的理由。后面还有") == "旧格式的理由。"


def test_unknown_hk_lot_size_skips_the_order(monkeypatch):
    import sim_trader
    sim_trader._LOT_SIZE_CACHE.clear()
    import pandas as pd
    qot = SimpleNamespace(get_stock_basicinfo=lambda *a, **k: (-1, pd.DataFrame()))
    assert sim_trader._get_lot_size(qot, "HK.00700") == 0
    assert sim_trader._get_lot_size(qot, "US.AAPL") == 1
    ok = SimpleNamespace(get_stock_basicinfo=lambda *a, **k: (0, pd.DataFrame([{"lot_size": 100}])))
    assert sim_trader._get_lot_size(ok, "HK.00700") == 100


def test_serper_out_of_credits_trips_a_shared_breaker(tmp_path, monkeypatch):
    import io
    import urllib.error
    import web_research as w
    monkeypatch.setattr(w, "_SERPER_OFF", tmp_path / "off")
    monkeypatch.setenv("SERPER_API_KEY", "k")
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise urllib.error.HTTPError("u", 400, "x", {}, io.BytesIO(b'{"message":"Not enough credits"}'))
    monkeypatch.setattr(w.urllib.request, "urlopen", boom)
    assert w._serper("q", 3) == [] and w._serper_disabled()
    assert w._serper("q", 3) == [] and len(calls) == 1                 # second call never hits the network
