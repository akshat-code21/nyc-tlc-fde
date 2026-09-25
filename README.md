# NYC TLC Trip Duration Reliability Pipeline


## Problem Statement

Fleet operations at NYC TLC lack visibility into where and when taxi trips take significantly longer than expected. Trip data is scattered across monthly bulk files with no linked zone context, no benchmark for "expected" duration, and no view of external factors like weather - so ops can't systematically identify which zone-hours need driver rebalancing or further investigation.
This project builds a repeatable monthly pipeline over TLC Yellow Taxi trip records (Jan–Mar 2026) that validates the raw data, models each trip as a pickup→dropoff event with zone and weather context, and produces 3–5 operational metrics answering: which zone-hours run unreliably long, and is weather a contributing factor?

---

## Stakeholders

| # | Stakeholder | Why They Matter | What I would ask them |
|---|-------------|-----------------|-----------------------|
| 1 | Fleet Operations | They own the driver supply and feel the cost of unreliable trip times directly | "Which zones do you suspect are problematic?" |
| 2 | TLC Policy Analyst | They decide whether delays are an operational issue or a systemic one, and what interventions (congestion policy, curb management) are warranted | "Do you need delays explained (weather? congestion?) or just flagged? And what report granularity do you operate on?" |

---

## Business KPIs & Decision

**Business KPI: Trip duration reliability** - for each zone and hour of the day, how often do trips take significantly longer than expected, and by how much?

**The decision this output supports:**

> Which **zone-hour combinations** need driver rebalancing or further investigation
> into pricing/routing issues - reviewed monthly by fleet operations.

**KPI definition:** "Expected" duration isn't provided by any source - it's derived as
the median duration for that zone-pair and hour-of-day (a judgment call, see Assumptions).

**Success criteria for the pipeline:** for any given month, one command reproduces the
full metric table from raw sources - validated inputs, rejected rows accounted for with
reasons, and identical output on re-runs.


---

## Source Overview

| # | Business Question | Data Needed | Owning Source | Source System / Access | Grain | Known Gaps & Limitations |
|---|-------------------|-------------|---------------|------------------------|-------|--------------------------|
| 1 | Which zone-hour combinations have trips running significantly longer than expected? | Pickup/dropoff timestamps, pickup/dropoff LocationID, trip distance, fare | NYC Taxi & Limousine Commission (TLC) | Yellow Taxi Trip Records - bulk Parquet download per month (`https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page`) | 1 row = 1 completed trip | No ground-truth "expected" duration (must be derived); timestamps occasionally invalid (dropoff before pickup); no traffic/congestion data to explain *why* trips are long |
| 2 | How does weather (rain/temperature) relate to trip delays? | Hourly precipitation and temperature for NYC | Open-Meteo | Historical Weather API - REST, no key required (`https://open-meteo.com/en/docs/historical-weather-api`) | 1 row = 1 hour, city-wide | City-wide aggregate, not route-specific - a trip from JFK to Manhattan experiences different conditions than "NYC average" implies; cannot capture localized events (e.g., a storm hitting only one borough) |
| 3 | Which boroughs/zones are affected (for rebalancing decisions)? | LocationID → Borough / Zone / service zone mapping | NYC TLC | Taxi Zone Lookup Table - CSV file linked from the same TLC page | 1 row = 1 LocationID (265 zones) | Lookup is static (snapshot at download time). Profiling found **zero** trip records with a LocationID missing from the lookup, so the join is currently complete; the rule is retained as a guard against future schema changes. Note that LocationID 264 is literally named "Unknown", so some trips map to a zone with no meaningful borough |

---

## Scope

**In scope:** Yellow Taxi trip records only, January-March 2026 (3 months - enough to
demonstrate a repeatable monthly pipeline without excessive volume).

**Out of scope:** Green Taxi, FHV, and HVFHV records - these are different fleets with
different schemas and are not needed for this KPI.

---

## Setup & Usage

**Requirements:** Python 3.12 with `pandas`, `pyarrow` and `requests`
(`pip install -r requirements.txt`). A full run downloads ~190 MB of TLC data and
takes roughly 2–3 minutes per month on a laptop.

**Run the pipeline stage by stage** (each is idempotent, so re-running is safe):

```bash
# Stage 1 - fetch raw sources into data/raw/ (skips files already present)
python src/ingest.py 2026-01

# Stage 2 - profile + validate, writing data/processed/ and data/rejected/
python src/validate.py 2026-01

# All three months
python src/ingest.py 2026-01 2026-02 2026-03
python src/validate.py 2026-01 2026-02 2026-03
```

Stages 3 (`model.py`), 4 (`metrics.py`) and the orchestrator (`pipeline.py`) are
documented below as they are added. `pipeline.py 2026-01` will run the whole chain
once it exists.

**Explore the data:** open `notebooks/exploration.ipynb` (already executed, with
outputs stored) for the profiling evidence behind every validation rule.

**Outputs produced so far:**

| Path | Contents |
|---|---|
| `data/raw/yellow_tripdata_<month>.parquet` | Untouched TLC download, read-only after ingest |
| `data/raw/weather_<month>.csv` | Hourly Open-Meteo data, local New York time |
| `data/raw/taxi_zone_lookup.csv` | Zone lookup (static, fetched once) |
| `data/processed/<month>_clean.parquet` | Rows passing all business rules, plus quality flags |
| `data/rejected/<month>_rejected.csv` | Every rejected row with a `rejection_reason` column |
| `logs/pipeline_run.log` | Stage-by-stage log: row counts in/out, rule violations, errors |
---

## Metrics

Five operational metrics, all computed per month by the pipeline:

### 1. Average trip duration by zone × hour-of-day
- **Definition:** Mean trip duration (minutes) for each pickup zone and pickup hour (0-23).
- **Grain:** zone × hour → up to 265 × 24 rows/month.
- **Serves:** Fleet ops: the "where and when" heatmap of slow trips.
- **Note:** Small-sample zone-hours are flagged (or excluded); a zone-hour with 2 trips
  has an unreliable average. Threshold documented in code.

### 2. % of trips exceeding expected duration by >25%
- **Definition:** Share of trips where `duration > 1.25 × expected_duration`, where
  **expected duration = median duration for that zone-pair and hour-of-day**.
- **Grain:** trip-level flag, aggregated by zone × hour.
- **Serves:** Fleet ops: the core reliability signal; this IS the KPI.
- **Judgment calls:**
  - *Median* (not mean) as "expected": robust to the very outliers we're detecting.
  - *Zone-pair × hour*: captures directional effects (JFK→Manhattan ≠ Manhattan→JFK).
  - *Fallback:* if a zone-pair/hour has too few trips for a stable median, fall back to
    pickup-zone × hour median, then borough × hour. Fallback chain documented in code.
  - *25% threshold:* stakeholder-informed (see Assumptions); deliberately not 50%+
    so moderate overruns are caught early.

### 3. Revenue per mile by borough
- **Definition:** Total fare-based revenue ÷ total trip distance, by pickup borough.
- **Grain:** borough → 5-7 rows/month (plus "Unknown" for missing LocationIDs).
- **Serves:** TLC policy analyst: are certain boroughs structurally poor value,
  suggesting pricing/routing issues rather than congestion?

### 4. Weather-adjusted delay rate
- **Definition:** Delay rate (% of trips exceeding expected duration by >25%, same
  definition as metric 2) on **rainy hours vs. dry hours**. A trip is "rainy"
  if hourly precipitation at its pickup hour > 0.
- **Grain:** comparison of two delay rates per month (plus per-zone breakdown).
- **Serves:** TLC policy analyst: distinguishes *weather-driven* delays (nobody's
  fault) from *operationally actionable* ones.
- **Judgment call:** rain is binary (precipitation > 0) rather than by intensity:
  simple, defensible, and easy to refine later.

### 5. Zero-distance / zero-fare trip rate
- **Definition:** % of trips with `trip_distance = 0` OR `fare_amount <= 0`.
- **Grain:** monthly rate (plus breakdown by reason).
- **Serves:** both: doubles as a **data-quality metric**. A rising zero-fare rate may
  mean cancelled trips, disputes, or upstream data problems; it bounds how much we
  trust metrics 1–4.

All metric definitions and thresholds are implemented in `src/metrics.py`.

---

## Facts / Assumptions / Bottlenecks

### Facts (verified against the data or sources, via `ingest.py` completeness checks)
- TLC Yellow Taxi records for Jan–Mar 2026 contain **3.4-4.0M trips per month**
  (Jan: 3,724,889; Feb: 3,399,866; Mar: 3,952,451), released as monthly Parquet
  files with a stable, documented schema.
- The Taxi Zone Lookup Table has 265 LocationIDs and is a static snapshot; it does
  not change month to month.
- Open-Meteo provides hourly precipitation and temperature for NYC with no API key,
  and returned a complete hourly series for Jan–Mar 2026 (744/672/744 hourly rows,
  exactly 24 per day, no missing hours).
- Trip duration is not a column in the data: it must be computed as
  dropoff_datetime minus pickup_datetime.
- Monthly TLC files contain a small number of pickup records outside the calendar
  month (a few minutes before/after the boundary), which is normal TLC behavior.
- The March file contains at least one implausibly old record (pickup timestamp
  2008-12-31), a genuine upstream data error; validation must catch and reject it
  rather than pass it through.
- **~29% of trips have a null `passenger_count`** (Jan: 1,088,058 of 3,724,889). The
  nulls are not random: they form one contiguous block at the tail of each monthly
  file, and the *exact same* rows have null `RatecodeID`, `store_and_fwd_flag`,
  `congestion_surcharge`, `Airport_fee` and `payment_type == 0`. This is a partial
  upstream feed, not missing-at-random data. The block's trips are otherwise normal
  (median duration 16.0 min, median fare $22.21, spanning the full month).
- The raw files contain **zero full-row duplicates**. Tens of thousands of rows share
  the (pickup, dropoff, pickup-zone, dropoff-zone) key, but with differing distances
  and fares, i.e. genuinely distinct trips that coincided to the second.
- After validation, 96.7-97.0% of rows survive per month (Jan: 3,590,046 valid /
  134,843 rejected; Feb: 3,288,955 / 110,911; Mar: 3,831,806 / 120,645). Clean +
  rejected reconciles exactly to the raw row count, so nothing is silently dropped.

### Assumptions (judgment calls we made, and why)
- **"Expected" duration is the median per zone-pair and hour-of-day.** No source
  provides ground-truth expected durations. Median is chosen over mean because it is
  robust to the very outliers we are trying to detect.
- **A trip is "unreliable" if it exceeds expected duration by more than 25%.**
  Threshold informed by what fleet ops would act on; moderate overruns are caught
  early, extreme ones are caught by metric 1 anyway.
- **Sparse zone-pair/hour cells fall back** to pickup-zone × hour median, then
  borough × hour, then citywide × hour. Without a fallback, rare zone-pairs would
  have no usable benchmark.
- **"Rainy" means hourly precipitation > 0 at pickup.** Binary rather than
  intensity-based: simple and defensible; can be refined later.
- **A missing `passenger_count` does not invalidate a trip; a populated value outside
  1–6 does.** The assignment's example rule reads "passenger count between 1 and 6",
  but applying it literally would reject the ~29% null block described above and
  discard roughly a million real trips per month. No metric uses passenger count, so
  the cost of that rejection is high and the benefit is zero. Rows with a *populated*
  value of 0 or 7–9 (~14.8k/month) are still rejected, and the missing ones are
  carried through as a `passenger_count_missing` flag for the data-quality view.
- **Minimum trip duration is 1 minute, not > 0.** 45,069 rows have pickup == dropoff
  to the second *while reporting a non-zero distance*, and ~31,000 more last between
  1 and 30 seconds. A trip cannot cover ground in zero seconds, so these are timestamp
  artifacts that would corrupt the duration KPI. The bound is a single named constant
  (`MIN_DURATION_MIN`) in `src/validate.py`.
- **Trips over 100 miles are rejected as data-entry errors.** 162–172 rows per month
  exceed 100 miles, with maxima in the hundreds of thousands of miles. Genuine long
  runs (JFK ↔ Manhattan) are ~30–35 miles, so the cap is generous; without it these
  rows would badly distort revenue-per-mile (metric 3).
- **Zero-fare and zero-distance trips are kept, not rejected.** They are the subject
  of metric 5, so they are evidence rather than error. Only *negative* fares (refunds
  and disputes) are rejected.
- **Rows sharing a (pickup, dropoff, zone-pair) key are kept and flagged, not
  deduplicated.** See the Facts entry above: the shared keys carry different fares and
  distances, so removing them would risk deleting real trips. The
  `is_duplicate_key` flag surfaces them in the data-quality view instead.

### Bottlenecks / Limitations (what this analysis cannot do)
- **No ground truth for "expected":** our benchmark is derived from the same data it
  judges, so a systemic slowdown (e.g., all of Midtown slower in March) shifts the
  baseline and becomes invisible to metric 2.
- **No traffic or congestion data:** we can flag that trips are slow, but not why
  (congestion, construction, closures). Weather is our only external explanatory factor.
- **Weather is city-wide, not route-specific:** a JFK→Manhattan trip in a localized
  storm is matched against the citywide hourly value.
- **No driver-level data:** driver behavior, experience, and shift patterns are
  invisible; two trips on the same route/hour can differ for reasons we cannot see.
- **Yellow fleet only:** findings do not generalize to Green, FHV, or HVFHV fleets
  (different operations, different schemas).
- **Completed trips only:** the TLC data records completed trips. Trips abandoned
  mid-journey (arguably the worst reliability failures) are absent by construction.
- **Small samples:** zone-pairs with few trips per hour have noisy medians even with
  the fallback chain; those rows are flagged, not silently trusted.

