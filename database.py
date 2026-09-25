"""
AUSTRO AI - Database shim.

Phase B: the shared SQLite instance lives in the `app.database` package. This
module re-exports the exact same `db` object so `database.db` and
`from database import db` keep working unchanged (tests, scripts, conftest).
"""

from app.database import db  # noqa: F401

__all__ = ["db"]