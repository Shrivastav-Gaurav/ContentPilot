"""Every test runs against its own temp database. The real data/contentpilot.sqlite3 is off limits.

- `_temp_db` (autouse) points contentpilot.db._DB_PATH at a fresh temp file for each test.
- `sqlite3.connect` is guarded for each test: opening the real database fails the test,
  even if the code under test swallows the error (the attempt is re-checked at teardown).
- At the end of the session the real file's modified time and size must be unchanged.
"""
import os
import sqlite3

import pytest

from contentpilot import config

REAL_DB = os.path.normcase(os.path.realpath(config.DB_PATH))


def _is_real_db(database) -> bool:
    if not isinstance(database, (str, bytes, os.PathLike)):
        return False
    path = os.fsdecode(database)
    if path == ":memory:" or path.startswith("file:"):
        path = path[5:].split("?", 1)[0] if path.startswith("file:") else path
        if path in ("", ":memory:"):
            return False
    return os.path.normcase(os.path.realpath(path)) == REAL_DB


@pytest.fixture(autouse=True)
def _temp_db(tmp_path, monkeypatch):
    from contentpilot import db

    monkeypatch.setattr(db, "_DB_PATH", str(tmp_path / "test.sqlite3"))

    attempts = []
    real_connect = sqlite3.connect

    def guarded_connect(database, *args, **kwargs):
        if _is_real_db(database):
            attempts.append(os.fsdecode(database))
            pytest.fail(f"test opened the real database: {database}", pytrace=True)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    yield
    if attempts:
        pytest.fail(f"test tried to open the real database {len(attempts)} time(s): {attempts[0]}")


def _stat(path):
    try:
        st = os.stat(path)
        return st.st_mtime_ns, st.st_size
    except FileNotFoundError:
        return None


@pytest.fixture(autouse=True, scope="session")
def _real_db_untouched():
    before = _stat(config.DB_PATH)
    yield
    after = _stat(config.DB_PATH)
    assert after == before, f"the real database changed during the test run: {before} -> {after}"
