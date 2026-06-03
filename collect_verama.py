#!/usr/bin/env python3
"""
Standalone Verama/Ework job collector.

Run from the project root:
    python collect_verama.py
    python collect_verama.py --consultant-id norberto.munoz   # also runs matching

The script opens a Chromium browser using the saved profile in sources.yaml.
Log in to Verama if needed, confirm the location filter, then press Enter
in this terminal to start collecting job detail pages.

Jobs are written to data/job_store/jobs/ — the same store the dashboard reads.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Run from project root without installing the package
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import yaml

from backend.opportunity_pipeline.source_collector.board_connector import collect_from_sources
from backend.opportunity_pipeline.source_collector.position_writer import write_positions
from backend.config import JOB_STORE_DIR, TA_CONFIG_DIR


def _load_verama_sources() -> list:
    sources_file = TA_CONFIG_DIR / "sources.yaml"
    if not sources_file.exists():
        print(f"[ERROR] sources.yaml not found at {sources_file}", file=sys.stderr)
        sys.exit(1)
    all_sources = yaml.safe_load(sources_file.read_text(encoding="utf-8")).get("sources", [])
    # Force-enable verama sources regardless of the enabled flag in yaml
    return [
        {**s, "enabled": True}
        for s in all_sources
        if s.get("type") == "verama_playwright"
    ]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect Verama/Ework jobs and write them to the job store."
    )
    parser.add_argument(
        "--consultant-id",
        metavar="ID",
        default=None,
        help="If provided, run matching/scoring after collection (e.g. norberto.munoz).",
    )
    args = parser.parse_args()

    sources = _load_verama_sources()
    if not sources:
        print("[ERROR] No verama_playwright source found in sources.yaml.", file=sys.stderr)
        sys.exit(1)

    print(f"Starting Verama collection ({len(sources)} source(s))…\n")

    raw_items = collect_from_sources(sources, ROOT)
    manifest = write_positions(raw_items, JOB_STORE_DIR)

    new_count = manifest["new"]
    dup_count = manifest["duplicate"]
    err_count = manifest["error"]

    print(f"\n{'─'*50}")
    print(f"Collection complete:")
    print(f"  New jobs stored : {new_count}")
    print(f"  Duplicates      : {dup_count}")
    print(f"  Errors/skipped  : {err_count}")

    if err_count:
        print("\nErrors:")
        for item in manifest.get("items", []):
            if item.get("status") == "error":
                print(f"  [{item.get('source_id', '?')}] {item.get('error', '')}", file=sys.stderr)

    if args.consultant_id:
        if new_count == 0 and dup_count == 0:
            print("\nNo jobs to score — skipping matching.")
        else:
            print(f"\nRunning matching for '{args.consultant_id}'…")
            from backend.opportunity_pipeline.pre_filter_matcher.batch_assembler import run_matching
            try:
                batch, stats = run_matching(args.consultant_id)
                selected = stats.get("selected", len(batch))
                scored = stats.get("scored", 0)
                print(f"Matching done: {selected} selected from {scored} scored.")
            except Exception as exc:
                print(f"[ERROR] Matching failed: {exc}", file=sys.stderr)
    else:
        if new_count > 0:
            print(f"\nJobs written to: {JOB_STORE_DIR}")
            print("Open the dashboard and click 'Collect & Score Jobs' to run matching.")

    print("─" * 50)


if __name__ == "__main__":
    main()
