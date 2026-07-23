import csv
import io
import secrets
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, abort, render_template, request

import checker
from config import ADMIN_PASSWORD, ADMIN_USERNAME
from db import get_db

bp = Blueprint("admin", __name__)

# poll_log.polled_at is stamped in Europe/Stockholm local time (checker.py's
# now_local()/_log_poll()), while every other timestamp in the DB
# (subscriptions.created_at/last_notified_at, SQLite's own datetime('now'))
# is UTC -- comparing them naively would silently misreport notification
# latency by the current UTC offset (1-2h depending on DST). See
# _notification_latency() below, the only place these two clocks meet.
_STOCKHOLM = ZoneInfo("Europe/Stockholm")


@bp.before_request
def _require_auth():
    if not ADMIN_PASSWORD:
        abort(404)
    auth = request.authorization
    valid = (
        auth
        and secrets.compare_digest(auth.username, ADMIN_USERNAME)
        and secrets.compare_digest(auth.password, ADMIN_PASSWORD)
    )
    if not valid:
        return Response(
            "Autentisering krävs.", 401,
            {"WWW-Authenticate": 'Basic realm="Admin"'},
        )


@bp.after_request
def _no_store(response):
    # Basic-Auth-gated content must never be cached by a shared cache
    # (Cloudflare) -- a cache hit would serve one admin's authenticated
    # response to anyone, credentials or not.
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _polled_at_to_utc_naive(polled_at_str):
    """poll_log.polled_at ("%Y-%m-%dT%H:%M:%S") is Europe/Stockholm local
    time with no offset stored -- attach the zone, convert to UTC, then drop
    tzinfo again so it compares directly against the UTC-naive strings
    everything else in the DB uses (datetime('now'))."""
    naive = datetime.strptime(polled_at_str, "%Y-%m-%dT%H:%M:%S")
    return naive.replace(tzinfo=_STOCKHOLM).astimezone(timezone.utc).replace(tzinfo=None)


def _notification_stats(db):
    """Andel bekräftade prenumerationer som någonsin fått ett notismejl.
    Deliberately ALL-TIME, not "currently active" -- includes subscriptions
    that have since expired (until db.cleanup_expired_subscriptions() clears
    them ~7 days later), so `total` here can be a different, usually larger
    number than active_subscriptions()'s "right now" count. Not an exact
    SLA, but a directional signal on whether the notification mechanism
    actually delivers over a subscription's whole lifetime."""
    row = db.execute("""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN s.last_notified_at IS NOT NULL THEN 1 ELSE 0 END) AS notified
        FROM subscriptions s
        JOIN subscribers sub ON s.subscriber_id = sub.id
        WHERE sub.confirmed_at IS NOT NULL
    """).fetchone()
    total = row["total"] or 0
    notified = row["notified"] or 0
    rate = round(100 * notified / total, 1) if total else None
    return {"total": total, "notified": notified, "rate": rate}


def _notification_latency(db, sample_size=50):
    """Tid mellan att en restock upptäcks (poll_log.notified=1 -- se
    checker.py's _log_poll(), satt när blip-filtret bekräftar en övergång
    till i lager) och att motsvarande prenumeration faktiskt får sitt
    last_notified_at satt. Ett enskilt notified=1-tillfälle kan trigga flera
    prenumeranters mejl (var för sig, med sin egen cooldown), så detta
    matchar varje prenumeration mot den senaste notified=1-raden för samma
    läkemedel före/vid dess last_notified_at -- ett rimligt, om än inte
    perfekt, mått per mejl.

    Matchningen görs i Python, inte SQL: poll_log.polled_at är Stockholm-
    lokal tid (checker.py's now_local()) medan last_notified_at är UTC
    (SQLite's datetime('now')) -- att jämföra dem direkt i en SQL WHERE-
    sats (t.ex. "polled_at <= ?" med ett UTC-bundet värde) skulle tyst ge
    fel resultat med 1-2 timmar beroende på sommar-/vintertid. Genom att
    hämta rådata och konvertera med _polled_at_to_utc_naive() innan
    jämförelse undviks det helt."""
    subs = db.execute("""
        SELECT npl_pack_id, last_notified_at FROM subscriptions
        WHERE last_notified_at IS NOT NULL
        ORDER BY last_notified_at DESC
        LIMIT ?
    """, [sample_size]).fetchall()
    if not subs:
        return {"count": 0, "avg_minutes": None, "median_minutes": None}

    npl_ids = list({s["npl_pack_id"] for s in subs})
    placeholders = ",".join("?" for _ in npl_ids)
    detections_by_id = {}
    for row in db.execute(
        f"SELECT npl_pack_id, polled_at FROM poll_log WHERE notified=1 AND npl_pack_id IN ({placeholders})",
        npl_ids,
    ):
        detections_by_id.setdefault(row["npl_pack_id"], []).append(
            _polled_at_to_utc_naive(row["polled_at"])
        )
    for detections in detections_by_id.values():
        detections.sort()

    deltas_minutes = []
    for s in subs:
        notified_utc = datetime.strptime(s["last_notified_at"], "%Y-%m-%d %H:%M:%S")
        prior = [d for d in detections_by_id.get(s["npl_pack_id"], []) if d <= notified_utc]
        if not prior:
            continue
        delta = (notified_utc - prior[-1]).total_seconds() / 60
        if delta >= 0:
            deltas_minutes.append(delta)

    if not deltas_minutes:
        return {"count": 0, "avg_minutes": None, "median_minutes": None}
    deltas_minutes.sort()
    n = len(deltas_minutes)
    median = deltas_minutes[n // 2] if n % 2 else (deltas_minutes[n // 2 - 1] + deltas_minutes[n // 2]) / 2
    return {
        "count": n,
        "avg_minutes": round(sum(deltas_minutes) / n, 1),
        "median_minutes": round(median, 1),
    }


def _signup_funnel(db):
    """Person-nivå (subscribers, inte subscriptions) tratt: hur många som
    angav sin mailadress -> hur många av dem som bekräftade den -> hur många
    av dem som någonsin fått minst en bevakningsnotis. All-time, inte
    datumavgränsad -- sajten är för ung för att det ska göra praktisk
    skillnad än, och varje steg räknar ALLA subscribers oavsett deleted_at
    (soft-delete händer bara efter att en prenumeration redan gått ut, ett
    senare livscykelsteg som inte ska dölja ett tidigare genomfört steg i
    tratten).

    Steget före det här (klick på "Bevaka"-knappen) finns bara i Umami
    ("bevaka-klick"-eventet) -- se caption i admin.html, ingen Umami API-
    integration byggd än (medveten avgränsning, kräver en egen API-nyckel
    att sätta upp senare om det blir aktuellt)."""
    total = db.execute("SELECT COUNT(*) AS n FROM subscribers").fetchone()["n"]
    confirmed = db.execute(
        "SELECT COUNT(*) AS n FROM subscribers WHERE confirmed_at IS NOT NULL"
    ).fetchone()["n"]
    notified = db.execute("""
        SELECT COUNT(DISTINCT sub.id) AS n
        FROM subscribers sub
        JOIN subscriptions s ON s.subscriber_id = sub.id
        WHERE s.last_notified_at IS NOT NULL
    """).fetchone()["n"]
    return {"total": total, "confirmed": confirmed, "notified": notified}


def _most_watched(db, limit=10):
    return db.execute("""
        SELECT s.npl_pack_id, m.name, COUNT(*) AS cnt
        FROM subscriptions s
        JOIN subscribers sub ON s.subscriber_id = sub.id
        JOIN medications m ON s.npl_pack_id = m.npl_pack_id
        WHERE sub.confirmed_at IS NOT NULL
        GROUP BY s.npl_pack_id
        ORDER BY cnt DESC
        LIMIT ?
    """, [limit]).fetchall()


def _curated_vs_catalog(db, days=30):
    """Nya BEKRÄFTADE bevakningar senaste `days` dagarna -- måste filtrera på
    sub.confirmed_at precis som alla andra "aktuellt läge"-mått här, annars
    räknas obekräftade signup-försök med och summan blir större än både
    _notification_stats() total och active_subscriptions, vilket bara
    förvirrar (upptäckt 2026-07-15: initial version saknade detta filter)."""
    curated_ids = [p["npl_pack_id"] for p in checker.PRODUCTS]
    placeholders = ",".join("?" for _ in curated_ids)
    row = db.execute(f"""
        SELECT
          SUM(CASE WHEN s.npl_pack_id IN ({placeholders}) THEN 1 ELSE 0 END) AS curated,
          SUM(CASE WHEN s.npl_pack_id NOT IN ({placeholders}) THEN 1 ELSE 0 END) AS catalog
        FROM subscriptions s
        JOIN subscribers sub ON s.subscriber_id = sub.id
        WHERE sub.confirmed_at IS NOT NULL AND sub.deleted_at IS NULL
          AND s.created_at >= datetime('now', ?)
    """, curated_ids + curated_ids + [f"-{days} days"]).fetchone()
    return {"curated": row["curated"] or 0, "catalog": row["catalog"] or 0}


def _weekly_new_subscriptions(db, weeks=8):
    """Nya BEKRÄFTADE bevakningar per vecka -- samma confirmed_at-filter som
    _curated_vs_catalog(), av samma skäl."""
    rows = db.execute("""
        SELECT strftime('%Y-W%W', s.created_at) AS week, COUNT(*) AS cnt
        FROM subscriptions s
        JOIN subscribers sub ON s.subscriber_id = sub.id
        WHERE sub.confirmed_at IS NOT NULL AND sub.deleted_at IS NULL
        GROUP BY week
        ORDER BY week DESC
        LIMIT ?
    """, [weeks]).fetchall()
    return list(rows)


@bp.route("/admin")
def admin():
    with get_db() as db:
        funnel = _signup_funnel(db)
        notification = _notification_stats(db)
        latency = _notification_latency(db)
        most_watched = _most_watched(db)
        split = _curated_vs_catalog(db)
        weekly = _weekly_new_subscriptions(db)
        confirmed_subscribers = db.execute(
            "SELECT COUNT(*) AS n FROM subscribers WHERE confirmed_at IS NOT NULL AND deleted_at IS NULL"
        ).fetchone()["n"]
        # Must join subscribers and filter confirmed_at/deleted_at here too --
        # a subscriptions row is created immediately at signup (routes/
        # subscribe.py), before the double opt-in confirmation, so without
        # this filter an unconfirmed pending signup would count as an
        # "active bevakning" even though it can never actually receive a
        # notification (see checker.py's _notify_subscribers(), which
        # requires the exact same three conditions to even consider sending).
        active_subscriptions = db.execute("""
            SELECT COUNT(*) AS n FROM subscriptions s
            JOIN subscribers sub ON s.subscriber_id = sub.id
            WHERE s.active=1 AND s.expires_at > datetime('now')
              AND sub.confirmed_at IS NOT NULL AND sub.deleted_at IS NULL
        """).fetchone()["n"]

    return render_template(
        "admin.html",
        funnel=funnel,
        notification=notification,
        latency=latency,
        most_watched=most_watched,
        split=split,
        weekly=weekly,
        confirmed_subscribers=confirmed_subscribers,
        active_subscriptions=active_subscriptions,
    )


@bp.route("/admin/poll-log.csv")
def poll_log_csv():
    """Raw poll_log dump for offline flapping-pattern analysis (run-length,
    isolated-blip frequency, threshold simulation) -- no aggregate view of
    this exists elsewhere, and the data isn't PII (medication stock counts,
    no subscriber info), so a plain CSV download behind the same admin auth
    as the dashboard above is enough; no need for a dedicated export UI."""
    with get_db() as db:
        rows = db.execute("""
            SELECT polled_at, npl_pack_id, name, pharmacy_count, glns_checked, notified
            FROM poll_log ORDER BY polled_at
        """).fetchall()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["polled_at", "npl_pack_id", "name", "pharmacy_count", "glns_checked", "notified"])
    for r in rows:
        writer.writerow([r["polled_at"], r["npl_pack_id"], r["name"], r["pharmacy_count"], r["glns_checked"], r["notified"]])

    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=poll_log.csv"},
    )
