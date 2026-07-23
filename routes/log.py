from flask import Blueprint, render_template

from caching import set_cache
from config import CONTENT_MAX_AGE, CONTENT_STALE_WHILE_REVALIDATE
from db import get_db

bp = Blueprint("log", __name__)


@bp.route("/log")
def poll_log():
    with get_db() as db:
        rows = db.execute("""
            SELECT polled_at, npl_pack_id, name, pharmacy_count, glns_checked, notified,
                   LAG(pharmacy_count) OVER (
                       PARTITION BY npl_pack_id ORDER BY polled_at
                   ) AS prev_count
            FROM poll_log
            ORDER BY polled_at DESC, name
            LIMIT 600
        """).fetchall()

    # Group rows by poll cycle (same polled_at timestamp)
    cycles = {}
    for r in rows:
        ts = r["polled_at"]
        if ts not in cycles:
            cycles[ts] = []
        prev = r["prev_count"]
        delta = None if prev is None else r["pharmacy_count"] - prev
        cycles[ts].append({
            "npl_pack_id": r["npl_pack_id"],
            "name":        r["name"],
            "count":       r["pharmacy_count"],
            "checked":     r["glns_checked"],
            "notified":    bool(r["notified"]),
            "delta":       delta,
        })

    return set_cache(
        render_template("log.html", cycles=list(cycles.items())[:100]),
        CONTENT_MAX_AGE, CONTENT_STALE_WHILE_REVALIDATE,
    )
