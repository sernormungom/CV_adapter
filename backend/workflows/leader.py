"""One leader per database, via a Postgres session advisory lock.

The problem this solves (P-006, ADR-011 §2): during an ECS rolling deployment
the old task and the new task are both alive for a while. Both would otherwise
start a runner, and two runners racing the same queue can double-execute work
whose handler is only *eventually* idempotent. A single advisory lock makes
leadership a property of the database rather than of deployment timing.

`pg_try_advisory_lock` is deliberate:

- **Session-scoped, not transaction-scoped** (`pg_advisory_lock`, not
  `..._xact_lock`), so it is held for the process's whole life on one dedicated
  connection rather than released at the next commit.
- **Try, not wait.** A losing task must start serving HTTP immediately with its
  runner disabled, not block startup until the old task drains.
- **Released when the session ends.** A session advisory lock is held by the
  Postgres *backend*, so it goes away when that backend does — including when
  the leader is SIGKILLed and its socket closes. The next task takes over with
  no operator unsticking anything, and no lease expiry to wait out. Note this
  is about the server-side session, not the client-side pool: returning or
  disposing a pool does not release a lock held on a checked-out connection,
  which is why `release()` unlocks explicitly and why
  `tests/integration/test_workflow_runner_pg.py` kills the backend rather than
  disposing the engine.

The lock is a Postgres mechanism. On SQLite (the fast gate) `acquire()` returns
True: a single in-process test is trivially the only leader, and there is no
lock to take. Real leadership is proved in `tests/integration/`.
"""

from __future__ import annotations

import logging

from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

#: Arbitrary but fixed application lock id. Any process using this constant
#: competes for the same leadership. Changing it would let two leaders coexist,
#: which is the whole thing this prevents.
#:
#: PostgreSQL advisory locks are scoped to one database. Deployments using
#: different databases may safely use this fixed key; only runners against the
#: same database contend, which is exactly the intended leadership boundary.
WORKFLOW_LEADER_LOCK_KEY = 0x43564144  # "CVAD"


class LeaderLock:
    """Holds workflow leadership for the life of this process."""

    def __init__(self, engine: Engine, lock_key: int = WORKFLOW_LEADER_LOCK_KEY):
        self._engine = engine
        self._lock_key = lock_key
        self._connection = None
        self._is_leader = False

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    @property
    def requires_lock(self) -> bool:
        """False when the backend has no advisory locks (SQLite in tests)."""
        return self._engine.dialect.name == "postgresql"

    def acquire(self) -> bool:
        """Try once to become leader. Idempotent; never raises on contention."""
        if self._is_leader:
            return True

        if not self.requires_lock:
            logger.info(
                "workflow leader: %s backend has no advisory locks; "
                "assuming single-process leadership",
                self._engine.dialect.name,
            )
            self._is_leader = True
            return True

        # A dedicated connection, checked out for as long as leadership lasts.
        # Using a pooled per-statement connection would release the session
        # lock the moment the connection returned to the pool.
        #
        # AUTOCOMMIT matters: without it SQLAlchemy opens an implicit
        # transaction for the lock statement that would then stay open for the
        # process's entire life, leaving a permanently `idle in transaction`
        # backend that holds back vacuum. A session advisory lock is unaffected
        # by autocommit — it belongs to the session, not the transaction.
        connection = self._engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        )
        try:
            acquired = bool(
                connection.exec_driver_sql(
                    "SELECT pg_try_advisory_lock(%s)" % int(self._lock_key)
                ).scalar()
            )
        except Exception:
            connection.close()
            raise

        if not acquired:
            connection.close()
            logger.info(
                "workflow leader: another process holds the lock; "
                "runner disabled in this process"
            )
            return False

        self._connection = connection
        self._is_leader = True
        logger.info("workflow leader: acquired")
        return True

    def verify(self) -> bool:
        """Confirm this process *still* holds leadership.

        Without this the runner can become a false leader: if the lock session
        dies (`pg_terminate_backend`, a failover, a network drop) Postgres
        releases the advisory lock, another task can acquire it, and this
        process would happily keep claiming work through its *pooled* worker
        connections — two leaders, which is the one thing the lock exists to
        prevent. `is_leader` alone cannot detect that, because nothing tells the
        client its session went away.

        A trivial query on the dedicated connection is a sufficient check: an
        advisory lock is held for the life of the session, so a session that
        still answers is a session that still holds the lock. On failure this
        steps itself down so the caller can re-acquire from scratch.
        """
        if not self._is_leader:
            return False
        if not self.requires_lock:
            return True
        if self._connection is None:  # pragma: no cover - defensive
            self._is_leader = False
            return False
        try:
            self._connection.exec_driver_sql("SELECT 1").scalar()
        except Exception:  # noqa: BLE001 - any failure means "not leader"
            logger.error(
                "workflow leader: lock session is gone; standing down",
                exc_info=True,
            )
            self._discard_connection()
            self._is_leader = False
            return False
        return True

    def _discard_connection(self) -> None:
        """Drop the dedicated connection without trying to unlock it."""
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:  # noqa: BLE001 - it is already broken
                pass
            self._connection = None

    def release(self) -> None:
        """Give up leadership and close the dedicated connection."""
        if self._connection is not None:
            try:
                self._connection.exec_driver_sql(
                    "SELECT pg_advisory_unlock(%s)" % int(self._lock_key)
                )
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.warning(
                    "workflow leader: unlock failed; the session close below "
                    "releases it anyway",
                    exc_info=True,
                )
            finally:
                self._discard_connection()
        self._is_leader = False
