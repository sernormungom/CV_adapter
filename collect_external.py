#!/usr/bin/env python3
"""
Standalone collector for sources that require a browser (Playwright-based).
These cannot run inside the uvicorn server, so they are triggered manually
or by a scheduler/cron job.

Run from the project root:

  # Collect all playwright sources defined in sources.yaml
  python collect_external.py

  # Collect only a specific source by id
  python collect_external.py --source-id verama
  python collect_external.py --source-id inkopio

  # Also run matching/scoring after collection
  python collect_external.py --consultant-id norberto.munoz

Jobs are written to data/job_store/jobs/ — the same store the dashboard reads.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import yaml

from backend.opportunity_pipeline.source_collector.board_connector import collect_from_sources
from backend.opportunity_pipeline.source_collector.position_writer import write_positions
from backend.config import JOB_STORE_DIR, TA_CONFIG_DIR

PLAYWRIGHT_TYPES = {"verama_playwright", "inkopio_playwright"}


def _load_sources(source_id: str | None) -> list:
    sources_file = TA_CONFIG_DIR / "sources.yaml"
    if not sources_file.exists():
        print(f"[ERROR] sources.yaml not found at {sources_file}", file=sys.stderr)
        sys.exit(1)
    all_sources = yaml.safe_load(sources_file.read_text(encoding="utf-8")).get("sources", [])

    playwright_sources = [
        s for s in all_sources if s.get("type") in PLAYWRIGHT_TYPES
    ]
    if source_id:
        playwright_sources = [s for s in playwright_sources if s.get("id") == source_id]
        if not playwright_sources:
            print(f"[ERROR] No playwright source with id='{source_id}' found.", file=sys.stderr)
            sys.exit(1)

    # Force-enable regardless of the enabled flag (these are manual/cron sources)
    return [{**s, "enabled": True} for s in playwright_sources]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect jobs from browser-based sources and write to the job store."
    )
    parser.add_argument(
        "--source-id", metavar="ID", default=None,
        help="Collect only this source id (default: all playwright sources).",
    )
    parser.add_argument(
        "--consultant-id", metavar="ID", default=None,
        help="Run matching/scoring after collection (e.g. norberto.munoz).",
    )
    args = parser.parse_args()

    sources = _load_sources(args.source_id)
    if not sources:
        print("[ERROR] No playwright sources found in sources.yaml.", file=sys.stderr)
        sys.exit(1)

    ids = ", ".join(s.get("id", "?") for s in sources)
    print(f"Collecting from: {ids}\n")

    raw_items = collect_from_sources(sources, ROOT)
    manifest = write_positions(raw_items, JOB_STORE_DIR)

    new_count  = manifest["new"]
    dup_count  = manifest["duplicate"]
    err_count  = manifest["error"]

    print(f"\n{'─'*50}")
    print(f"Collection complete:")
    print(f"  New jobs stored : {new_count}")
    print(f"  Duplicates      : {dup_count}")
    print(f"  Errors/skipped  : {err_count}")

    if err_count:
        print("\nErrors:")
        for item in manifest.get("items", []):
            if item.get("status") == "error":
                print(f"  [{item.get('source_id','?')}] {item.get('error','')}", file=sys.stderr)

    if args.consultant_id:
        if new_count == 0 and dup_count == 0:
            print("\nNo jobs in store — skipping matching.")
        else:
            print(f"\nRunning matching for '{args.consultant_id}'…")
            from backend.opportunity_pipeline.pre_filter_matcher.batch_assembler import run_matching
            try:
                batch, stats = run_matching(args.consultant_id)
                print(f"Matching done: {stats.get('selected', len(batch))} selected "
                      f"from {stats.get('scored', 0)} scored.")
            except Exception as exc:
                print(f"[ERROR] Matching failed: {exc}", file=sys.stderr)
    elif new_count > 0:
        print(f"\nJobs written to: {JOB_STORE_DIR}")
        print("Open the dashboard and click 'Collect & Score Jobs' to run matching.")

    print("─" * 50)


if __name__ == "__main__":
    main()
