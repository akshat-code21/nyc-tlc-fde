"""validate.py - Stage 2: profile the raw month and split it into clean/rejected.

Outputs (data/raw/ is never modified):
  data/processed/<month>_clean.parquet  - rows passing every business rule
  data/rejected/<month>_rejected.csv   - every failed row + a rejection_reason column

Design (per assignment, Class 6 "Profile & validate"):
  - profile() logs nulls, dtypes, min/max, duplicates and outliers: the evidence
    behind the rule thresholds below.
  - run() applies explicit, named business rules. A row failing several rules is
    rejected once with ALL reasons joined by ';' - nothing is silently dropped.
  - Every judgment call is a named constant at the top with the observed
    evidence that motivated it, mirrored in the README Assumptions section.

Usage:
    python src/validate.py 2026-01
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

try:  # package import (when pipeline.py does `from src import validate`)
    from . import ingest
except ImportError:  # direct script run (python src/validate.py)
    import ingest

logger = logging.getLogger("validate")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
REJECTED_DIR = PROJECT_ROOT / "data" / "rejected"

# ---------------------------------------------------------------------------
# Business rules and judgment calls (each value below is backed by profiling
# evidence logged by profile(); see notebooks/exploration.ipynb)
# ---------------------------------------------------------------------------

# Assignment rule: trip duration > 0 and < 4 hours. We tighten the lower bound
# to 1 minute on purpose. Evidence (2026-01): 45,069 rows have pickup == dropoff
# to the second - 43,905 of them with trip_distance > 0, i.e. timestamp
# artifacts rather than real trips - plus 31,679 rows lasting 1-30 seconds.
# These would otherwise drag the duration KPI. One constant to tune if a
# stakeholder disagrees with the 1-minute floor.
MIN_DURATION_MIN = 1.0
MAX_DURATION_MIN = 240.0

# Assignment rule: fare_amount >= 0. fare == 0 is KEPT on purpose: the
# zero-fare rate is metric 5, so those rows are evidence, not errors.
MIN_FARE_AMOUNT = 0.0

# Extra rule beyond the assignment list: a NYC yellow trip over 100 miles is a
# data-entry error, not a trip. Evidence: 162-172 rows/month exceed 100 miles,
# with maxima of 269k-328k miles. They would also wreck revenue-per-mile
# (metric 3), so this rule is about metric validity, not tidiness.
MAX_TRIP_DISTANCE_MI = 100.0

# Assignment rule: passenger count between 1 and 6 - but enforced only on
# POPULATED values. Evidence: 2026-01 has 1,088,058 rows (29.2%) with null
# passenger_count, a contiguous tail block of the monthly file where
# RatecodeID, fees and store_and_fwd_flag are also null and payment_type == 0:
# a partial upstream feed. Those rows carry plausible durations (median 16 min)
# and fares ($22 median), so they are real trips with missing metadata. No
# metric uses passenger_count, so rejecting them would discard ~29% of the
# data for nothing. Populated but out-of-range values (0, or 7-9: ~14.8k
# rows/month) ARE rejected.
MIN_PASSENGERS, MAX_PASSENGERS = 1, 6

# Pickup must fall inside the month +/- 1 day. TLC files legitimately contain a
# few boundary-spillover trips (e.g. 2026-01-31 23:57), but rows such as
# 2008-12-31 (found in the 2026-03 file) are misfiled records, not trips.
MONTH_WINDOW_PAD_DAYS = 1

# basicConfig is a no-op if a handler already exists (ingest.py configures the
# same handlers on import), so importing both modules never double-logs.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.FileHandler(PROJECT_ROOT / "logs" / "pipeline_run.log"),
        logging.StreamHandler(sys.stdout),
    ],
)


# ---------------------------------------------------------------------------
# Profiling
# ---------------------------------------------------------------------------
def month_bounds(month: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    """(first day, last day) of a 'YYYY-MM' string."""
    dt = datetime.strptime(month, "%Y-%m")
    first = pd.Timestamp(year=dt.year, month=dt.month, day=1)
    return first, first + pd.offsets.MonthEnd(0)


def duration_minutes(df: pd.DataFrame) -> pd.Series:
    """Trip duration in minutes. Computed here to evaluate the rules; model.py
    derives its own copy when building the event model, so stage boundaries
    stay clean."""
    delta = df["tpep_dropoff_datetime"] - df["tpep_pickup_datetime"]
    return delta.dt.total_seconds() / 60.0


def profile(df: pd.DataFrame) -> dict:
    """Log the profiling evidence (nulls, dtypes, min/max, duplicates,
    outliers) that the business rules are based on. Class 6 requirement."""
    logger.info("[profile] shape: %s rows x %s cols", f"{len(df):,}", len(df.columns))
    logger.info("[profile] dtypes:\n%s", df.dtypes.to_string())

    nulls = df.isna().sum()
    nonzero = nulls[nulls > 0]
    logger.info("[profile] null counts (non-zero only):\n%s",
                nonzero.to_string() if not nonzero.empty else "none")

    for name, series in [
        ("duration_min", duration_minutes(df)),
        ("fare_amount", df["fare_amount"]),
        ("trip_distance", df["trip_distance"]),
        ("passenger_count", df["passenger_count"]),
    ]:
        logger.info("[profile] %s quantiles:\n%s", name,
                    series.quantile([0, 0.01, 0.5, 0.99, 1]).round(2).to_string())

    dup_key = ["tpep_pickup_datetime", "tpep_dropoff_datetime",
               "PULocationID", "DOLocationID"]
    dup_full = int(df.duplicated().sum())
    dup_on_key = int(df.duplicated(subset=dup_key).sum())
    logger.info("[profile] duplicates: %d full-row, %d on (pickup, dropoff, PU, DO)",
                dup_full, dup_on_key)
    logger.info("[profile] pickup range: %s .. %s",
                df["tpep_pickup_datetime"].min(), df["tpep_pickup_datetime"].max())

    return {
        "rows": len(df),
        "cols": len(df.columns),
        "nulls": nonzero.to_dict(),
        "dup_full": dup_full,
        "dup_on_key": dup_on_key,
    }


# ---------------------------------------------------------------------------
# Business rules
# ---------------------------------------------------------------------------
def build_rule_masks(df: pd.DataFrame, month: str, zone_ids: set) -> dict[str, pd.Series]:
    """One boolean mask per named business rule; violation counts are logged so
    the rejection log is fully auditable."""
    first, last = month_bounds(month)
    pad = pd.Timedelta(days=MONTH_WINDOW_PAD_DAYS)
    dur = duration_minutes(df)
    pc = df["passenger_count"]

    masks = {
        "invalid_duration":
            (dur < MIN_DURATION_MIN) | (dur > MAX_DURATION_MIN),
        "negative_fare":
            df["fare_amount"] < MIN_FARE_AMOUNT,
        "implausible_distance":
            df["trip_distance"] > MAX_TRIP_DISTANCE_MI,
        "unknown_pickup_zone":
            ~df["PULocationID"].isin(zone_ids),
        "unknown_dropoff_zone":
            ~df["DOLocationID"].isin(zone_ids),
        # NaN passenger_count means missing, not invalid - see the constant.
        "invalid_passenger_count":
            pc.notna() & ((pc < MIN_PASSENGERS) | (pc > MAX_PASSENGERS)),
        "pickup_outside_month":
            (df["tpep_pickup_datetime"] < first - pad)
            | (df["tpep_pickup_datetime"] > last + pad),
    }
    for name, mask in masks.items():
        logger.info("[rules] %-24s violations: %s", name, f"{int(mask.sum()):,}")
    return masks


def run(month: str, raw_paths: dict[str, Path]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Profile the raw month, apply all rules, return (clean, rejected)."""
    logger.info("=== VALIDATE START: %s ===", month)

    df = pd.read_parquet(raw_paths["trips"])
    profile(df)

    zone_ids = set(pd.read_csv(raw_paths["zones"])["LocationID"])
    masks = build_rule_masks(df, month, zone_ids)

    # A row is rejected if it fails ANY rule, and carries every reason it failed.
    any_bad = np.logical_or.reduce([m.to_numpy() for m in masks.values()])
    bad_positions = np.flatnonzero(any_bad)

    if bad_positions.size:
        # Reasons are materialized only for rejected rows (~2-3% of the month),
        # so this stays fast even on 4M-row files.
        reason_parts = pd.DataFrame({
            name: np.where(m.to_numpy()[bad_positions], name, "")
            for name, m in masks.items()
        })
        reasons_bad = reason_parts.apply(
            lambda row: ";".join([v for v in row if v]), axis=1
        )
    else:
        reasons_bad = pd.Series([], dtype=object)

    good_positions = np.flatnonzero(~any_bad)

    dup_key = ["tpep_pickup_datetime", "tpep_dropoff_datetime",
               "PULocationID", "DOLocationID"]
    # Kept, not dropped: two genuine trips can share a timestamp + location key
    # (the raw files have no full-row duplicates at all), so deduplicating on
    # this key risks deleting real trips. Flagged for the data-quality view.
    flags = pd.DataFrame({
        "passenger_count_missing": df["passenger_count"].isna().to_numpy(),
        "is_duplicate_key": df.duplicated(subset=dup_key, keep=False).to_numpy(),
    })

    clean = df.iloc[good_positions].copy()
    clean[flags.columns] = flags.iloc[good_positions]
    clean.reset_index(drop=True, inplace=True)

    rejected = df.iloc[bad_positions].copy()
    rejected[flags.columns] = flags.iloc[bad_positions]
    rejected["rejection_reason"] = reasons_bad.to_numpy()
    rejected.reset_index(drop=True, inplace=True)

    pct = 100.0 * len(rejected) / max(len(df), 1)
    logger.info("[validate] %s: %s valid, %s rejected (%.2f%%)",
                month, f"{len(clean):,}", f"{len(rejected):,}", pct)
    if len(rejected):
        reason_counts = (rejected["rejection_reason"].str.split(";")
                         .explode().value_counts())
        logger.info("[validate] rejection reasons:\n%s", reason_counts.to_string())

    logger.info("=== VALIDATE END: %s ===", month)
    return clean, rejected


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def save_outputs(month: str, clean: pd.DataFrame, rejected: pd.DataFrame) -> tuple[Path, Path]:
    """Write clean/rejected artifacts. Always overwrites (never appends), so
    reruns are idempotent - Class 8 requirement."""
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    REJECTED_DIR.mkdir(parents=True, exist_ok=True)

    clean_path = PROCESSED_DIR / f"{month}_clean.parquet"
    rejected_path = REJECTED_DIR / f"{month}_rejected.csv"

    clean.to_parquet(clean_path, index=False)
    rejected.to_csv(rejected_path, index=False)
    logger.info("[validate] wrote %s (%s rows) and %s (%s rows)",
                clean_path.name, f"{len(clean):,}",
                rejected_path.name, f"{len(rejected):,}")
    return clean_path, rejected_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Profile and validate one or more months of raw TLC trips."
    )
    parser.add_argument("months", nargs="+", help="Months, formatted YYYY-MM.")
    args = parser.parse_args()

    for month in args.months:
        try:
            # Idempotent: ensures the raw sources exist (skips downloads).
            raw_paths = ingest.fetch_all(month)
            clean, rejected = run(month, raw_paths)
            save_outputs(month, clean, rejected)
        except (ingest.SourceMissingError, ValueError) as exc:
            logger.error("Validation failed for %s: %s", month, exc)
            raise SystemExit(1) from exc


if __name__ == "__main__":
    main()