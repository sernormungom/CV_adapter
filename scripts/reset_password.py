"""Reset a user's password (admin recovery for a forgotten login).

There is no self-service password reset; an admin runs this against the
database to set a new password for any account (admin / TA / consultant).

    python -m scripts.reset_password --identifier ta@example.com --password "<pw>"

`--identifier` accepts an email or a username. The new password must satisfy
the account password policy (`backend/auth/service.py::validate_password_policy`):
at least 12 characters with an uppercase letter, a number, and a symbol, and
not the account's own username or email local part.

Resetting also **revokes every live session** for that account and clears any
login lockout, so this is a complete containment action as well as a recovery
one. Tell the user their new password out of band; they can change nothing
in-app today, so this is the supported reset path.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.auth import service  # noqa: E402
from backend.db.session import SessionLocal  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset a user's password.")
    parser.add_argument(
        "--identifier", required=True, help="Email or username of the account"
    )
    parser.add_argument(
        "--password",
        required=True,
        help="New password (min 12 chars, with an uppercase letter, a number, "
        "and a symbol)",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        user = service.set_password(db, args.identifier, args.password)
        print(
            f"Password reset for {user.username} <{user.email}> "
            f"(roles: {', '.join(sorted(user.roles))})"
        )
    except service.AuthError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
