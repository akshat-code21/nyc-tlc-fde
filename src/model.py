"""model.py - Stage 3: build the trip/zone/time event model for one month.

Entity/event model (Class 7):
  Entity  Trip    - one row per completed yellow taxi trip (the event:
                    pickup -> dropoff)
  Entity  Zone    - TLC taxi zone dimension (LocationID -> Borough / Zone)
  Context Weather - hourly Open-Meteo observations joined on the LOCAL pickup hour

The model is a flat event table (one row per trip), which the assignment
explicitly allows. Everything metrics.py needs is derived here once, so the
metric definitions stay consistent with each other.

Judgment calls (all mirrored in the README Assumptions section):
  - `zone_pair` is directional (PU->DO): JFK->Manhattan is not Manhattan->JFK.
  - Weather is joined on the local `pickup_hour` rather than separately on trip
    start and end: the rainy/dry split is a trip-level attribute and a trip can
    span an hour boundary. Using pickup hour keeps the join a simple hour lookup
    and matches the "conditions at pickup" framing of the KPI.
  - Trips whose pickup hour has no weather row are flagged, not dropped.

Usage:
    python src/model.py 2026-01
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
RAW_DIR = PROJECT_ROOT / "data" / "raw"

logger = logging.getLogger("model")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.FileHandler(PROJECT_ROOT / "logs" / "pipeline_run.log"),
        logging.StreamHandler(sys.stdout),
    ],
)

# Columns carried forward from validation. Keeping the model narrow (rather than
# all 20 raw columns) makes the event table self-documenting and keeps the
# parquet small.
TRIP_COLUMNS = [
    "tpep_pickup_datetime",
    "tpep_dropoff_datetime",
    "PULocationID",
    "DOLocationID",
    "trip_distance",
    "fare_amount",
    "total_amount",
    "tip_amount",
    "tolls_amount",
    "congestion_surcharge",
    "passenger_count",
    "passenger_count_missing",
    "is_duplicate_key",
]


def build_event_model(clean: pd.DataFrame, zones: pd.DataFrame,
                      weather: pd.DataFrame) -> pd.DataFrame:
    """Join trips to the zone dimension and hourly weather, and derive the
    event-level fields the metrics are defined on."""
    logger.info("=== MODEL START: building event model from %s clean rows ===",
                f"{len(clean):,}")

    missing = [c for c in TRIP_COLUMNS if c not in clean.columns]
    if missing:
        raise ValueError(
            f"clean data is missing expected columns: {missing}. "
            "Run validate.py before model.py."
        )

    trips = clean[TRIP_COLUMNS].copy()

    # --- Event timing -----------------------------------------------------
    # Duration is recomputed here (not read from validate) so the event model
    # owns its derived fields and the two stages stay independent.
    trips["trip_duration_min"] = (
        trips["tpep_dropoff_datetime"] - trips["tpep_pickup_datetime"]
    ).dt.total_seconds() / 60.0

    # Local pickup date/hour. TLC timestamps are already local New York time,
    # and so is the weather file (see ingest.fetch_weather), so these are
    # directly comparable join keys.
    trips["pickup_date"] = trips["tpep_pickup_datetime"].dt.date
    trips["pickup_hour"] = trips["tpep_pickup_datetime"].dt.hour
    trips["pickup_weekday"] = trips["tpep_pickup_datetime"].dt.dayofweek

    # --- Zone dimension ---------------------------------------------------
    # Rename to avoid a suffixed _x/_y pair after the merge.
    zone_dim = zones.rename(columns={
        "LocationID": "PULocationID",
        "Borough": "pickup_borough",
        "Zone": "pickup_zone",
    })[["PULocationID", "pickup_borough", "pickup_zone"]]

    dropoff_dim = zones.rename(columns={
        "LocationID": "DOLocationID",
        "Borough": "dropoff_borough",
        "Zone": "dropoff_zone",
    })[["DOLocationID", "dropoff_borough", "dropoff_zone"]]

    trips = trips.merge(zone_dim, on="PULocationID", how="left", validate="many_to_one")
    trips = trips.merge(dropoff_dim, on="DOLocationID", how="left", validate="many_to_one")

    # Join-integrity check. Note: this must test the LocationID, NOT the
    # borough label. Lookup rows 264 ("Unknown") and 265 ("Outside of NYC")
    # have a NaN Borough/Zone by design, so a label-based test would report
    # false "unmatched" rows even though the join succeeded. Validated data
    # contains only IDs present in the lookup, so this should be 0.
    unmatched = int((~trips["PULocationID"].isin(zone_dim["PULocationID"])).sum()
                    + (~trips["DOLocationID"].isin(dropoff_dim["DOLocationID"])).sum())
    if unmatched:
        logger.warning("[model] %s zone-lookup joins did not match a LocationID",
                       f"{unmatched:,}")
    # Rows whose zone label is genuinely blank (IDs 264/265) are labelled
    # "Unknown" rather than left null, so downstream group-bys never produce
    # NaN keys. This is a label, not a dropped trip.
    trips["pickup_borough"] = trips["pickup_borough"].fillna("Unknown")
    trips["dropoff_borough"] = trips["dropoff_borough"].fillna("Unknown")
    trips["pickup_zone"] = trips["pickup_zone"].fillna("Unknown")
    trips["dropoff_zone"] = trips["dropoff_zone"].fillna("Unknown")

    # Directional zone-pair key. Kept as a string so it is readable in the
    # output tables and stable across runs.
    trips["zone_pair"] = (
        trips["PULocationID"].astype(str) + "->" + trips["DOLocationID"].astype(str)
    )

    # --- Weather context --------------------------------------------------
    wx = weather.rename(columns={
        "time": "weather_time",
        "temperature_2m": "temperature_c",
        "precipitation": "precipitation_mm",
    })[["weather_time", "temperature_c", "precipitation_mm"]].copy()
    wx["weather_time"] = pd.to_datetime(wx["weather_time"])

    # Floor the pickup timestamp to the hour to build the join key. Both sides
    # are local New York time (verified in ingest.py), so no conversion happens.
    trips["pickup_hour_ts"] = trips["tpep_pickup_datetime"].dt.floor("h")
    trips = trips.merge(wx, left_on="pickup_hour_ts", right_on="weather_time",
                        how="left", validate="many_to_one")

    no_weather = int(trips["weather_time"].isna().sum())
    if no_weather:
        # Flagged, never dropped: the trip is real, only its weather context
        # is unknown. metric 4 excludes these from the rainy/dry split.
        logger.warning("[model] %s trips have no weather row for their pickup hour",
                       f"{no_weather:,}")
    trips["weather_available"] = trips["weather_time"].notna()

    # Binary rain flag used by metric 4. "Rainy" = any measurable precipitation
    # at the pickup hour. Judgment call: binary rather than intensity-banded -
    # simple, explainable, and easy to refine later.
    trips["is_rainy"] = trips["precipitation_mm"].fillna(0) > 0

    trips = trips.drop(columns=["weather_time", "pickup_hour_ts"])

    logger.info("[model] event model built: %s rows x %s cols",
                f"{len(trips):,}", len(trips.columns))
    logger.info("[model] median trip duration: %.1f min | rainy share: %.1f%%",
                trips["trip_duration_min"].median(), 100 * trips["is_rainy"].mean())
    logger.info("=== MODEL END ===")
    return trips


def save_model(month: str, model: pd.DataFrame) -> Path:
    """Persist the event model. Overwrites, so reruns are idempotent."""
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    out = PROCESSED_DIR / f"{month}_model.parquet"
    model.to_parquet(out, index=False)
    logger.info("[model] wrote %s (%s rows)", out.name, f"{len(model):,}")
    return out


def build_from_disk(month: str) -> pd.DataFrame:
    """Load the stage artifacts for a month and build the event model."""
    clean_path = PROCESSED_DIR / f"{month}_clean.parquet"
    if not clean_path.exists():
        raise FileNotFoundError(
            f"{clean_path} not found. Run `python src/validate.py {month}` first."
        )
    weather_path = RAW_DIR / f"weather_{month}.csv"
    if not weather_path.exists():
        raise FileNotFoundError(
            f"{weather_path} not found. Run `python src/ingest.py {month}` first."
        )

    clean = pd.read_parquet(clean_path)
    zones = pd.read_csv(RAW_DIR / "taxi_zone_lookup.csv")
    weather = pd.read_csv(weather_path)
    return build_event_model(clean, zones, weather)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the trip/zone/weather event model for one or more months."
    )
    parser.add_argument("months", nargs="+", help="Months, formatted YYYY-MM.")
    args = parser.parse_args()

    for month in args.months:
        try:
            model = build_from_disk(month)
            save_model(month, model)
        except (FileNotFoundError, ValueError) as exc:
            logger.error("Model build failed for %s: %s", month, exc)
            raise SystemExit(1) from exc


if __name__ == "__main__":
    main()