from datetime import timedelta

import db


def test_init_db_is_safe_to_call_twice(db_path):
    db.init_db()
    db.init_db()  # must not raise (CREATE TABLE IF NOT EXISTS + guarded ALTER TABLE)


def test_migrate_add_column_is_idempotent(ready_db):
    with db.get_db() as con:
        db._migrate_add_column(con, "medications", "atc_code", "TEXT")  # already exists
        con.commit()
        cols = {row[1] for row in con.execute("PRAGMA table_info(medications)")}
    assert "atc_code" in cols


def test_get_medication_returns_all_expected_columns(ready_db):
    with db.get_db() as con:
        row = db.get_medication(con, "20040113100574")
    assert row is not None
    for col in ["npl_pack_id", "name", "strength", "form", "package_description", "npl_id", "manufacturer", "atc_code"]:
        assert col in row.keys()


def test_get_medication_unknown_id_returns_none(ready_db):
    with db.get_db() as con:
        assert db.get_medication(con, "00000000000000") is None


def test_utcnow_str_format_matches_sqlite_datetime_now(ready_db):
    """utcnow_str() must produce the same space-separated (not 'T'-separated)
    format as SQLite's own datetime('now'), or expiry/comparison queries
    across the two silently misbehave (see utcnow_str()'s own docstring)."""
    with db.get_db() as con:
        sqlite_now = con.execute("SELECT datetime('now') AS n").fetchone()["n"]
    py_now = db.utcnow_str()
    assert len(py_now) == len(sqlite_now)
    assert "T" not in py_now
    assert py_now[4] == "-" and py_now[10] == " "


def test_cleanup_expired_subscriptions_removes_only_old_expired_rows(ready_db):
    with db.get_db() as con:
        con.execute("INSERT INTO subscribers (email, confirmed_at) VALUES ('old@example.com', datetime('now'))")
        old_id = con.execute("SELECT id FROM subscribers WHERE email='old@example.com'").fetchone()["id"]
        con.execute(
            "INSERT INTO subscriptions (subscriber_id, npl_pack_id, expires_at) VALUES (?, ?, datetime('now', '-10 days'))",
            [old_id, "20040113100574"],
        )
        con.execute("INSERT INTO subscribers (email, confirmed_at) VALUES ('recent@example.com', datetime('now'))")
        recent_id = con.execute("SELECT id FROM subscribers WHERE email='recent@example.com'").fetchone()["id"]
        con.execute(
            "INSERT INTO subscriptions (subscriber_id, npl_pack_id, expires_at) VALUES (?, ?, datetime('now', '-2 days'))",
            [recent_id, "20040113100574"],
        )
        con.commit()

        db.cleanup_expired_subscriptions(con)
        con.commit()

        remaining = {r["subscriber_id"] for r in con.execute("SELECT subscriber_id FROM subscriptions").fetchall()}
        assert old_id not in remaining
        assert recent_id in remaining
