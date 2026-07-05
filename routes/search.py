import json

from flask import Blueprint, jsonify, request

import checker
import fass
from db import get_db

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
                med = db.execute(
                    "SELECT npl_pack_id, name, strength, form FROM medications WHERE npl_pack_id=?",
                    [npl_id],
                ).fetchone()
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
    # Return cached state first (fast path for monitored medications)
    with checker.state_lock:
        for p in checker.state.get("products", []):
            if p.get("npl_pack_id") == npl_pack_id:
                return jsonify({
                    "npl_pack_id": npl_pack_id,
                    "pharmacies": p.get("pharmacies", []),
                    "cached": True,
                })

    # Live check against a sample of pharmacies (first 300 GLN codes)
    pharmacy_map = checker._pharmacy_map
    if not pharmacy_map:
        return jsonify({"npl_pack_id": npl_pack_id, "pharmacies": [], "cached": False})

    sample_glns = list(pharmacy_map.keys())[:300]
    try:
        pharmacies = fass.check_stock(npl_pack_id, sample_glns, pharmacy_map)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    # Ensure medication exists in DB so subscriptions can reference it
    _upsert_medication(npl_pack_id)

    return jsonify({
        "npl_pack_id": npl_pack_id,
        "pharmacies": pharmacies,
        "cached": False,
        "note": f"Samplad koll på {len(sample_glns)} apotek — prenumerera för fullständig bevakning",
    })


def _upsert_medication(npl_pack_id):
    """Ensure a medication row exists so subscriptions can reference it."""
    try:
        with get_db() as db:
            exists = db.execute(
                "SELECT 1 FROM medications WHERE npl_pack_id=?", [npl_pack_id]
            ).fetchone()
            if not exists:
                db.execute(
                    "INSERT OR IGNORE INTO medications (npl_pack_id, name) VALUES (?, ?)",
                    [npl_pack_id, npl_pack_id],
                )
                db.commit()
    except Exception:
        pass
