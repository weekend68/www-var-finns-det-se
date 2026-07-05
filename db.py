import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

DB_PATH = os.getenv("DB_PATH", "/data/medicinstatus.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS medications (
    npl_pack_id TEXT PRIMARY KEY,
    npl_id      TEXT,
    name        TEXT NOT NULL,
    strength    TEXT,
    form        TEXT,
    last_seen_at TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS subscribers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    email        TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    confirmed_at TEXT,
    deleted_at   TEXT
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    subscriber_id    INTEGER NOT NULL REFERENCES subscribers(id),
    npl_pack_id      TEXT NOT NULL REFERENCES medications(npl_pack_id),
    pharmacy_glns    TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at       TEXT NOT NULL,
    last_notified_at TEXT,
    active           INTEGER NOT NULL DEFAULT 1,
    UNIQUE(subscriber_id, npl_pack_id)
);

CREATE TABLE IF NOT EXISTS tokens (
    token           TEXT PRIMARY KEY,
    type            TEXT NOT NULL CHECK(type IN ('confirm', 'unsubscribe', 'manage', 'extend')),
    subscriber_id   INTEGER REFERENCES subscribers(id),
    subscription_id INTEGER REFERENCES subscriptions(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at      TEXT NOT NULL,
    used_at         TEXT
);

CREATE TABLE IF NOT EXISTS daily_mail_count (
    date  TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0
);
"""

_SEED = """
INSERT OR IGNORE INTO medications (npl_pack_id, name, strength, form) VALUES
    ('20040113100574', 'Estradot 25 mcg depotplåster',         '25 mcg/24 h', 'depotplåster'),
    ('20011130100489', 'Estradot 37,5 mcg depotplåster',       '37,5 mcg/24 h', 'depotplåster'),
    ('20011130100502', 'Estradot 50 mcg depotplåster',         '50 mcg/24 h', 'depotplåster'),
    ('20011130100526', 'Estradot 75 mcg depotplåster',         '75 mcg/24 h', 'depotplåster'),
    ('20011130100564', 'Estradot 100 mcg depotplåster',        '100 mcg/24 h', 'depotplåster'),
    ('20181129100025', 'Estrogel transdermal gel 0,75 mg/dos', '0,75 mg/dos', 'gel');
"""


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(_SCHEMA)
    con.executescript(_SEED)
    con.commit()
    con.close()


@contextmanager
def get_db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
    finally:
        con.close()


def create_token(db, token_type, subscriber_id, subscription_id=None, ttl_hours=48):
    token = str(uuid.uuid4())
    expires = (datetime.utcnow() + timedelta(hours=ttl_hours)).isoformat()
    db.execute(
        "INSERT INTO tokens (token,type,subscriber_id,subscription_id,expires_at) VALUES (?,?,?,?,?)",
        [token, token_type, subscriber_id, subscription_id, expires],
    )
    return token


def get_or_create_token(db, token_type, subscriber_id, subscription_id=None, ttl_hours=30 * 24):
    """Return an existing valid token of this type, or create a new one."""
    if subscription_id is not None:
        row = db.execute(
            "SELECT token FROM tokens WHERE subscriber_id=? AND subscription_id=? AND type=? "
            "AND used_at IS NULL AND expires_at > datetime('now')",
            [subscriber_id, subscription_id, token_type],
        ).fetchone()
    else:
        row = db.execute(
            "SELECT token FROM tokens WHERE subscriber_id=? AND type=? "
            "AND used_at IS NULL AND expires_at > datetime('now') "
            "ORDER BY expires_at DESC LIMIT 1",
            [subscriber_id, token_type],
        ).fetchone()
    if row:
        return row["token"]
    return create_token(db, token_type, subscriber_id, subscription_id, ttl_hours)
