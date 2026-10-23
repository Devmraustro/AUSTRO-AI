"""Run the memory erasure regression suite against real PostgreSQL.

The test bodies and helpers live in tests/test_memory_erasure.py. They are
copied into this module's namespace unchanged, so pytest collects them here as
well; only the ``memory`` fixture is replaced so the same assertions run on a
PostgreSQL 16 schema. Skipped automatically when PostgreSQL is unreachable.
"""

import pytest

import test_memory_erasure as _erasure
import test_repositories_pg as _pg

# PostgreSQL fixtures (per-test clean schema). `fresh_db` shadows conftest's
# SQLite fixture for this module, exactly as in test_repositories_pg.py.
globals().update({k: v for k, v in vars(_pg).items()
                  if k in ("_pg_engine", "fresh_db", "pg_db")})
# Test bodies, helpers and the `seeded` fixture from the shared module.
globals().update({k: v for k, v in vars(_erasure).items()
                  if not k.startswith("__")})


@pytest.fixture()
def memory(pg_db):
    from app.core.container import build_container

    return build_container(db_instance=pg_db).memory
