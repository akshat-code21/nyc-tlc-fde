"""ingest.py - pull all 3 raw sources into data/raw/, unmodified.

Sources (retrieval modes):
  1. TLC Yellow Taxi trip records  -> bulk Parquet download per month
  2. TLC Taxi Zone Lookup Table    -> CSV file download
  3. Open-Meteo historical weather -> REST API, no key required

Design principles (per assignment, Class 5):
  - Idempotent: if a raw file already exists and passes a non-empty check, the
    download is skipped. Re-running never duplicates or re-downloads unnecessarily.
  - Raw files are never edited after fetch. Validation happens in validate.py,
    which writes to data/processed/ and data/rejected/, never back into raw/.
  - Completeness checks are run after every download and logged: this is the
    "prove retrieval was complete" evidence.

Usage:
    python src/ingest.py 2026-01
    python src/ingest.py 2026-01 2026-02 2026-03
"""

from __future__ import annotations

import calendar
import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

# ---------------------------------------------------------------------------
# Paths (project root = parent of src/, so this works from any cwd)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
LOG_DIR = PROJECT_ROOT / "logs"

RAW_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Logging: console + file. pipeline.py will reuse this logger.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "pipeline_run.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("ingest")


class SourceMissingError(Exception):
    """Raised when a required source file cannot be downloaded or is corrupt.

    pipeline.py catches this explicitly and halts the run with a clear message
    instead of crashing uninformatively or silently producing bad output.
    """


# ---------------------------------------------------------------------------
# Configuration / constants
# ---------------------------------------------------------------------------

# TLC hosts monthly files on CloudFront with a stable URL pattern.
# Judgment call: we hardcode the pattern rather than scraping the TLC page,
# because the page layout is not part of the data contract and scraping adds
# a fragile dependency. If TLC changes the pattern, this one constant changes.
TLC_PARQUET_URL = (
    "https://d37ci6vzurychx.cloudfront.net/trip-data/"
    "yellow_tripdata_{month}.parquet"
)

TLC_ZONE_LOOKUP_URL = (
    "https://d37ci6vzurychx.cloudfront.net/misc/taxi_zone_lookup.csv"
)

# Open-Meteo historical (archive) API - no key required.
# Judgment call: NYC coordinates are city-wide on purpose. The README documents
# that weather is city-wide, not route-specific (a known limitation, not a bug).
OPEN_METEO_URL = "https://archive-api.open-meteo.com/v1/archive"
NYC_LAT, NYC_LON = 40.7128, -74.0060
# Columns the downstream pipeline cannot work without. We check for this SUBSET
# only, not the full schema: TLC occasionally adds fee columns (e.g. airport_fee,
# congestion_surcharge) across years, and requiring the full schema would make
# ingest brittle for no downstream benefit.
REQUIRED_TRIP_COLUMNS = [
    "tpep_pickup_datetime",
    "tpep_dropoff_datetime",
    "PULocationID",
    "DOLocationID",
    "trip_distance",
    "fare_amount",
    "passenger_count",
]

REQUIRED_ZONE_COLUMNS = ["LocationID", "Borough", "Zone", "service_zone"]

# Completeness bounds for monthly trip counts.
# Judgment call: recent Yellow months have run ~1-3M rows; we use a generous
# 500k-5M band. The point of the check is to catch truncated/empty downloads,
# not to pin an exact forecast. Verified against actual files during first run.
MIN_TRIP_ROWS, MAX_TRIP_ROWS = 500_000, 5_000_000

MONTH_FORMAT = "%Y-%m"


def _validate_month(month: str) -> str:
    """Normalize and validate a 'YYYY-MM' month string."""
    try:
        return datetime.strptime(month, MONTH_FORMAT).strftime(MONTH_FORMAT)
    except ValueError:
        raise ValueError(
            f"Invalid month '{month}': expected format 'YYYY-MM' (e.g. 2026-01)"
        )


def _month_bounds(month: str) -> tuple[str, str]:
    """First and last calendar day of the month as ISO date strings."""
    year, mon = int(month[:4]), int(month[5:7])
    last_day = calendar.monthrange(year, mon)[1]
    return f"{year}-{mon:02d}-01", f"{year}-{mon:02d}-{last_day:02d}"


def _download(url: str, dest: Path) -> Path:
    """Stream a URL to dest. Raises SourceMissingError on any failure."""
    log.info(f"Downloading {url} -> {dest.name}")
    try:
        with requests.get(url, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            tmp = dest.with_suffix(dest.suffix + ".part")
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
            tmp.replace(dest)  # atomic move only after full download
    except requests.RequestException as e:
        if dest.exists():
            dest.unlink()  # never leave a partial file behind
        raise SourceMissingError(f"Failed to download {url}: {e}") from e
    return dest


def _skip_if_present(path: Path) -> bool:
    """Idempotency check: True if the file exists and is non-empty."""
    if path.exists() and path.stat().st_size > 0:
        log.info(
            f"Skipping download, raw file already present: {path.name} "
            f"({path.stat().st_size / 1e6:.1f} MB)"
        )
        return True
    return False


# ---------------------------------------------------------------------------
# Source 1: TLC Yellow Taxi trip records (bulk Parquet, per month)
# ---------------------------------------------------------------------------
def fetch_parquet(month: str) -> Path:
    """Fetch one month of Yellow Taxi trips into data/raw/. Idempotent."""
    month = _validate_month(month)
    dest = RAW_DIR / f"yellow_tripdata_{month}.parquet"

    if not _skip_if_present(dest):
        _download(TLC_PARQUET_URL.format(month=month), dest)

    _check_parquet_completeness(dest, month)
    return dest


def _check_parquet_completeness(path: Path, month: str) -> None:
    """Completeness check for a monthly trip file; logged as evidence.

    Raises SourceMissingError if the file is empty, unreadable, missing
    required columns, has implausible row counts, or lacks the expected
    date range. The raw file itself is never modified.
    """
    if path.stat().st_size == 0:
        raise SourceMissingError(f"Raw file is empty: {path.name}")

    try:
        df = pd.read_parquet(path, columns=REQUIRED_TRIP_COLUMNS)
    except Exception as e:
        raise SourceMissingError(f"Raw file unreadable as Parquet: {path.name}: {e}")

    n = len(df)
    log.info(f"[completeness] {path.name}: {n:,} rows")

    missing = set(REQUIRED_TRIP_COLUMNS) - set(df.columns)
    if missing:
        raise SourceMissingError(
            f"{path.name}: missing required columns {sorted(missing)}"
        )

    # Row-count band: catches truncated downloads.
    if not (MIN_TRIP_ROWS <= n <= MAX_TRIP_ROWS):
        raise SourceMissingError(
            f"{path.name}: row count {n:,} outside expected band "
            f"[{MIN_TRIP_ROWS:,}, {MAX_TRIP_ROWS:,}]"
        )

    # Date range: pickups must span (approximately) the full calendar month.
    # Tolerance: TLC files occasionally contain a few rows from adjacent days,
    # so we check coverage of the month, not an exact boundary match.
    start, end = _month_bounds(month)
    ts = pd.to_datetime(df["tpep_pickup_datetime"])
    log.info(
        f"[completeness] {path.name}: pickup range "
        f"{ts.min()} .. {ts.max()} (expected within {start} .. {end})"
    )
    if ts.min() > pd.Timestamp(start) or ts.max() < pd.Timestamp(end):
        raise SourceMissingError(
            f"{path.name}: pickup dates do not cover {month} "
            f"(got {ts.min()} .. {ts.max()})"
        )


# ---------------------------------------------------------------------------
# Source 2: TLC Taxi Zone Lookup Table (CSV)
# ---------------------------------------------------------------------------
def fetch_zone_lookup() -> Path:
    """Fetch the LocationID -> Borough/Zone mapping. Idempotent."""
    dest = RAW_DIR / "taxi_zone_lookup.csv"

    if not _skip_if_present(dest):
        _download(TLC_ZONE_LOOKUP_URL, dest)

    _check_zone_lookup_completeness(dest)
    return dest


def _check_zone_lookup_completeness(path: Path) -> None:
    """The lookup must be non-empty, parse as CSV, and have unique LocationIDs."""
    try:
        df = pd.read_csv(path)
    except Exception as e:
        raise SourceMissingError(f"Zone lookup unreadable as CSV: {path.name}: {e}")

    n = len(df)
    log.info(f"[completeness] {path.name}: {n:,} rows")

    missing = set(REQUIRED_ZONE_COLUMNS) - set(df.columns)
    if missing:
        raise SourceMissingError(
            f"{path.name}: missing required columns {sorted(missing)}"
        )
    # NYC has 265 zones. A much smaller table means a truncated download.
    if n < 250:
        raise SourceMissingError(
            f"{path.name}: only {n} zones (expected ~265) - truncated download?"
        )
    if df["LocationID"].duplicated().any():
        raise SourceMissingError(f"{path.name}: duplicate LocationIDs found")


# ---------------------------------------------------------------------------
# Source 3: Open-Meteo historical weather API (hourly, no key required)
# ---------------------------------------------------------------------------
def fetch_weather(month: str) -> Path:
    """Fetch hourly NYC weather for one month via the Open-Meteo archive API.

    Writes data/raw/weather_<month>.csv. Idempotent. The API is queried in UTC
    (timezone=utc) so join keys are unambiguous; trip timestamps are local New
    York time. Judgment call: for the binary rainy/dry split used in metric 4,
    a one-hour offset at month/DST boundaries is immaterial, and this avoids
    timezone-conversion complexity in the pipeline. Documented as an assumption.
    """
    month = _validate_month(month)
    dest = RAW_DIR / f"weather_{month}.csv"

    if _skip_if_present(dest):
        _check_weather_completeness(dest, month)
        return dest

    start, end = _month_bounds(month)
    params = {
        "latitude": NYC_LAT,
        "longitude": NYC_LON,
        "start_date": start,
        "end_date": end,
        "hourly": "temperature_2m,precipitation",
        "timezone": "UTC",
    }
    log.info(f"Requesting Open-Meteo archive for {month} ({start} .. {end})")
    try:
        resp = requests.get(OPEN_METEO_URL, params=params, timeout=60)
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as e:
        raise SourceMissingError(f"Open-Meteo request failed for {month}: {e}")

    if "error" in payload:
        raise SourceMissingError(
            f"Open-Meteo returned an error for {month}: {payload.get('reason')}"
        )

    hourly = payload.get("hourly", {})
    df = pd.DataFrame(hourly)
    if df.empty:
        raise SourceMissingError(f"Open-Meteo returned no hourly data for {month}")

    # Raw files are stored as fetched; completeness is verified separately.
    # A .part -> rename keeps the write atomic (no half-written raw files).
    tmp = dest.with_suffix(".csv.part")
    df.to_csv(tmp, index=False)
    tmp.replace(dest)
    log.info(f"Saved {len(df):,} hourly weather rows -> {dest.name}")

    _check_weather_completeness(dest, month)
    return dest


def _check_weather_completeness(path: Path, month: str) -> None:
    """Weather must cover every hour of the month (1-hour tolerance for
    UTC/day-boundary edge effects)."""
    df = pd.read_csv(path)
    n = len(df)
    year, mon = int(month[:4]), int(month[5:7])
    expected = calendar.monthrange(year, mon)[1] * 24
    log.info(f"[completeness] {path.name}: {n:,} hourly rows (expected ~{expected})")

    required = {"time", "temperature_2m", "precipitation"}
    missing = required - set(df.columns)
    if missing:
        raise SourceMissingError(f"{path.name}: missing columns {sorted(missing)}")
    if not (expected - 1 <= n <= expected + 1):
        raise SourceMissingError(
            f"{path.name}: {n} hourly rows, expected ~{expected} - incomplete series"
        )
    if df["temperature_2m"].isna().all() or df["precipitation"].isna().all():
        raise SourceMissingError(f"{path.name}: weather series is entirely null")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def fetch_all(month: str) -> dict[str, Path]:
    """Fetch all 3 sources for a month. Returns a dict of raw paths.

    Raises SourceMissingError on any failure; pipeline.py catches it and
    halts the run with a clear, logged error.
    """
    month = _validate_month(month)
    log.info(f"[ingest] start for {month}")
    paths = {
        "trips": fetch_parquet(month),
        "zones": fetch_zone_lookup(),
        "weather": fetch_weather(month),
    }
    log.info(
        "[ingest] done for "
        + month
        + ": "
        + ", ".join(f"{k}={v.name}" for k, v in paths.items())
    )
    return paths


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: python src/ingest.py YYYY-MM [YYYY-MM ...]")
    for m in sys.argv[1:]:
        fetch_all(m)
