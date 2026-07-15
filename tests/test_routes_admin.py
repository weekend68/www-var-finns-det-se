import base64
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def _auth_header(username, password):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_admin_disabled_without_password(client, monkeypatch):
    import routes.admin as admin_mod

    monkeypatch.setattr(admin_mod, "ADMIN_PASSWORD", "")
    r = client.get("/admin")
    assert r.status_code == 404


def test_admin_requires_correct_credentials(client, monkeypatch):
    import routes.admin as admin_mod

    monkeypatch.setattr(admin_mod, "ADMIN_PASSWORD", "secret123")
    monkeypatch.setattr(admin_mod, "ADMIN_USERNAME", "admin")

    assert client.get("/admin").status_code == 401
    assert client.get("/admin", headers=_auth_header("admin", "wrong")).status_code == 401
    assert client.get("/admin", headers=_auth_header("wrong", "secret123")).status_code == 401
    assert client.get("/admin", headers=_auth_header("admin", "secret123")).status_code == 200


def _seed_three_subscribers(db):
    """A: signed up, never confirmed. B: confirmed, never notified.
    C: confirmed and notified 10 minutes after its restock was detected."""
    with db.get_db() as con:
        con.execute("UPDATE medications SET name=? WHERE npl_pack_id=?",
                    ["Estradot 25 mikrogram/24 timmar Depotplåster", "20040113100574"])

        con.execute("INSERT INTO subscribers (email) VALUES (?)", ["pending@example.com"])
        a_id = con.execute("SELECT id FROM subscribers WHERE email=?", ["pending@example.com"]).fetchone()["id"]
        con.execute(
            "INSERT INTO subscriptions (subscriber_id, npl_pack_id, expires_at) VALUES (?, ?, datetime('now','+30 days'))",
            [a_id, "20040113100574"],
        )

        con.execute("INSERT INTO subscribers (email, confirmed_at) VALUES (?, datetime('now'))", ["confirmed_only@example.com"])
        b_id = con.execute("SELECT id FROM subscribers WHERE email=?", ["confirmed_only@example.com"]).fetchone()["id"]
        con.execute(
            "INSERT INTO subscriptions (subscriber_id, npl_pack_id, expires_at, active) VALUES (?, ?, datetime('now','+30 days'), 1)",
            [b_id, "20040113100574"],
        )

        con.execute("INSERT INTO subscribers (email, confirmed_at) VALUES (?, datetime('now'))", ["notified@example.com"])
        c_id = con.execute("SELECT id FROM subscribers WHERE email=?", ["notified@example.com"]).fetchone()["id"]
        con.execute(
            "INSERT INTO subscriptions (subscriber_id, npl_pack_id, expires_at, active, last_notified_at) "
            "VALUES (?, ?, datetime('now','+30 days'), 1, datetime('now'))",
            [c_id, "20040113100574"],
        )

        # poll_log.polled_at is stamped in Europe/Stockholm LOCAL time
        # (checker.py's now_local()), NOT UTC like every other timestamp
        # here -- 10 minutes ago in Stockholm time, matching the real
        # app's convention exactly (see routes/admin.py's
        # _polled_at_to_utc_naive()).
        stockholm_10min_ago = (datetime.now(ZoneInfo("Europe/Stockholm")) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S")
        con.execute(
            "INSERT INTO poll_log (polled_at, npl_pack_id, name, pharmacy_count, glns_checked, notified) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [stockholm_10min_ago, "20040113100574", "Estradot 25 mikrogram/24 timmar Depotplåster", 5, 1000, 1],
        )
        con.commit()


def test_admin_funnel_and_active_subscriptions_count_only_confirmed(client, monkeypatch, ready_db):
    """Regression test for the 2026-07-15 bug: active_subscriptions and the
    signup funnel must exclude person A (signed up, never confirmed) --
    routes/subscribe.py creates a subscriptions row immediately at signup,
    before the double opt-in confirmation, so any query here that forgets
    to join subscribers and filter confirmed_at silently inflates these
    numbers with pending, never-notifiable signups."""
    import db as dbmod
    import routes.admin as admin_mod

    monkeypatch.setattr(admin_mod, "ADMIN_PASSWORD", "secret123")
    _seed_three_subscribers(dbmod)

    r = client.get("/admin", headers=_auth_header("admin", "secret123"))
    assert r.status_code == 200
    body = r.get_data(as_text=True)

    import re

    active = int(re.search(r'<div class="value">(\d+)</div>\s*<div class="label">Aktiva bevakningar', body).group(1))
    assert active == 2  # B + C, not A (unconfirmed)

    funnel_total = int(re.search(r'<div class="value">(\d+)</div>\s*<div class="label">Angett mailadress', body).group(1))
    funnel_confirmed = int(re.search(r'<div class="value">(\d+)</div>\s*<div class="label">Bekräftat mailadress', body).group(1))
    funnel_notified = int(re.search(r'<div class="value">(\d+)</div>\s*<div class="label">Fått minst en notis', body).group(1))
    assert (funnel_total, funnel_confirmed, funnel_notified) == (3, 2, 1)


def test_admin_notification_latency_handles_stockholm_vs_utc_clocks(client, monkeypatch, ready_db):
    """Regression test for the timezone bug caught during development:
    poll_log.polled_at is Stockholm local time, last_notified_at is UTC.
    A naive comparison silently misreports latency by 1-2h depending on
    DST. The seeded restock-to-notification gap here is exactly 10
    minutes -- if the calculation is wrong, this will be off by roughly
    the current UTC offset instead."""
    import db as dbmod
    import routes.admin as admin_mod

    monkeypatch.setattr(admin_mod, "ADMIN_PASSWORD", "secret123")
    _seed_three_subscribers(dbmod)

    r = client.get("/admin", headers=_auth_header("admin", "secret123"))
    body = r.get_data(as_text=True)

    import re

    m = re.search(r'([\d.]+) min</div>\s*<div class="label">Snitt', body)
    assert m, "latency value not found in rendered page"
    latency_minutes = float(m.group(1))
    assert 9.0 <= latency_minutes <= 11.0
