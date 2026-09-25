# Key Findings (Jan–Mar 2026)

What the pipeline actually found, with the caveats that stop each number being
over-read. Every figure here comes from `output/metrics_<month>.csv` and is
reproducible with:

```bash
python src/pipeline.py 2026-01 2026-02 2026-03
```

Metric definitions are in the [README](README.md#metrics). The "expected duration"
benchmark and why it behaves the way it does is in
[The Expected-Duration Benchmark](README.md#the-expected-duration-benchmark).

---

## 1. Where and when trips overrun their benchmark

The worst *decision-grade* zone-hours (cells with at least 30 trips, so the
benchmark rests on enough observations to mean something):

| Month | Worst zone-hour | Trips | Delay rate | Avg vs expected |
|---|---|---|---|---|
| 2026-01 | Central Harlem, 16:00 | 1,024 | 51.6% | 23.8 min vs 16.6 min |
| 2026-02 | Central Harlem, 17:00 | 1,298 | 50.8% | 20.5 min vs 14.8 min |
| 2026-03 | South Ozone Park, 16:00 | 91 | 59.3% | 33.4 min vs 19.8 min |

**January and February agree.** Central Harlem at 16:00-17:00 and East Harlem
South at 13:00-21:00 sit at the top in both months, with 986-1,972 trips behind
each cell. That stability across independent months is what makes these worth
acting on: a single month's tail could be noise, but the same zones and hours
repeating is a pattern.

**March looks different, and the reason is worth noticing.** The March leaders
are South Ozone Park and Jackson Heights, on much thinner samples (91-161 trips),
and the two Harlem zones drop out of the top five. Two readings are possible:
either the Harlem pattern genuinely eased, or the March grid shifted enough that
the benchmark each cell is measured against moved. We cannot separate those with
these three sources, and it is the single most useful thing to investigate next.

**Read this as a relative ranking, not an absolute failure rate.** The overall
delay rate hovers near 35% in every month, but that number is largely arithmetic:
the benchmark is a median and trip durations are right-skewed, so roughly a third
of trips exceed 1.25x their benchmark by construction (37.1% exceed 1.25x the
citywide median in Jan). The ranking is the finding; the percentage is not.

## 2. Revenue per mile by borough

| Borough | Jan | Feb | Mar | Trips (Jan) |
|---|---|---|---|---|
| EWR | $16.40 | $13.62 | $35.77 | 30 |
| Manhattan | $10.46 | $10.96 | $10.58 | 3,074,511 |
| Brooklyn | $6.40 | $6.85 | $6.53 | 153,311 |
| Queens | $5.82 | $5.94 | $5.90 | 320,937 |
| Staten Island | $5.97 | $6.13 | $5.82 | 460 |
| Bronx | $4.77 | $5.00 | $4.73 | 36,217 |

Manhattan, Brooklyn, Queens, Staten Island and the Bronx are all stable to within
a few cents across three months, which is what a real structural difference looks
like.

**Two numbers here should not be trusted, and the table shows why:**

- **EWR is not a real signal.** It swings from $13.62 to $35.77 on a base of
  **30-31 trips**. A handful of long airport runs can move that ratio several
  fold. The `trips` column exists in the output precisely so this trap is
  visible; it is the reason the metric ships with its denominator.
- **The spread is a mix effect, not mispricing.** Revenue per mile tracks trip
  length almost perfectly: Manhattan averages 14.8 min per trip, the Bronx 36.7
  min. Short airport runs earn a high ratio per mile; long outer-borough runs
  earn a low one. That is arithmetic, not evidence that any borough is
  over- or under-priced. To answer the pricing question you would need to control
  for distance, which is outside this pipeline.

## 3. Weather-adjusted delay rate

| Month | Rainy | Dry | Difference | Rainy temp / duration | Dry temp / duration |
|---|---|---|---|---|---|
| 2026-01 | 29.2% (365,565) | 33.2% (3,224,477) | **-4.00pp** | 1.4C / 16.0 min | -2.6C / 17.4 min |
| 2026-02 | 27.7% (296,118) | 33.6% (2,992,825) | **-5.92pp** | 0.9C / 16.4 min | -3.0C / 18.0 min |
| 2026-03 | 33.3% (628,349) | 30.8% (3,203,443) | **+2.46pp** | 6.3C / 17.5 min | 7.7C / 17.4 min |

**The honest conclusion is that the weather signal is not consistent, so weather
is not a reliable explanation for the unreliability.**

An earlier draft of this project reported the January figure alone ("rainy hours
are delayed *less* often, so weather is not the driver") as a settled result. It
is not. March reverses the sign: rainy hours are delayed *more* often, and March
also has far more rain (628k rainy-hour trips against 365k in January). One
plausible reading is that the relationship is confounded by season and time of
day rather than by rain itself: rainy trips are shorter on average in January and
February (16.0 and 16.4 min against 17.4 and 18.0), and shorter trips are more
likely to beat a benchmark built from longer ones.

**What this does and does not support.** It is enough to say that rain is not a
sufficient explanation for zone-hour unreliability, which redirects the
investigation toward operations. It is **not** enough to say rain has no effect.
Settling that needs rain intensity rather than a binary flag, and controls for
hour and season, neither of which this pipeline does.

## 4. Data quality (metric 5)

| Month | Zero-distance | Zero-fare | Missing `passenger_count` | Duplicate-key rate |
|---|---|---|---|---|
| 2026-01 | 2.73% | 0.04% | 30.22% | 0.89% |
| 2026-02 | 3.02% | 0.05% | 31.03% | 0.66% |
| 2026-03 | 2.40% | 0.06% | 24.61% | 0.43% |

The first two columns are the *subject* of metric 5: zero-distance and zero-fare
trips are real records that validation deliberately keeps, because cancelled or
disputed trips are part of what fleet ops needs to see.

The last two are caveats on everything above. The ~25-31% missing
`passenger_count` is the partial upstream feed documented in the README's Facts
section. The duplicate-key rate falls steadily across the three months (0.89% to
0.43%); the flag counts rows sharing a (pickup, dropoff, zone-pair) key, and
because those rows carry different distances and fares they are treated as
distinct trips rather than deduplicated. The decline is unexplained and would be
worth a question to TLC.

---

## What to do with this

For **fleet operations:** the January/February pattern points at Central Harlem
and East Harlem South during the afternoon and evening peak. The metric tables
sort decision-grade cells first, so `output/metrics_<month>_unreliable_trip_rate.csv`
is the working list.

For the **policy analyst:** weather does not explain the unreliability, and
revenue per mile reflects trip mix rather than pricing. The open question is what
does explain it, and the three sources here cannot answer that.

**Highest-value next step:** reconcile why the March ranking differs from
January and February, and obtain a historical (multi-month or prior-year)
expected-duration baseline. A benchmark derived from the month being measured
cannot distinguish "this zone got worse" from "the yardstick moved", which is
the core limitation of metrics 2 and 4.
