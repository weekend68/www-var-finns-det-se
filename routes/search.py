from flask import Blueprint, jsonify, request

import checker
import fass
from db import get_db, get_medication

bp = Blueprint("search", __name__)


@bp.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    results = fass.search_medications(q)
    return jsonify(results)


@bp.route("/api/packages")
def api_packages():
    npl_id = request.args.get("nplId", "").strip()
    if not npl_id:
        return jsonify({"error": "nplId required"}), 400

    # npl_pack_id is 14 digits — look up directly in DB, skip Fass packages API
    if len(npl_id) == 14 and npl_id.isdigit():
        try:
            with get_db() as db:
                med = get_medication(db, npl_id)
            if med:
                return jsonify([{
                    "npl_pack_id": med["npl_pack_id"],
                    "name": med["name"],
                    "strength": med["strength"] or "",
                    "form": med["form"] or "",
                }])
        except Exception:
            pass

    packages = fass.get_packages(npl_id)
    return jsonify(packages)


@bp.route("/api/stock/<npl_pack_id>")
def api_stock(npl_pack_id):
    try:
        stock = checker.get_stock_info(npl_pack_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    cached = stock["source"] == "polled"
    if not cached:
        # Live/live_cache/stale result — backfill a real medication name if
        # one was supplied and the row doesn't have one yet.
        med_name = request.args.get("name", "").strip()
        _upsert_medication(npl_pack_id, med_name or None)

    resp = {
        "npl_pack_id": npl_pack_id,
        "pharmacies": stock["pharmacies"],
        "cached": cached,
    }
    if not cached:
        resp["note"] = "Samplad koll på apotek — prenumerera för fullständig bevakning"
    return jsonify(resp)


def _upsert_medication(npl_pack_id, name=None):
    """Ensure a medication row exists with a real name. Fixes rows where name=npl_pack_id."""
    try:
        with get_db() as db:
            existing = db.execute(
                "SELECT name FROM medications WHERE npl_pack_id=?", [npl_pack_id]
            ).fetchone()
            if not existing:
                db.execute(
                    "INSERT OR IGNORE INTO medications (npl_pack_id, name) VALUES (?, ?)",
                    [npl_pack_id, name or npl_pack_id],
                )
                db.commit()
            elif existing["name"] == npl_pack_id and name and name != npl_pack_id:
                db.execute(
                    "UPDATE medications SET name=? WHERE npl_pack_id=?",
                    [name, npl_pack_id],
                )
                db.commit()
    except Exception:
        pass
