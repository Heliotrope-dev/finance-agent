"""Backfill advice outcomes from daily bars and matching market benchmarks.

The old review job fetched one current quote per record and preserved only that
single point.  Daily history provides every horizon plus the path in one call,
so it can distinguish a clean gain from a trade that first breached its stop.

Usage:
    python3 backfill_outcomes.py             # preview; no database writes
    python3 backfill_outcomes.py --write     # fill mature, currently NULL cells
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, timedelta, timezone

import pandas as pd

import data_sources as ds
import tracker


logger = logging.getLogger(__name__)
_HORIZONS = (1, 5, 20, 60)


def _column(df: pd.DataFrame, *names: str) -> str | None:
    return next((name for name in names if name in df.columns), None)


def _normalise_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Return date/close/high/low columns without inventing missing values."""
    if df is None or df.empty:
        return pd.DataFrame()
    date_col = _column(df, "日期", "date", "time_key")
    close_col = _column(df, "收盘", "close")
    if not date_col or not close_col:
        return pd.DataFrame()
    high_col = _column(df, "最高", "high")
    low_col = _column(df, "最低", "low")
    out = pd.DataFrame({
        "date": pd.to_datetime(df[date_col], errors="coerce").dt.date,
        "close": pd.to_numeric(df[close_col], errors="coerce"),
    })
    out["high"] = pd.to_numeric(df[high_col], errors="coerce") if high_col else pd.NA
    out["low"] = pd.to_numeric(df[low_col], errors="coerce") if low_col else pd.NA
    return out.dropna(subset=["date", "close"]).sort_values("date").drop_duplicates("date")


def _created_date(value: str) -> date:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).date()


def _benchmark_returns(benchmark: pd.DataFrame, advice_date: date) -> dict:
    """Calculate returns from the last benchmark close on/before advice date."""
    bars = _normalise_bars(benchmark)
    before = bars[bars["date"] <= advice_date]
    after = bars[bars["date"] > advice_date]
    if before.empty:
        return {}
    base = float(before.iloc[-1]["close"])
    if base <= 0:
        return {}
    result = {}
    for horizon in _HORIZONS:
        if len(after) >= horizon:
            result[f"bench_ret_{horizon}d"] = (float(after.iloc[horizon - 1]["close"]) / base - 1) * 100
    return result


def calculate_outcomes(row: dict, history: pd.DataFrame, benchmark: pd.DataFrame) -> dict:
    """Calculate only horizons that have actually matured."""
    base = float(row["price_at_advice"])
    advice_date = _created_date(row["created_at"])
    future = _normalise_bars(history)
    future = future[future["date"] > advice_date]
    values = tracker.extract_advice_metadata(row.get("fundamental_verdict") or "")

    for horizon in _HORIZONS:
        if len(future) >= horizon:
            values[f"review_price_{horizon}d"] = float(future.iloc[horizon - 1]["close"])

    # MFE/MAE describe a complete 20-session path.  Computing them from an
    # immature partial window would silently make recent records incomparable.
    if len(future) >= 20:
        window = future.iloc[:20]
        highs = pd.to_numeric(window["high"], errors="coerce").dropna()
        lows = pd.to_numeric(window["low"], errors="coerce").dropna()
        if not highs.empty:
            values["mfe_pct_20d"] = (float(highs.max()) / base - 1) * 100
        if not lows.empty:
            values["mae_pct_20d"] = (float(lows.min()) / base - 1) * 100
        stop = row.get("stop_loss") or values.get("stop_loss")
        if stop and not lows.empty:
            for day_number, low in enumerate(window["low"], 1):
                if pd.notna(low) and float(low) <= float(stop):
                    values["stop_hit_day"] = day_number
                    break

    values.update(_benchmark_returns(benchmark, advice_date))
    return values


def run(*, write: bool = False, limit: int = 500) -> dict:
    rows = tracker.get_advice_for_outcome_backfill(limit=limit)
    if not rows:
        return {"scanned": 0, "updated_records": 0, "filled_cells": 0, "failed": 0, "write": write}

    today = datetime.now(timezone.utc).date()
    benchmark_by_market: dict[str, pd.DataFrame] = {}
    for market in sorted({row.get("market", "A") for row in rows}):
        if market not in ("A", "HK", "US"):
            benchmark_by_market[market] = pd.DataFrame()
            continue
        oldest = min(_created_date(row["created_at"]) for row in rows if row.get("market", "A") == market)
        benchmark_by_market[market] = ds.get_benchmark_history(
            (oldest - timedelta(days=10)).isoformat(), today.isoformat(), market=market
        )

    updated_records = 0
    filled_cells = 0
    failed = 0
    for row in rows:
        try:
            advice_date = _created_date(row["created_at"])
            history = ds.get_stock_history(
                row["symbol"], advice_date.isoformat(), today.isoformat(), market=row.get("market", "A")
            )
            values = calculate_outcomes(
                row, history, benchmark_by_market.get(row.get("market", "A"), pd.DataFrame())
            )
            if not values:
                continue
            changed = tracker.record_advice_outcomes(row["id"], values) if write else len(
                [key for key, value in values.items() if value is not None and row.get(key) is None]
            )
            if changed:
                updated_records += 1
                filled_cells += changed
        except Exception as exc:
            # One unavailable symbol must not prevent the rest of the nightly
            # batch from being filled.  Keep the field NULL and retry tomorrow.
            failed += 1
            logger.warning("outcome backfill skipped id=%s symbol=%s error=%s", row["id"], row["symbol"], exc)

    return {
        "scanned": len(rows), "updated_records": updated_records,
        "filled_cells": filled_cells, "failed": failed, "write": write,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="write mature values to SQLite")
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run(write=args.write, limit=max(1, args.limit))
    logger.info(
        "outcome backfill scanned=%d updated=%d cells=%d failed=%d mode=%s",
        result["scanned"], result["updated_records"], result["filled_cells"],
        result["failed"], "write" if result["write"] else "preview",
    )
    return 1 if result["failed"] and result["updated_records"] == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
