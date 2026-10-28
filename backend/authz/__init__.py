"""Authorization: one policy module, plus the dependencies that apply it.

`policy` owns every access decision (and documents the two invariants that keep
it honest: role membership rather than role bypass, and Site is never an
authorization predicate). `dependencies` wires those decisions into routes.
"""

from backend.authz import policy

__all__ = ["policy"]
