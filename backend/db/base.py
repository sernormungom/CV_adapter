"""Declarative base for all ORM models.

Every model in the platform inherits from `Base`. Alembic autogenerate compares
`Base.metadata` against the live database to produce migrations, so every model
module must be imported before autogenerate runs (see `backend/db/__init__.py`).
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
