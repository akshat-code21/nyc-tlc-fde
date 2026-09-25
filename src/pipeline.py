"""pipeline.py - Stage 5: orchestrate ingest -> validate -> model -> metrics.

Runs the whole chain for a given month with logging at every stage, row counts
in and out, and explicit failure handling:

  * A missing, corrupt or incomplete source raises SourceMissingError. The run
    halts with a clear logged error and a non-zero exit code rather than
    silently producing bad output.
  * Every write overwrites, so re-running a month reproduces byte-identical
    outputs instead of appending duplicates (idempotency, Class 8).

Usage:
    python src/pipeline.py 2026-01
    python src/pipeline.py 2026-01 2026-02 2026-03
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import pandas as pd

try:  # package import (python -m src.pipeline)
    from . import ingest, metrics, model, validate
except ImportError:  # direct script run (python src/pipeline.py)
    import ingest
    import metrics
    import model
    import validate

logger = logging.getLogger("pipeline")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = PROJECT_ROOT / "logs"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "pipeline_run.log"),
        logging.StreamHandler(sys.stdout),
    ],
)


class PipelineError(Exception):
    """Raised when a stage fails in a way that should halt the run."""


def run_pipeline(month: str) -> dict:
    """Run ingest -> validate -> model -> metrics for one month.

    Returns a summary dict of row counts and output paths. Raises
    SourceMissingError / PipelineError on failure; the caller decides whether
    to continue with other months.
    """
    started = time.time()
    logger.info("=" * 70)
    logger.info("PIPELINE START for %s", month)
    logger.info("=" * 70)

    # --- Stage 1: ingest ---------------------------------------------------
    stage_started = time.time()
    raw_paths = ingest.fetch_all(month)
    logger.info("[pipeline] stage 1 ingest done in %.1fs", time.time() - stage_started)

    # --- Stage 2: validate -------------------------------------------------
    stage_started = time.time()
    clean, rejected = validate.run(month, raw_paths)
    clean_path, rejected_path = validate.save_outputs(month, clean, rejected)
    logger.info("[pipeline] stage 2 validate done in %.1fs (%s valid, %s rejected)",
                time.time() - stage_started, f"{len(clean):,}", f"{len(rejected):,}")
    del clean, rejected  # free memory before the model stage copies the data

    # --- Stage 3: model ----------------------------------------------------
    stage_started = time.time()
    event_model = model.build_from_disk(month)
    model_path = model.save_model(month, event_model)
    logger.info("[pipeline] stage 3 model done in %.1fs (%s event rows)",
                time.time() - stage_started, f"{len(event_model):,}")
    del event_model

    # --- Stage 4: metrics --------------------------------------------------
    stage_started = time.time()
    trips = pd.read_parquet(model_path)
    results = metrics.compute(trips, month)
    output_paths = metrics.save_metrics(month, results)
    logger.info("[pipeline] stage 4 metrics done in %.1fs (%s metric tables written)",
                time.time() - stage_started, len(output_paths))
    del trips, results

    elapsed = time.time() - started
    logger.info("=" * 70)
    logger.info("PIPELINE SUCCEEDED for %s in %.1fs", month, elapsed)
    logger.info("  clean:    %s", clean_path)
    logger.info("  rejected: %s", rejected_path)
    logger.info("  model:    %s", model_path)
    for path in output_paths:
        logger.info("  metrics:  %s", path)
    logger.info("=" * 70)

    return {
        "month": month,
        "elapsed_seconds": round(elapsed, 1),
        "clean_path": clean_path,
        "rejected_path": rejected_path,
        "model_path": model_path,
        "output_paths": output_paths,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the full NYC TLC metrics pipeline for one or more months."
    )
    parser.add_argument(
        "months", nargs="+",
        help="Months to process, formatted YYYY-MM (e.g. 2026-01).",
    )
    args = parser.parse_args()

    LOG_DIR.mkdir(exist_ok=True)
    failures: list[tuple[str, str]] = []

    for month in args.months:
        try:
            run_pipeline(month)
        except ingest.SourceMissingError as exc:
            # The documented failure path: a missing/corrupt/incomplete source
            # halts this month with a clear message. It is re-raised rather than
            # swallowed so no partial output is mistaken for a good run.
            logger.error("PIPELINE FAILED for %s: source problem: %s", month, exc)
            failures.append((month, str(exc)))
        except (PipelineError, FileNotFoundError, ValueError) as exc:
            logger.error("PIPELINE FAILED for %s: %s: %s",
                         month, type(exc).__name__, exc)
            failures.append((month, f"{type(exc).__name__}: {exc}"))
        except Exception as exc:  # unexpected: still logged, never silent
            logger.exception("PIPELINE FAILED for %s with an unexpected error", month)
            failures.append((month, f"unexpected {type(exc).__name__}: {exc}"))

    if failures:
        logger.error("=" * 70)
        logger.error("PIPELINE FINISHED WITH %d FAILED MONTH(S):", len(failures))
        for month, reason in failures:
            logger.error("  %s: %s", month, reason)
        raise SystemExit(1)

    logger.info("All %d month(s) completed successfully.", len(args.months))


if __name__ == "__main__":
    main()