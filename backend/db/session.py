"""Engine and session management.

`DATABASE_URL` comes from `backend.config` (env-driven; Postgres everywhere).
`get_db` is the FastAPI dependency that yields a request-scoped session and
always closes it.
"""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.config import DATABASE_URL

# pool_pre_ping avoids handing out connections that the server has already
# dropped (common with cloud Postgres idle timeouts).
engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)

SessionLocal = sessionmaker(
    bind=engine, autocommit=False, autoflush=False, class_=Session
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: yields a session, closes it after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
