import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

DB_PATH = os.getenv("DB_PATH", "/data/medicinstatus.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS medications (
    npl_pack_id TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    strength    TEXT,
    form        TEXT,
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

CREATE TABLE IF NOT EXISTS poll_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    polled_at      TEXT NOT NULL,
    npl_pack_id    TEXT NOT NULL,
    name           TEXT NOT NULL,
    pharmacy_count INTEGER NOT NULL,
    glns_checked   INTEGER NOT NULL DEFAULT 0,
    notified       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS poll_log_at ON poll_log (polled_at DESC);

CREATE TABLE IF NOT EXISTS pharmacy_cache (
    id       INTEGER PRIMARY KEY CHECK(id = 1),
    data     TEXT NOT NULL,
    saved_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(_SCHEMA)
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


def utcnow_str(delta=None):
    """UTC 'now' (optionally offset by a timedelta) as TEXT in the same
    space-separated, second-precision format SQLite's own datetime('now')
    produces. Never store datetime.utcnow().isoformat() in a column that's
    compared against datetime('now') in SQL, or against this function in
    Python -- isoformat()'s 'T' separator sorts after a space, so it always
    compares as "later" than a same-day datetime('now')/utcnow_str() value,
    letting expired tokens/subscriptions pass expiry checks for up to ~24h."""
    dt = datetime.utcnow()
    if delta is not None:
        dt += delta
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def create_token(db, token_type, subscriber_id, subscription_id=None, ttl_hours=48):
    token = str(uuid.uuid4())
    expires = utcnow_str(timedelta(hours=ttl_hours))
    db.execute(
        "INSERT INTO tokens (token,type,subscriber_id,subscription_id,expires_at) VALUES (?,?,?,?,?)",
        [token, token_type, subscriber_id, subscription_id, expires],
    )
    return token


def get_medication(db, npl_pack_id):
    return db.execute(
        "SELECT npl_pack_id, name, strength, form FROM medications WHERE npl_pack_id=?",
        [npl_pack_id],
    ).fetchone()


def is_medication_indexable(db, npl_pack_id):
    """A medication is only worth indexing (sitemap + index,follow) once it has
    at least one confirmed subscription, ever — not merely having been searched.
    Keeps Google's/bots' crawlable surface tied to proven demand instead of
    growing unboundedly with every curious one-off search."""
    row = db.execute(
        "SELECT 1 FROM subscriptions s JOIN subscribers sub ON s.subscriber_id = sub.id "
        "WHERE s.npl_pack_id = ? AND sub.confirmed_at IS NOT NULL LIMIT 1",
        [npl_pack_id],
    ).fetchone()
    return row is not None


def list_medications_for_sitemap(db):
    """Medications qualifying for sitemap.xml — real name (not a placeholder
    row) and at least one confirmed subscription ever."""
    return db.execute(
        "SELECT m.npl_pack_id, m.name, m.strength, m.form FROM medications m "
        "WHERE m.name != m.npl_pack_id "
        "AND EXISTS ("
        "  SELECT 1 FROM subscriptions s JOIN subscribers sub ON s.subscriber_id = sub.id "
        "  WHERE s.npl_pack_id = m.npl_pack_id AND sub.confirmed_at IS NOT NULL"
        ")"
    ).fetchall()


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
