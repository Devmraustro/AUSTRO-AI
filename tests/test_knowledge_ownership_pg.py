"""Run the knowledge cross-owner regression suite against PostgreSQL 16.

Test bodies live in tests/test_knowledge_ownership.py. Only the ``manager``
fixture is replaced. Skipped automatically when PostgreSQL is unreachable.
"""

import pytest

import test_knowledge_ownership as _shared
import test_repositories_pg as _pg

globals().update({k: v for k, v in vars(_pg).items()
                  if k in ("_pg_engine", "fresh_db", "pg_db")})
globals().update({k: v for k, v in vars(_shared).items()
                  if not k.startswith("__")})


@pytest.fixture()
def manager(pg_db):
    return pg_db._manager
