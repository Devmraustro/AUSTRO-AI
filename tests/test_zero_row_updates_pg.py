"""PostgreSQL companion: zero-row updates report failure (audit finding M1).

Runs the store-level bodies of tests/test_zero_row_updates.py against a real
PostgreSQL 16 server by replacing only the ``manager`` and ``store`` fixtures.
The ingestion-level tests are not re-run here: they build the default container.
Skipped automatically when PostgreSQL is unreachable.
"""
import pytest

import test_repositories_pg as _pg

globals().update({k: v for k, v in vars(_pg).items()
                  if k in ("_pg_engine", "fresh_db", "pg_db")})

from test_zero_row_updates import (  # noqa: E402
    a,
    test_complete_cross_owner_reports_failure_and_changes_nothing,
    test_complete_existing_document_succeeds,
    test_complete_missing_document_reports_failure,
    test_set_state_cross_owner_reports_failure_and_changes_nothing,
    test_set_state_on_existing_source_succeeds,
    test_set_state_on_missing_source_reports_failure,
)


# Names pytest collects from this module. Listing them keeps the re-exported
# bodies and fixtures explicit for pyflakes (CI lint runs over tests/).
__all__ = [
    "a",
    "test_complete_cross_owner_reports_failure_and_changes_nothing",
    "test_complete_existing_document_succeeds",
    "test_complete_missing_document_reports_failure",
    "test_set_state_cross_owner_reports_failure_and_changes_nothing",
    "test_set_state_on_existing_source_succeeds",
    "test_set_state_on_missing_source_reports_failure",
    "manager",
    "store",
]


@pytest.fixture()
def manager(pg_db):
    return pg_db._manager


@pytest.fixture()
def store(manager):
    from app.knowledge.repositories import KnowledgeStore

    return KnowledgeStore(manager)
