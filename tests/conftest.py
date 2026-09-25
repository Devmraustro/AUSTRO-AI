import os
import pathlib
import tempfile

os.environ.setdefault("BOT_TOKEN", "123456789:ABCdefghIJKlmnoPQRstuvwxyz-DummyToken")
os.environ["GEMINI_API_KEY"] = ""
os.environ["DB_PATH"] = str(pathlib.Path(tempfile.gettempdir()) / "austro_ai_test_global.db")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    """Point the shared DB singleton at a fresh temp file for every test."""
    import database

    database.db._close_connection()
    database.db.db_path = str(tmp_path / "test.db")
    database.db._local.connection = None
    database.db.init_database()
    return database.db