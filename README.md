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
| 3 | Which boroughs/zones are affected (for rebalancing decisions)? | LocationID → Borough / Zone / service zone mapping | NYC TLC | Taxi Zone Lookup Table - CSV file linked from the same TLC page | 1 row = 1 LocationID (265 zones) | Lookup is static (snapshot at download time); a small number of trip records reference LocationIDs not present in the lookup (treated as unknown zone) |

---

## Scope

**In scope:** Yellow Taxi trip records only, January-March 2026 (3 months - enough to
demonstrate a repeatable monthly pipeline without excessive volume).

**Out of scope:** Green Taxi, FHV, and HVFHV records - these are different fleets with
different schemas and are not needed for this KPI.

---

## Setup & Usage


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
- **Passenger count of 0 is treated as invalid** (business rule: 1–6), since it most
  likely indicates missing data rather than a real trip.

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

