"""Database package: declarative base, engine, and session dependency.

Import models here so that `Base.metadata` is fully populated whenever this
package is imported — Alembic autogenerate and `create_all` both depend on it.
Models are added in Phase 2 (users, ta_consultants, sources, verdicts,
notifications, ...); the imports go below as each model module lands.
"""

from backend.db.base import Base
from backend.db.session import SessionLocal, engine, get_db

# Importing the models package registers every model on Base.metadata, which
# Alembic autogenerate and create_all rely on.
from backend.db import models  # noqa: E402,F401

__all__ = ["Base", "SessionLocal", "engine", "get_db", "models"]
