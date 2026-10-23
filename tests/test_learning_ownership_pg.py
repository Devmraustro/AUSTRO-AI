"""Run the learning cross-owner reference regression suite against PostgreSQL 16.

The test bodies live in tests/test_learning_ownership.py and are copied into
this module's namespace unchanged. Only the ``manager`` fixture is replaced so
the same assertions run on a PostgreSQL schema. Skipped automatically when
PostgreSQL is unreachable.
"""

import pytest

import test_learning_ownership as _shared
import test_repositories_pg as _pg

globals().update({k: v for k, v in vars(_pg).items()
                  if k in ("_pg_engine", "fresh_db", "pg_db")})
globals().update({k: v for k, v in vars(_shared).items()
                  if not k.startswith("__")})


@pytest.fixture()
def manager(pg_db):
    return pg_db._manager
