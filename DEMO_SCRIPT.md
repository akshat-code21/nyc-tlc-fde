# Demo Script (3–5 minutes)

Talk track for the NYC TLC Trip Duration Reliability Pipeline. The brief asks for a
demo that explains **one important FDE judgment call**, so this centres on that
(marked ★) rather than touring every file.

**Before you start:** clone the repo, `pip install -r requirements.txt`, and have
the README open at the Pipeline at a Glance diagram. Run the pipeline once
beforehand so you can talk over cached results if the network is slow.

---

## 0:00: The question (20s)

Fleet ops can't see *where* or *when* taxi trips run significantly longer than
expected. No source provides "expected" duration, no traffic data exists, and the
zone information is a bare LocationID. The job was to turn three fragmented
sources into one monthly table that answers it.

## 0:20: Run it live (60s)

In the repo root:

```bash
python src/pipeline.py 2026-01 2026-02 2026-03
```

Point out as it runs:
- Fetches ~190 MB on a cold start; ~18s per month once the data is local.
- Logs every stage to console and `logs/pipeline_run.log`.
- Re-running is safe, because every write overwrites.
- If a source is missing or corrupt it halts with a named `SourceMissingError`
  and exit code 1, rather than writing bad output. (Demonstrated by replacing a
  raw parquet with a corrupt file.)

## 1:20: Walk the pipeline (40s)

Three sources, two retrieval modes (bulk file download + REST API):

`ingest.py`: idempotent fetch, with completeness checks on row count, date span
and required columns → `validate.py`: profiling plus 7 named business rules,
where every rejected row keeps its reason → `model.py`: one row per trip,
joined to zone and hourly weather → `metrics.py`: five metrics.

Point at the diagram: `diagrams/workflow_model.png`.

## ★ 2:00: The judgment call: what counts as "expected"? (90s)

*No source supplies expected trip duration, so I had to derive it, and the choice
changes the answer.* I use the **median duration for that zone-pair and hour**.

**Why median, not mean:** the mean gets pulled up by exactly the long trips I'm
trying to detect. The median is robust to them.

**Why the fallback chain exists:** I profiled the grid first. 70.5% of the
~299,000 zone-pair × hour cells hold fewer than 5 trips, and the median cell holds
2. A median built from 2 trips is noise. So cells under 30 trips fall back:

```
zone-pair × hour  →  pickup-zone × hour  →  borough × hour  →  citywide
```

Every trip records which tier produced its benchmark (`benchmark_level`), so the
fallback is auditable rather than invisible. 70.6% of trips get a zone-pair
benchmark; 99.3% get zone-or-better.

**The uncomfortable part:** the headline delay rate is 32.77%, and that number is
**mostly arithmetic, not a finding**. With a median benchmark and right-skewed
durations, about a third of trips exceed 1.25× the median *by construction*: I
checked: 37.1% exceed 1.25× the citywide median. So I've documented metric 2 as
a **relative ranking between zone-hours**, not an absolute share of bad trips.

The useful output is *"Central Harlem at 16:00 and 17:00 tops the list in both
January and February"*: not *"a third of trips are broken."*

## 3:30: The finding (40s)

The worst decision-grade zone-hours (cells with at least 30 trips) are stable
across all three months: Central Harlem 16:00–17:00 and East Harlem South in
Jan/Feb, South Ozone Park and Jackson Heights in Mar.

Metric 4 is a **negative** result: weather does not explain the unreliability. The
rainy-vs-dry gap is -4.0pp in January, -5.9pp in February, but **+2.5pp in March**
- the sign flips, so rain is not a sufficient explanation and the investigation
points at operations. (Worth saying out loud: my first draft reported only
January and called it settled. Checking all three months is what caught it.)

## 4:10: Limits I know about (30s)

Say these out loud; naming them is the point.

- The benchmark is **self-referential**: a trip in a small cell helps set the
  median it must beat.
- A *uniform* slowdown in a zone is **invisible to metric 2**, because the median
  moves with it. That's why metric 1's averages ship alongside it.
- Revenue-per-mile looks wide, but EWR's $16.40/mi rests on **30 trips**, and the
  ordering mostly tracks trip length. It's a mix effect, not mispricing.
- ~29% of rows have a missing `passenger_count` (a partial upstream feed). No
  metric uses that field, so I kept those trips and flagged them, rather than
  discarding a million real rows a month over a field we never read.

## 4:40: Close (20s)

One command reproduces every number above from raw inputs. The judgment calls are
written down in the README, the code comments and the notebook, so a reviewer can
disagree with any of them and see exactly what would change.

---

## Likely questions

**"Why not just use the mean?"** Because the mean is inflated by the long trips
metric 2 exists to detect. The median is robust to them.

**"Isn't a 25% threshold arbitrary?"** It's a judgment call, and a tunable
constant (`DELAY_THRESHOLD`). I chose it to catch moderate overruns worth acting
on; 50%+ overruns are already obvious in metric 1's averages. Changing it is a
one-line change, and the fallback chain means the benchmark moves sensibly either
way.

**"Why exclude the missing-passenger-count rows?"** I don't. I only reject
*populated* values outside 1–6. The ~29% null block is real trips with missing
metadata, and no metric reads that field, so rejecting them would have discarded
about a million real trips per month for no analytical benefit.

**"How do you know retrieval was complete?"** Every run logs a row-count band
check, a date-span check and a required-column check. The weather file must have
exactly 24 rows per day with no null measurements or the run halts.

**"Can I rerun this safely?"** Yes, verified: rerunning produces byte-identical
output CSVs, and the rejected-row count is unchanged.
