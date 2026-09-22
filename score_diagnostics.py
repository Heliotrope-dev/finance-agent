"""Offline diagnostics for score ranking, horizons, and statistical certainty.

SciPy is intentionally imported only here.  This module is not imported by the
Streamlit or FastAPI entry points, so the production web process does not pay
its memory cost on a host with only 1.9G RAM.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy.stats import false_discovery_control, spearmanr

import tracker


DIMENSIONS = ("fundamental", "price_position", "technical", "chips", "analyst", "data_certainty")
HORIZONS = (1, 5, 20, 60)
SCORE_BANDS = ((90, 100, "90-100"), (70, 89, "70-89"), (50, 69, "50-69"), (30, 49, "30-49"), (0, 29, "0-29"))


def _excess_return(row: dict, horizon: int) -> float | None:
    review = row.get(f"review_price_{horizon}d")
    benchmark = row.get(f"bench_ret_{horizon}d")
    base = row.get("price_at_advice")
    if review is None or benchmark is None or not base:
        return None
    return (float(review) / float(base) - 1) * 100 - float(benchmark)


def _dimension_ratio(row: dict, dimension: str) -> float | None:
    value = row.get(f"score_{dimension}")
    maximum = row.get(f"score_{dimension}_max")
    if value is None or maximum in (None, 0):
        return None
    return float(value) / float(maximum)


def _bootstrap_ic(scores: np.ndarray, returns: np.ndarray, *, samples: int, seed: int) -> tuple[float, float, float]:
    rng = np.random.default_rng(seed)
    boot = []
    n = len(scores)
    for _ in range(samples):
        indices = rng.integers(0, n, n)
        ic = spearmanr(scores[indices], returns[indices]).statistic
        if not math.isnan(ic):
            boot.append(float(ic))
    if not boot:
        return math.nan, math.nan, 1.0
    lo, hi = np.percentile(boot, [2.5, 97.5])
    non_positive = sum(value <= 0 for value in boot)
    non_negative = sum(value >= 0 for value in boot)
    p_value = min(1.0, 2 * min(non_positive, non_negative) / len(boot))
    return float(lo), float(hi), max(p_value, 1 / (len(boot) + 1))


def dimension_horizon_stats(rows: list[dict], *, bootstrap_samples: int = 2000, seed: int = 20260922) -> list[dict]:
    """Calculate the 24 hypotheses, then control their false-discovery rate."""
    results = []
    raw_p_values = []
    tested_indexes = []
    for dimension in DIMENSIONS:
        for horizon in HORIZONS:
            pairs = [
                (_dimension_ratio(row, dimension), _excess_return(row, horizon))
                for row in rows
            ]
            pairs = [(score, ret) for score, ret in pairs if score is not None and ret is not None]
            entry = {
                "dimension": dimension, "horizon_days": horizon, "sample_count": len(pairs),
                "ic": None, "ci_low": None, "ci_high": None, "fdr_p": None,
                "verdict": "尚未验证",
            }
            if len(pairs) >= 30:
                scores = np.asarray([pair[0] for pair in pairs], dtype=float)
                returns = np.asarray([pair[1] for pair in pairs], dtype=float)
                ic = spearmanr(scores, returns).statistic
                if not math.isnan(ic):
                    lo, hi, p_value = _bootstrap_ic(
                        scores, returns, samples=bootstrap_samples,
                        seed=seed + len(results),
                    )
                    entry.update(ic=float(ic), ci_low=lo, ci_high=hi, raw_p=p_value)
                    tested_indexes.append(len(results))
                    raw_p_values.append(p_value)
            results.append(entry)

    if raw_p_values:
        adjusted = false_discovery_control(np.asarray(raw_p_values), method="bh")
        for index, fdr_p in zip(tested_indexes, adjusted):
            entry = results[index]
            entry["fdr_p"] = float(fdr_p)
            if fdr_p <= 0.05 and entry["ci_low"] > 0:
                entry["verdict"] = "有信号"
            elif fdr_p <= 0.05 and entry["ci_high"] < 0:
                entry["verdict"] = "反向"
            # Internal p-values are useful for audit, but raw_p is not part of
            # the public report and would invite reading uncorrected tests.
            entry.pop("raw_p", None)
    return results


def daily_cross_sectional_ic(rows: list[dict], horizon: int = 5) -> dict:
    """Measure stock selection within each day/market, removing market timing."""
    groups: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for row in rows:
        excess = _excess_return(row, horizon)
        if row.get("score") is None or excess is None:
            continue
        day = str(row.get("created_at") or "")[:10]
        groups[(day, row.get("market") or "A")].append((float(row["score"]), excess))

    daily = []
    for (day, market), pairs in sorted(groups.items()):
        if len(pairs) < 5:
            continue
        ic = spearmanr([pair[0] for pair in pairs], [pair[1] for pair in pairs]).statistic
        if not math.isnan(ic):
            daily.append({"date": day, "market": market, "ic": float(ic), "n": len(pairs)})
    if not daily:
        return {"count": 0, "ic_mean": None, "ic_std": None, "icir": None, "daily": []}
    values = [item["ic"] for item in daily]
    mean = statistics.mean(values)
    std = statistics.pstdev(values)
    return {
        "count": len(daily), "ic_mean": mean, "ic_std": std,
        "icir": mean / std * math.sqrt(len(values)) if std else None,
        "daily": daily,
    }


def score_band_monotonicity(rows: list[dict], horizon: int) -> dict:
    bands = []
    for low, high, label in SCORE_BANDS:
        returns = [
            excess for row in rows
            if row.get("score") is not None and low <= row["score"] <= high
            for excess in [_excess_return(row, horizon)] if excess is not None
        ]
        bands.append({
            "band": label, "count": len(returns),
            "avg_excess_pct": statistics.mean(returns) if returns else None,
        })
    usable = [band["avg_excess_pct"] for band in bands if band["avg_excess_pct"] is not None]
    monotonic = len(usable) >= 3 and all(usable[index] >= usable[index + 1] for index in range(len(usable) - 1))
    return {"horizon_days": horizon, "monotonic": monotonic, "bands": bands}


def confidence_calibration(rows: list[dict], horizon: int = 5,
                           bootstrap_samples: int = 2000,
                           seed: int = 20260922) -> list[dict]:
    """Check whether high/medium/low confidence separates realized outcomes."""
    out = []
    rng = np.random.default_rng(seed)
    for level in ("高", "中", "低"):
        samples = []
        for row in rows:
            if row.get("confidence") != level or row.get("action") not in ("买入", "卖出"):
                continue
            excess = _excess_return(row, horizon)
            if excess is None:
                continue
            samples.append(excess if row["action"] == "买入" else -excess)
        entry = {
            "level": level, "sample_count": len(samples), "hit_rate_pct": None,
            "avg_signed_excess_pct": None, "hit_rate_ci_low": None,
            "hit_rate_ci_high": None, "verdict": "尚未验证",
        }
        if len(samples) >= 30:
            values = np.asarray(samples, dtype=float)
            boot = [
                float(np.mean(values[rng.integers(0, len(values), len(values))] > 0) * 100)
                for _ in range(bootstrap_samples)
            ]
            lo, hi = np.percentile(boot, [2.5, 97.5])
            entry.update(
                hit_rate_pct=float(np.mean(values > 0) * 100),
                avg_signed_excess_pct=float(np.mean(values)),
                hit_rate_ci_low=float(lo), hit_rate_ci_high=float(hi),
                verdict="已验证",
            )
        out.append(entry)
    return out


def build_report(source: str | None = None, *, bootstrap_samples: int = 2000) -> dict:
    rows = tracker.get_advice_diagnostic_rows(source=source)
    return {
        "source": source or "all", "records": len(rows),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "daily_cross_sectional_ic_5d": daily_cross_sectional_ic(rows, 5),
        "dimension_horizon": dimension_horizon_stats(rows, bootstrap_samples=bootstrap_samples),
        "score_band_monotonicity": [score_band_monotonicity(rows, horizon) for horizon in HORIZONS],
        "confidence_calibration_5d": confidence_calibration(
            rows, bootstrap_samples=bootstrap_samples,
        ),
    }


def public_report(report: dict) -> dict:
    """Hide unstable point estimates whenever the corrected test is inconclusive."""
    safe = json.loads(json.dumps(report))
    for item in safe["dimension_horizon"]:
        if item["verdict"] == "尚未验证":
            item["ic"] = None
            item["ci_low"] = None
            item["ci_high"] = None
    return safe


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=None)
    parser.add_argument("--bootstrap-samples", type=int, default=2000)
    parser.add_argument("--output")
    args = parser.parse_args()
    report = build_report(args.source, bootstrap_samples=max(100, args.bootstrap_samples))
    rendered = json.dumps(public_report(report), ensure_ascii=False, indent=2)
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(rendered + "\n", encoding="utf-8")
        temporary.replace(target)
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
