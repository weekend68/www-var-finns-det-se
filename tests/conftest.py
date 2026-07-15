"""Shared pytest fixtures.

Sets SITE_URL/DB_PATH/CACHE_FILE in os.environ *before* importing any app
module -- db.py/config.py/checker.py all read these via os.getenv() at
their own module import time (not lazily), and several other modules do
`from config import SITE_URL` at their own top level, which copies the
value into their own namespace rather than referencing config.SITE_URL
live. Monkeypatching config.SITE_URL later would NOT reach those. Real
per-test isolation for the database is instead done by monkeypatching
db.DB_PATH directly (db.py's functions read that module-level name at
call time, so this works reliably regardless of import order) -- see the
db_path fixture below.
"""
import os

os.environ.setdefault("SITE_URL", "http://testserver")
os.environ.setdefault("DB_PATH", "/tmp/varfinnsdet_pytest_unused.db")
os.environ.setdefault("CACHE_FILE", "/tmp/varfinnsdet_pytest_unused_cache.json")

import pytest

import checker

checker.start_polling = lambda: None  # never spawn the real background poller in tests

import db

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
SAMPLE_SHORTAGES_XML = os.path.join(FIXTURES_DIR, "sample_shortages.xml")


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Point db.DB_PATH at a fresh, empty file for this test only."""
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    return path


@pytest.fixture
def ready_db(db_path):
    """db_path, but already initialized + seeded with the curated placeholders."""
    db.init_db()
    checker.seed_products()
    return db_path


@pytest.fixture
def app(ready_db):
    """A Flask app wired to the per-test database (ready_db runs first)."""
    import app as appmod

    flask_app = appmod.create_app()
    flask_app.testing = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()
