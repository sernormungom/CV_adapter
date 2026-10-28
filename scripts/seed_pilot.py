"""Seed the three fabricated V2 Pilot accounts without logging credentials.

Set PILOT_SEED_PASSWORD in the environment (not on the command line, shell
history, or source control) before running this script.
"""

from __future__ import annotations

import os
import sys

from backend.bootstrap.pilot import PilotSeedError, seed_pilot
from backend.db.session import SessionLocal


def main() -> int:
    password = os.environ.get("PILOT_SEED_PASSWORD")
    if not password:
        print("PILOT_SEED_PASSWORD must be set in the environment", file=sys.stderr)
        return 2
    db = SessionLocal()
    try:
        seed_pilot(db, password=password)
    except PilotSeedError as exc:
        print(f"Pilot seed failed: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()
    print("V2 Pilot seed is present and validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
