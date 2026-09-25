"""metrics.py - Stage 4: compute the operational metrics for the trip KPI.

Business KPI: trip duration reliability - which zone-hours run significantly
longer than expected, so fleet ops can rebalance drivers.

Metrics (one function each):
  1. avg_duration_by_zone_hour   - average trip duration by zone x hour-of-day
  2. unreliable_trip_rate        - % of trips exceeding expected duration by >25%
  3. revenue_per_mile_by_borough - total_amount / trip_distance by pickup borough
  4. weather_delay_rate          - unreliable rate on rainy vs dry hours
  5. zero_trip_rate              - zero-distance / zero-fare trip rate (data quality)

The central judgment call - "expected" duration
---------------------------------------------
No source provides ground-truth expected trip durations, so it is DERIVED:

    expected_duration = MEDIAN duration for that zone-pair and hour-of-day

Median (not mean) because it is robust to the very outliers we are trying to
detect: a mean would be inflated by exactly the long trips metric 2 flags.

Sparsity fallback (the reason the fallback chain exists)
-------------------------------------------------------
Profiling 2026-01 showed the zone-pair x hour grid is extremely sparse: 70.5% of
its ~299k cells hold fewer than 5 trips and the median cell holds 2. A benchmark
built from 2 trips is noise, so cells with fewer than MIN_CELL_TRIPS trips fall
back, in order:

    zone-pair x hour  ->  pickup-zone x hour  ->  pickup-borough x hour
                      ->  citywide x hour     ->  citywide (all hours)

Each trip records which level its benchmark came from (`benchmark_level`), so
the fallback is auditable rather than invisible.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
OUTPUT_DIR = PROJECT_ROOT / "output"

logger = logging.getLogger("metrics")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.FileHandler(PROJECT_ROOT / "logs" / "pipeline_run.log"),
        logging.StreamHandler(sys.stdout),
    ],
)

# A cell needs at least this many trips for its median to mean anything. 30 is a
# judgment call: it is the point where a cell's median rests on enough
# observations to be stable, and it keeps 5,814 pickup-zone x hour cells
# (median 59 trips) as the primary fallback tier.
MIN_CELL_TRIPS = 30

# A trip is "unreliable" when it exceeds its expected duration by more than
# 25%. Judgment call: low enough to catch moderate overruns worth acting on,
# high enough to ignore ordinary variance. 50%+ overruns are already obvious in
# metric 1's averages.
DELAY_THRESHOLD = 1.25


def _revenue_per_mile(trips: pd.DataFrame) -> float:
    """Revenue per mile for a group of trips.

    Judgment call: uses `total_amount` (fare + surcharges + tips + tolls), not
    `fare_amount`, because "revenue per mile" should reflect what the rider
    actually paid. A group whose total distance is zero has no defined
    revenue-per-mile, so it yields NaN rather than infinity; zero-distance trips
    are still measured by metric 5.
    """
    distance = trips["trip_distance"].sum()
    if distance <= 0:
        return float("nan")
    return float(trips["total_amount"].sum() / distance)


def attach_expected_duration(trips: pd.DataFrame) -> pd.DataFrame:
    """Attach `expected_duration_min` and `benchmark_level` to every trip.

    Returns a copy; the input is not modified. The same flag column
    (`exceeds_expected`) backs metrics 2 and 4, so the two are definitionally
    identical by construction.
    """
    out = trips.copy()

    # Benchmark tiers, most specific first.
    tiers = [
        ("zone_pair_hour", ["zone_pair", "pickup_hour"]),
        ("pickup_zone_hour", ["PULocationID", "pickup_hour"]),
        ("pickup_borough_hour", ["pickup_borough", "pickup_hour"]),
        ("citywide_hour", ["pickup_hour"]),
    ]
    # Final fallback: the citywide median across all hours, repeated per row.
    citywide_median = out["trip_duration_min"].median()

    expected = pd.Series(np.nan, index=out.index, dtype="float64")
    level = pd.Series(pd.NA, index=out.index, dtype="object")
    unresolved = pd.Series(True, index=out.index)

    for tier_name, keys in tiers:
        if not unresolved.any():
            break
        grouped = out.groupby(keys)["trip_duration_min"]
        medians = grouped.median()
        counts = grouped.size()
        # Only trust a cell with enough observations behind its median.
        trusted = medians[counts >= MIN_CELL_TRIPS]

        if len(trusted) == 0:
            logger.info("[metrics] tier %s had no cell with >= %d trips; skipped",
                        tier_name, MIN_CELL_TRIPS)
            continue

        key_index = (pd.MultiIndex.from_frame(out.loc[unresolved, keys])
                     if len(keys) > 1 else out.loc[unresolved, keys[0]])
        tier_values = trusted.reindex(key_index).to_numpy()
        hit = pd.notna(tier_values)
        target = out.index[unresolved][hit]
        expected.loc[target] = tier_values[hit]
        level.loc[target] = tier_name
        unresolved.loc[target] = False
        logger.info("[metrics] benchmark tier %-20s resolved %s trips (%s open)",
                    tier_name, f"{len(target):,}", f"{int(unresolved.sum()):,}")

    # Final fallback: citywide median, all hours pooled.
    if unresolved.any():
        expected.loc[unresolved] = citywide_median
        level.loc[unresolved] = "citywide_all_hours"
        logger.info("[metrics] benchmark tier %-20s resolved %s trips (0 open)",
                    "citywide_all_hours", f"{int(unresolved.sum()):,}")

    out["expected_duration_min"] = expected
    out["benchmark_level"] = level.astype("object")

    # THE shared delay flag: metrics 2 and 4 both read this exact column.
    out["exceeds_expected"] = (
        out["trip_duration_min"] > DELAY_THRESHOLD * out["expected_duration_min"]
    )
    # Trips behind a given trip's benchmark. When a trip is part of a small cell
    # it is partly compared against itself - a real limitation of a
    # within-month benchmark (see README Bottlenecks).
    out["benchmark_sample_size"] = out.groupby(
        ["zone_pair", "pickup_hour"])["trip_duration_min"].transform("size")

    logger.info("[metrics] delay rate: %.2f%% of %s trips exceed expected by >%.0f%%",
                100 * out["exceeds_expected"].mean(), f"{len(out):,}",
                100 * (DELAY_THRESHOLD - 1))
    logger.info("[metrics] benchmark level mix:\n%s",
                out["benchmark_level"].value_counts().to_string())
    return out


# ---------------------------------------------------------------------------
# Metric 1: average trip duration by zone x hour-of-day
# ---------------------------------------------------------------------------
def avg_duration_by_zone_hour(trips: pd.DataFrame, month: str) -> pd.DataFrame:
    """Metric 1 - the operational 'where and when' view fleet ops acts on.

    Includes the benchmark columns alongside the average so a reader can see
    what 'normal' looks like for that cell, not just the observed mean.
    """
    grouped = trips.groupby(["PULocationID", "pickup_zone", "pickup_hour"], dropna=False)
    result = grouped.agg(
        trips=("trip_duration_min", "size"),
        avg_duration_min=("trip_duration_min", "mean"),
        median_duration_min=("trip_duration_min", "median"),
        expected_duration_min=("expected_duration_min", "median"),
        p90_duration_min=("trip_duration_min", lambda s: s.quantile(0.90)),
    ).reset_index()

    result["delay_rate"] = grouped["exceeds_expected"].mean().to_numpy()
    result["month"] = month
    result["metric"] = "avg_duration_by_zone_hour"

    # Long trips are only comparable within cells of similar size; the flag lets
    # a consumer filter instead of silently trusting a 3-trip average.
    result["low_sample"] = result["trips"] < MIN_CELL_TRIPS

    # Sort decision-grade cells FIRST, then low-sample ones. Sorting on
    # delay_rate alone puts 1-trip cells (which are trivially 0% or 100%) at
    # the top, which is exactly the opposite of useful to a stakeholder
    # scanning for problem zone-hours.
    result = result.sort_values(
        ["low_sample", "delay_rate", "avg_duration_min"],
        ascending=[True, False, False],
    ).reset_index(drop=True)

    decision_grade = result.loc[~result["low_sample"]]
    logger.info("[metrics] metric 1: %s zone-hour rows (%s low-sample, sorted last)",
                f"{len(result):,}", f"{int(result['low_sample'].sum()):,}")
    if len(decision_grade):
        worst = decision_grade.iloc[0]
        logger.info("[metrics] metric 1 worst decision-grade cell: %s at %02d:00 "
                    "(%.1f%% delayed, %s trips, avg %.1f min vs expected %.1f min)",
                    worst["pickup_zone"], int(worst["pickup_hour"]),
                    100 * worst["delay_rate"], f"{int(worst['trips']):,}",
                    worst["avg_duration_min"], worst["expected_duration_min"])
    return result


# ---------------------------------------------------------------------------
# Metric 2: % of trips exceeding expected duration by >25%
# ---------------------------------------------------------------------------
def unreliable_trip_rate(trips: pd.DataFrame, month: str) -> pd.DataFrame:
    """Metric 2 - the core KPI: share of trips overrunning their benchmark.

    Reported alongside the benchmark level mix, because a delay rate built on
    citywide fallbacks is a weaker statement than one built on zone-pair cells.
    """
    grouped = trips.groupby(["PULocationID", "pickup_zone", "pickup_hour"])
    result = grouped.agg(
        trips=("exceeds_expected", "size"),
        unreliable_trips=("exceeds_expected", "sum"),
        avg_duration_min=("trip_duration_min", "mean"),
        expected_duration_min=("expected_duration_min", "median"),
    ).reset_index()

    result["delay_rate"] = result["unreliable_trips"] / result["trips"]
    result["month"] = month
    result["metric"] = "unreliable_trip_rate"

    # Share of this cell's trips whose benchmark came from each tier. Lets a
    # stakeholder discount cells leaning on coarse fallbacks.
    level_mix = (trips.groupby(["PULocationID", "pickup_hour"])["benchmark_level"]
                 .value_counts(normalize=True).unstack(fill_value=0))
    level_mix.columns = [f"benchmark_share_{c}" for c in level_mix.columns]
    result = result.merge(
        level_mix.reset_index(), on=["PULocationID", "pickup_hour"], how="left"
    )

    result["low_sample"] = result["trips"] < MIN_CELL_TRIPS
    # Decision-grade cells first (see the note in avg_duration_by_zone_hour).
    result = result.sort_values(
        ["low_sample", "delay_rate"], ascending=[True, False]
    ).reset_index(drop=True)

    logger.info("[metrics] metric 2: overall unreliable rate %.2f%%",
                100 * trips["exceeds_expected"].mean())
    # Documented interpretation limit: because the benchmark is a median and
    # trip durations are right-skewed, roughly a third of trips exceed 1.25x it
    # BY CONSTRUCTION (verified on 2026-01: 37.1% exceed 1.25x the citywide
    # median). This metric is therefore a RELATIVE ranking between zone-hours,
    # not an absolute share of "bad" trips.
    logger.info("[metrics] metric 2 interpretation: median benchmark + right-skewed "
                "durations means ~1/3 exceed 1.25x it by construction; read as a "
                "RELATIVE zone-hour ranking, not an absolute failure rate.")
    return result


# ---------------------------------------------------------------------------
# Metric 3: revenue per mile by borough
# ---------------------------------------------------------------------------
def revenue_per_mile_by_borough(trips: pd.DataFrame, month: str) -> pd.DataFrame:
    """Metric 3 - is a borough structurally poor value per mile?"""
    rows = []
    for borough, group in trips.groupby("pickup_borough"):
        rows.append({
            "month": month,
            "metric": "revenue_per_mile_by_borough",
            "pickup_borough": borough,
            "trips": len(group),
            "total_revenue": group["total_amount"].sum(),
            "total_distance_mi": group["trip_distance"].sum(),
            "revenue_per_mile": _revenue_per_mile(group),
            "avg_fare": group["fare_amount"].mean(),
            "avg_duration_min": group["trip_duration_min"].mean(),
        })
    result = pd.DataFrame(rows).sort_values(
        "revenue_per_mile", ascending=False
    ).reset_index(drop=True)

    logger.info("[metrics] metric 3: revenue/mile by borough:\n%s",
                result[["pickup_borough", "trips", "revenue_per_mile"]]
                .round(2).to_string(index=False))
    return result


# ---------------------------------------------------------------------------
# Metric 4: weather-adjusted delay rate
# ---------------------------------------------------------------------------
def weather_delay_rate(trips: pd.DataFrame, month: str) -> pd.DataFrame:
    """Metric 4 - are delays weather-driven or operationally actionable?

    Judgment call: trips without a weather observation are EXCLUDED from this
    metric (they cannot be classified as rainy or dry) but the excluded count
    is logged, so the reader knows the denominator.
    """
    usable = trips.loc[trips["weather_available"]]
    excluded = len(trips) - len(usable)

    overall = []
    for condition, mask in [("rainy", usable["is_rainy"]), ("dry", ~usable["is_rainy"])]:
        subset = usable.loc[mask]
        overall.append({
            "month": month,
            "metric": "weather_delay_rate",
            "scope": "citywide",
            "condition": condition,
            "pickup_borough": "ALL",
            "trips": len(subset),
            "delay_rate": subset["exceeds_expected"].mean() if len(subset) else float("nan"),
            "avg_duration_min": subset["trip_duration_min"].mean() if len(subset) else float("nan"),
            "avg_temperature_c": subset["temperature_c"].mean() if len(subset) else float("nan"),
        })

    by_borough = []
    for (borough, is_rainy), group in usable.groupby(["pickup_borough", "is_rainy"]):
        by_borough.append({
            "month": month,
            "metric": "weather_delay_rate",
            "scope": "borough",
            "condition": "rainy" if is_rainy else "dry",
            "pickup_borough": borough,
            "trips": len(group),
            "delay_rate": group["exceeds_expected"].mean(),
            "avg_duration_min": group["trip_duration_min"].mean(),
            "avg_temperature_c": group["temperature_c"].mean(),
        })

    result = pd.concat([pd.DataFrame(overall), pd.DataFrame(by_borough)],
                       ignore_index=True)

    citywide = {r["condition"]: r for r in overall}
    if "rainy" in citywide and "dry" in citywide:
        rainy = citywide["rainy"]["delay_rate"]
        dry = citywide["dry"]["delay_rate"]
        logger.info("[metrics] metric 4: rainy delay rate %.2f%% vs dry %.2f%% "
                    "(difference %+.2f pp, %s trips excluded for no weather)",
                    100 * rainy, 100 * dry, 100 * (rainy - dry), f"{excluded:,}")
    return result


# ---------------------------------------------------------------------------
# Metric 5: zero-distance / zero-fare trip rate (data quality)
# ---------------------------------------------------------------------------
def zero_trip_rate(trips: pd.DataFrame, month: str) -> pd.DataFrame:
    """Metric 5 - bounds how far the other four metrics can be trusted.

    Doubles as a data-quality metric: a rising zero-trip rate would mean the
    other four numbers are resting on a thinner base of real journeys.
    """
    zero_distance = trips["trip_distance"] <= 0
    zero_fare = trips["fare_amount"] <= 0
    both = zero_distance & zero_fare

    rows = [{
        "month": month,
        "metric": "zero_trip_rate",
        "pickup_borough": "ALL",
        "trips": len(trips),
        "zero_distance_trips": int(zero_distance.sum()),
        "zero_fare_trips": int(zero_fare.sum()),
        "zero_distance_and_fare_trips": int(both.sum()),
        "zero_distance_rate": zero_distance.mean(),
        "zero_fare_rate": zero_fare.mean(),
        "passenger_count_missing_rate": trips["passenger_count_missing"].mean(),
        "duplicate_key_rate": trips["is_duplicate_key"].mean(),
    }]

    for borough, group in trips.groupby("pickup_borough"):
        zd = group["trip_distance"] <= 0
        zf = group["fare_amount"] <= 0
        rows.append({
            "month": month,
            "metric": "zero_trip_rate",
            "pickup_borough": borough,
            "trips": len(group),
            "zero_distance_trips": int(zd.sum()),
            "zero_fare_trips": int(zf.sum()),
            "zero_distance_and_fare_trips": int((zd & zf).sum()),
            "zero_distance_rate": zd.mean(),
            "zero_fare_rate": zf.mean(),
            "passenger_count_missing_rate": group["passenger_count_missing"].mean(),
            "duplicate_key_rate": group["is_duplicate_key"].mean(),
        })

    result = pd.DataFrame(rows)
    total = result.iloc[0]
    logger.info("[metrics] metric 5: zero-distance %.2f%% | zero-fare %.2f%% | "
                "missing passenger_count %.2f%%",
                100 * total["zero_distance_rate"], 100 * total["zero_fare_rate"],
                100 * total["passenger_count_missing_rate"])
    return result


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def compute(trips: pd.DataFrame, month: str) -> dict[str, pd.DataFrame]:
    """Compute all five metrics for one month.

    Returns a dict of metric name -> DataFrame. The benchmark and the shared
    `exceeds_expected` flag are attached once, up front, so metrics 2 and 4 are
    guaranteed to agree with each other by construction.
    """
    logger.info("=== METRICS START: %s (%s trips) ===", month, f"{len(trips):,}")
    enriched = attach_expected_duration(trips)

    results = {
        "avg_duration_by_zone_hour": avg_duration_by_zone_hour(enriched, month),
        "unreliable_trip_rate": unreliable_trip_rate(enriched, month),
        "revenue_per_mile_by_borough": revenue_per_mile_by_borough(enriched, month),
        "weather_delay_rate": weather_delay_rate(enriched, month),
        "zero_trip_rate": zero_trip_rate(enriched, month),
    }
    for name, frame in results.items():
        logger.info("[metrics] %-30s -> %s rows", name, f"{len(frame):,}")
    logger.info("=== METRICS END: %s ===", month)
    return results


def save_metrics(month: str, results: dict[str, pd.DataFrame]) -> list[Path]:
    """Write output/metrics_<month>.csv plus one CSV per metric.

    Always overwrites, never appends, so reruns are idempotent (Class 8).
    The combined file is the final evidence table; the per-metric files keep
    the differently-shaped tables readable.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    combined = pd.concat(
        [frame.assign(metric_order=i) for i, frame in enumerate(results.values(), 1)],
        ignore_index=True, sort=False,
    )
    combined_path = OUTPUT_DIR / f"metrics_{month}.csv"
    combined.to_csv(combined_path, index=False)
    written.append(combined_path)

    for name, frame in results.items():
        path = OUTPUT_DIR / f"metrics_{month}_{name}.csv"
        frame.to_csv(path, index=False)
        written.append(path)

    for path in written:
        logger.info("[metrics] wrote %s", path.name)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute the five trip-duration-reliability metrics."
    )
    parser.add_argument("months", nargs="+", help="Months, formatted YYYY-MM.")
    args = parser.parse_args()

    for month in args.months:
        model_path = PROCESSED_DIR / f"{month}_model.parquet"
        if not model_path.exists():
            logger.error("Model not found: %s. Run `python src/model.py %s` first.",
                         model_path, month)
            raise SystemExit(1)
        try:
            trips = pd.read_parquet(model_path)
            results = compute(trips, month)
            save_metrics(month, results)
        except (FileNotFoundError, ValueError) as exc:
            logger.error("Metrics failed for %s: %s", month, exc)
            raise SystemExit(1) from exc


if __name__ == "__main__":
    main()