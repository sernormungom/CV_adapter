"""Pure cryptographic helpers for auth — no database access.

Passwords are hashed with bcrypt. Session and invite tokens are random
url-safe strings; only their SHA-256 hash is stored, so the raw value (held in
a cookie or invite URL) never touches the database.

The bcrypt work factor comes from `config.BCRYPT_ROUNDS`, which defaults to 12
and cannot go below it in production (`backend/config.py`). `verify_password`
takes no cost parameter deliberately: bcrypt encodes the cost in the hash, so
a stored hash always verifies at the cost it was created with — lowering the
setting for a test run cannot make an existing production hash cheaper to
attack, and raising it later does not invalidate old hashes.
"""

import hashlib
import secrets

import bcrypt

from backend import config


def hash_password(password: str) -> str:
    salt = bcrypt.gensalt(rounds=config.BCRYPT_ROUNDS)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"), password_hash.encode("utf-8")
        )
    except (ValueError, TypeError):
        return False


def generate_token() -> str:
    """A fresh secret token to put in a cookie or invite link."""
    return secrets.token_urlsafe(32)


def hash_token(raw_token: str) -> str:
    """Deterministic SHA-256 hash used to look a token up in the database."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
