"""Bootstrap the first admin account.

Hierarchical invite needs a root admin to exist before anyone can be
provisioned. Run this once against a migrated database.

    python -m scripts.create_admin --email admin@example.com --password "<pw>"

Without --password, an invite URL is printed; redeem it via the set-password
flow (POST /api/auth/invite/accept) to set the password.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.auth import service  # noqa: E402
from backend.config import APP_BASE_URL  # noqa: E402
from backend.db.models import UserRole  # noqa: E402
from backend.db.session import SessionLocal  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Create the bootstrap admin user.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--site", choices=["GBG", "MAL", "STH", "UPS"], default="GBG")
    parser.add_argument("--username", default=None, help="Defaults to email local part")
    parser.add_argument("--full-name", default=None)
    parser.add_argument(
        "--password",
        default=None,
        help="Set immediately; if omitted, an invite URL is printed instead",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        from backend.sites.service import require_active_site
        site = require_active_site(db, code=args.site)
        user, raw_invite = service.create_user(
            db,
            email=args.email,
            site_id=site.id,
            role=UserRole.admin,
            full_name=args.full_name,
            username=args.username,
            created_by=None,
        )
        if args.password:
            service.accept_invite(db, raw_invite, args.password)
            print(f"Admin created and password set: {user.username} <{user.email}>")
        else:
            print(f"Admin created: {user.username} <{user.email}>")
            print(f"Set a password via: {APP_BASE_URL}/html/setup-password.html?token={raw_invite}")
    except service.AuthError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
