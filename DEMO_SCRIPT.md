# Demo Script

Read only the text inside quotation marks aloud, exactly as written.
[BRACKETS] are your screen directions: do not say them.

## Before you press record

1. Repo cloned, `pip install -r requirements.txt` done.
2. Run `python src/pipeline.py 2026-01 2026-02 2026-03` once beforehand, so all
data is cached and there is no network wait on camera. The recording then
shows a real end-to-end run, not a replay.
3. Have three things open: **(a)** a terminal in the repo root, **(b)** the
GitHub repo page, **(c)** `diagrams/workflow_model.png`.
4. Recording tips: do it in one take. If you flub a line, pause three seconds,
then restart the sentence — that silence is easy to cut. Keep the terminal
font large enough to read at 1080p.

---

## [0:00] Cold open — SCREEN: GitHub repo front page

> "Fleet operations at New York's taxi regulator has a question it can't answer:
> which zones, at which hours, do trips run much longer than they should — so it
> can move drivers there, or investigate what's wrong? The trip data exists, but
> it's scattered across three systems. No source says what 'expected' means. And
> nearly a third of one field is just missing. So I built a monthly pipeline that
> turns those three sources into five metrics, with one command."

## [0:25] The live run — SCREEN: terminal, type the command

[TYPE] `python src/pipeline.py 2026-01 2026-02 2026-03`

> "Here's the whole thing. Three months, end to end — about eighteen seconds a
> month now the data's local. And watch the log as it goes: every stage reports
> its row counts in and out. Ingest proves the download is complete — row count,
> date span, required columns. Validate counts every rule violation by name.
> Nothing is silently dropped: rejects go to a file with their reasons attached,
> clean plus rejected always adds up to the raw count, exactly. Everything
> overwrites, so rerunning is safe — the outputs come out byte-identical. And if
> a source is missing or corrupt, it stops with a named error instead of writing
> bad numbers."

## [1:20] The shape of it — SCREEN: the workflow diagram

> "The shape is simple. Three sources, two retrieval modes: bulk files for the
> trips and the zone lookup, an API for the weather. Validate applies seven
> named rules. The model joins each trip to its zone and to its pickup hour's
> weather. Metrics computes the five numbers. The full detail is in the README —
> I want to spend the time on the one decision that changes the answer."

## [1:50] The judgment call — SCREEN: stay on the diagram

> "No source tells you what an 'expected' trip duration is. So I had to invent
> it — and the choice changes every number downstream. I use the median duration
> for that zone-pair, at that hour of day. Median, not mean, because the mean
> gets dragged up by exactly the long trips I'm trying to detect.
>
> But then I profiled the grid, and it's mostly empty: seventy percent of
> zone-pair-by-hour cells hold fewer than five trips. A median of two trips is
> noise. So cells under thirty trips fall back — zone-pair, to zone, to borough,
> to citywide — and every trip records which tier it used, so you can audit it.
>
> And here's the uncomfortable part. The headline rate is thirty-three percent
> of trips 'unreliable' — but that's mostly arithmetic, not a finding. With a
> median benchmark and skewed durations, a third of trips exceed it by
> construction; I checked. So metric two is a ranking of zone-hours, not a share
> of broken trips. Central Harlem at four and five PM tops the list in both
> January and February. That's the finding — not the percentage."

## [3:15] What the numbers say [about 35 seconds] - SCREEN: the output CSV or KEY_FINDINGS.md

> "The worst cells are stable in January and February — Harlem on afternoons and
> evenings — and they shift in March, which is itself worth investigating. And
> metric four is a negative result: weather doesn't explain the delays. January
> says minus four points, February minus six — but March flips to plus two and a
> half. So the sign isn't even stable. I first wrote this up from January alone
> and called it settled. Checking all three months is what caught it."

## [3:55] Limits, out loud — SCREEN: back to the repo front page

> "Three limits, because naming them is the point. One: the benchmark is partly
> self-referential — a trip in a small cell helps set the median it has to beat.
> Two: a zone that slows down uniformly is invisible, because the median moves
> with it — that's why the plain averages ship alongside the delay rate. Three:
> nearly a third of passenger counts are missing from one upstream feed, so I
> kept those trips and flagged them instead of throwing away a million real rows
> a month over a field no metric reads. One command reproduces everything,
> and every judgment call is written down so a reviewer can disagree with any of
> them and see exactly what would change."

---

## Likely questions (do not read on camera; prep only)

**"Why not just use the mean?"** Because the mean is inflated by the long trips
metric 2 exists to detect. The median is robust to them.

**"Isn't a 25% threshold arbitrary?"** It's a judgment call, and a tunable
constant (`DELAY_THRESHOLD`). I chose it to catch moderate overruns worth acting
on; 50%+ overruns are already obvious in metric 1's averages. Changing it is a
one-line change.

**"Why didn't you drop the missing-passenger-count rows?"** I only reject
*populated* values outside 1–6. The ~29% null block is real trips with missing
metadata, and no metric reads that field, so dropping them would have discarded
about a million real trips per month for no analytical benefit.

**"How do you know retrieval was complete?"** Every run logs a row-count band
check, a date-span check and a required-column check. The weather file must have
exactly 24 rows per day with no null measurements or the run halts.

**"Can I rerun this safely?"** Yes, verified: rerunning produces byte-identical
output CSVs, and the rejected-row count is unchanged.
