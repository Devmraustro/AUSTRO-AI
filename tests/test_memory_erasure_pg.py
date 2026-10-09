"""Run the memory erasure regression suite against real PostgreSQL.

The test bodies live in tests/test_memory_erasure.py and are re-collected here
unchanged; only the ``memory`` fixture is replaced so the same assertions run
on a PostgreSQL 16 schema. Skipped automatically when PostgreSQL is unreachable.
"""

import pytest

from test_repositories_pg import (  # noqa: F401 - fixtures, shadows conftest fresh_db
    _pg_engine,
    fresh_db,
    pg_db,
)
from test_memory_erasure import *  # noqa: F401,F403 - re-collect the test bodies


@pytest.fixture()
def memory(pg_db):
    from app.core.container import build_container

    return build_container(db_instance=pg_db).memory
