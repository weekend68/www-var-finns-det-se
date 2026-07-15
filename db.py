import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

from config import SUBSCRIPTION_TTL_DAYS

DB_PATH = os.getenv("DB_PATH", "/data/medicinstatus.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS medications (
    npl_pack_id         TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    strength            TEXT,
    form                TEXT,
    -- Container/pack-size description (e.g. "Påse, 8 x 1 depotplåster"),
    -- distinct from `form` (dosage form, e.g. "depotplåster"). Only ever
    -- populated for national-shortage-catalogue-backfilled rows (see
    -- national_shortages.py) -- a single product commonly has several
    -- packages short at once sharing the exact same name/form, and this is
    -- what actually distinguishes them. Never guessed/parsed for curated
    -- checker.PRODUCTS rows, which don't need it (one package per entry).
    package_description TEXT,
    -- Product-level NPL id (distinct id-space from npl_pack_id, which is the
    -- PACKAGE-level id) -- needed to link out to FASS Patient
    -- (https://www.fass.se/LIF/product?userType=2&nplId=<npl_id>), which
    -- only accepts product-level ids. Populated for curated checker.PRODUCTS
    -- rows via seed_products() and for catalogue rows via
    -- national_shortages.py's _backfill_medications() (the feed already
    -- carries npl_id per row). May be NULL for medications resolved only via
    -- fass.lookup_name()'s package-level fallback in routes/lakemedel.py.
    npl_id              TEXT,
    -- Marketing authorisation holder (Läkemedelsverket's
    -- MarketAuthorisationHolderName field) -- the actual manufacturer/brand
    -- owner, e.g. "Sandoz A/S". Populated for curated checker.PRODUCTS rows
    -- via seed_products() and for catalogue rows via national_shortages.py's
    -- _backfill_medications() (the feed carries this per product). May be
    -- NULL for medications resolved only via fass.lookup_name()'s
    -- package-level fallback in routes/lakemedel.py, which has no access to
    -- this field.
    manufacturer        TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
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

CREATE INDEX IF NOT EXISTS subscriptions_npl_pack_id ON subscriptions (npl_pack_id);
CREATE INDEX IF NOT EXISTS subscriptions_active_expires ON subscriptions (active, expires_at);

CREATE TABLE IF NOT EXISTS tokens (
    token           TEXT PRIMARY KEY,
    type            TEXT NOT NULL CHECK(type IN ('confirm', 'unsubscribe', 'manage', 'extend')),
    subscriber_id   INTEGER REFERENCES subscribers(id),
    subscription_id INTEGER REFERENCES subscriptions(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at      TEXT NOT NULL,
    used_at         TEXT
);

CREATE INDEX IF NOT EXISTS tokens_subscriber_type ON tokens (subscriber_id, type);
CREATE INDEX IF NOT EXISTS tokens_subscription_type ON tokens (subscription_id, type);

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
CREATE INDEX IF NOT EXISTS poll_log_npl_pack_id_at ON poll_log (npl_pack_id, polled_at DESC);

CREATE TABLE IF NOT EXISTS pharmacy_cache (
    id       INTEGER PRIMARY KEY CHECK(id = 1),
    data     TEXT NOT NULL,
    saved_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Fas 3: broad national shortage catalogue (all current Läkemedelsverket
-- shortage reports, not just the 10 hardcoded checker.PRODUCTS -- see
-- national_shortages.py). One row per PACKAGE (npl_pack_id), matching the
-- rest of the app's granularity -- a single product (npl_id) can have
-- several rows here, one per pack size.
CREATE TABLE IF NOT EXISTS national_shortages (
    npl_pack_id      TEXT PRIMARY KEY REFERENCES medications(npl_pack_id),
    npl_id           TEXT,
    product_name     TEXT,
    atc_code         TEXT,
    atc_term         TEXT,
    type_of_shortage TEXT,
    forecasted_start TEXT,
    forecasted_end   TEXT,
    actual_end       TEXT,
    last_updated     TEXT,
    is_active        INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS national_shortages_atc_active ON national_shortages (atc_code, is_active);

-- Single-row "last successful catalogue refresh" marker, same idea as
-- pharmacy_cache above -- lets checker.py's polling_loop() gate the ~19MB
-- daily feed fetch to once per day instead of every POLL_INTERVAL cycle.
CREATE TABLE IF NOT EXISTS national_shortages_meta (
    id                INTEGER PRIMARY KEY CHECK(id = 1),
    last_refreshed_at TEXT
);
"""

def _migrate_add_column(con, table, column, coltype):
    """CREATE TABLE IF NOT EXISTS only helps brand-new databases -- an
    already-existing production/beta database needs an explicit ALTER TABLE
    for a column added after the table was first created. Guarded by
    PRAGMA table_info rather than relying on SQLite's own
    "ADD COLUMN IF NOT EXISTS" (only in newer SQLite versions) for
    portability across whatever SQLite build the deploy environment has."""
    cols = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(_SCHEMA)
    _migrate_add_column(con, "medications", "package_description", "TEXT")
    _migrate_add_column(con, "medications", "npl_id", "TEXT")
    _migrate_add_column(con, "medications", "manufacturer", "TEXT")
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


def cleanup_old_tokens(db):
    """Tokens are never deleted otherwise -- the table just grows forever.
    Safe to remove: already-used tokens (used_at IS NOT NULL means it can
    never be used again) and tokens expired long enough ago that no
    legitimate use is possible."""
    db.execute(
        "DELETE FROM tokens WHERE used_at IS NOT NULL "
        "OR expires_at < datetime('now', '-7 days')"
    )


def cleanup_expired_subscriptions(db):
    """Delete subscriptions more than 7 days past their expiry, per the
    privacy policy's retention promise ("inaktiva prenumerationer raderas
    automatiskt 7 dagar efter utgångsdatum") -- expiry (not the active flag)
    is the trigger, since nothing else ever flips active=0 on a subscription
    that simply lapses without an explicit unsubscribe/renewal. Deletes any
    tokens still referencing the subscription first (foreign_keys=ON would
    otherwise reject the delete), then soft-deletes any subscriber left with
    zero remaining subscriptions."""
    rows = db.execute(
        "SELECT id, subscriber_id FROM subscriptions WHERE expires_at < datetime('now', '-7 days')"
    ).fetchall()
    for row in rows:
        db.execute("DELETE FROM tokens WHERE subscription_id=?", [row["id"]])
        db.execute("DELETE FROM subscriptions WHERE id=?", [row["id"]])
        remaining = db.execute(
            "SELECT 1 FROM subscriptions WHERE subscriber_id=? LIMIT 1", [row["subscriber_id"]]
        ).fetchone()
        if not remaining:
            db.execute(
                "UPDATE subscribers SET deleted_at=datetime('now') WHERE id=? AND deleted_at IS NULL",
                [row["subscriber_id"]],
            )


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


def escape_like(s):
    """Escape LIKE wildcards in user-supplied text -- a literal % or _ would
    otherwise be interpreted as "any characters"/"any one character" instead
    of a literal, matching far more than an actual substring search should.
    Pair with "... LIKE ? ESCAPE '\\'" in the query."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def get_medication(db, npl_pack_id):
    return db.execute(
        "SELECT npl_pack_id, name, strength, form, package_description, npl_id, manufacturer "
        "FROM medications WHERE npl_pack_id=?",
        [npl_pack_id],
    ).fetchone()


def get_token(db, token, token_type):
    """Shared token lookup used by every /confirm, /extend, /unsubscribe and
    /manage route -- selects t.* plus the subscriber's email/confirmed_at so
    it covers what each of those call sites needs. Callers decide for
    themselves how to treat used_at/expires_at, since each route's own
    used/expired messaging differs."""
    return db.execute(
        "SELECT t.*, sub.email, sub.confirmed_at "
        "FROM tokens t JOIN subscribers sub ON t.subscriber_id=sub.id "
        "WHERE t.token=? AND t.type=?",
        [token, token_type],
    ).fetchone()


def is_medication_indexable(db, npl_pack_id):
    """A medication is worth indexing (sitemap + index,follow) once it has
    EITHER at least one confirmed subscription ever, OR real catalogue data
    from Läkemedelsverket (a row in national_shortages) -- whichever comes
    first. The subscription condition alone left every one of the ~2500
    catalogue-only medications (see national_shortages.py) permanently
    unindexable, since nobody had ever subscribed to them; being in the
    national shortage catalogue is itself proof of real, verifiable content
    worth indexing, independent of subscriber demand."""
    row = db.execute(
        "SELECT 1 WHERE EXISTS ("
        "  SELECT 1 FROM subscriptions s JOIN subscribers sub ON s.subscriber_id = sub.id "
        "  WHERE s.npl_pack_id = ? AND sub.confirmed_at IS NOT NULL"
        ") OR EXISTS ("
        "  SELECT 1 FROM national_shortages ns WHERE ns.npl_pack_id = ?"
        ")",
        [npl_pack_id, npl_pack_id],
    ).fetchone()
    return row is not None


def list_medications_for_sitemap(db):
    """Medications qualifying for sitemap.xml — real name (not a placeholder
    row) and either at least one confirmed subscription ever, or a row in
    national_shortages (Läkemedelsverket catalogue data). Same extended
    condition as is_medication_indexable() above -- keep the two in sync."""
    return db.execute(
        "SELECT m.npl_pack_id, m.name, m.strength, m.form FROM medications m "
        "WHERE m.name != m.npl_pack_id "
        "AND ("
        "  EXISTS ("
        "    SELECT 1 FROM subscriptions s JOIN subscribers sub ON s.subscriber_id = sub.id "
        "    WHERE s.npl_pack_id = m.npl_pack_id AND sub.confirmed_at IS NOT NULL"
        "  )"
        "  OR EXISTS ("
        "    SELECT 1 FROM national_shortages ns WHERE ns.npl_pack_id = m.npl_pack_id"
        "  )"
        ")"
    ).fetchall()


def get_or_create_token(db, token_type, subscriber_id, subscription_id=None, ttl_hours=SUBSCRIPTION_TTL_DAYS * 24):
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
