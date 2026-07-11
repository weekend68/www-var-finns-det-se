import re
from datetime import datetime

from flask import Blueprint, redirect, render_template, request

import checker
import fass
from config import SITE_URL
from db import get_db, get_medication, is_medication_indexable
from pharmacy_grouping import group_pharmacies_by_omrade, normalize_omrade
from slugs import medication_url, slugify_medication

bp = Blueprint("lakemedel", __name__)

# lakemedel.html must stay strictly informational (availability facts only).
# No promotional/purchase-inducing language ("köp nu", price comparisons,
# urgency framing) -- several tracked products are prescription-only, and
# Swedish law (Läkemedelslagen 2 kap.) restricts marketing of prescription
# drugs to the public.

_ID_SLUG_RE = re.compile(r"^(\d{14})(?:-(.*))?$")


def _stock_history(db, npl_pack_id, limit=200):
    """National aggregate history from poll_log — how long a medication has
    been (out of) stock. Never per-pharmacy/per-postnummer (poll_log only
    stores an aggregate pharmacy_count per poll, not per-pharmacy detail)."""
    rows = db.execute(
        "SELECT polled_at, pharmacy_count FROM poll_log WHERE npl_pack_id=? "
        "ORDER BY polled_at DESC LIMIT ?",
        [npl_pack_id, limit],
    ).fetchall()
    if not rows:
        return None

    in_stock = rows[0]["pharmacy_count"] > 0
    since = rows[0]["polled_at"]
    found_boundary = False
    for r in rows:
        if (r["pharmacy_count"] > 0) != in_stock:
            found_boundary = True
            break
        since = r["polled_at"]
    # "at_least" only means something when we genuinely don't know the exact
    # boundary: the loop ran through the whole fetched window without finding
    # a status change, AND that window was capped by `limit` (so there could
    # be more history before it we didn't fetch). If the loop found the exact
    # boundary, `since` is precise regardless of how many rows were fetched.
    at_least = not found_boundary and len(rows) >= limit

    days = None
    try:
        # polled_at is written as naive LOCAL (Europe/Stockholm) time via
        # checker.now_local(), not UTC -- must compare against local "now",
        # not datetime.utcnow(), or the offset can push this negative.
        since_dt = datetime.fromisoformat(since).replace(tzinfo=checker.TZ)
        days = (datetime.now(checker.TZ) - since_dt).days
    except ValueError:
        pass

    return {
        "in_stock": in_stock,
        "since_date": since[:10],
        "days": days,
        "at_least": at_least,
    }


def _sibling_packages(db, med):
    """Other packages/strengths of the same medication. medications.npl_id is
    never populated by any current code path, so name-prefix matching on the
    trade name is the only DB-only signal available today."""
    base = (med["name"] or "").strip().split(" ")[0]
    if len(base) < 3:
        return []
    rows = db.execute(
        "SELECT npl_pack_id, name, strength, form FROM medications "
        "WHERE name LIKE ? AND npl_pack_id != ? AND name != npl_pack_id "
        "ORDER BY name LIMIT 10",
        [f"{base}%", med["npl_pack_id"]],
    ).fetchall()
    return [
        {
            "npl_pack_id": r["npl_pack_id"],
            "name": r["name"],
            "slug": slugify_medication(r["name"], r["strength"], r["form"]),
        }
        for r in rows
    ]


@bp.route("/lakemedel/<path:id_slug>")
def lakemedel(id_slug):
    m = _ID_SLUG_RE.match(id_slug)
    not_found = render_template("message.html",
        title="Läkemedlet hittades inte",
        message="Vi har ingen information om det här läkemedlet.",
        icon="❌",
        cta_url="/",
        cta_text="Till startsidan",
    ), 404
    if not m:
        return not_found
    npl_pack_id, given_slug = m.group(1), m.group(2) or ""

    with get_db() as db:
        med = get_medication(db, npl_pack_id)
        if not med or med["name"] == npl_pack_id:
            # Row missing or still a placeholder -- this route must work from
            # any entry point (a race with /api/stock's own backfill, a fresh
            # deploy with no poll cycle yet, a notification email, a pasted
            # URL). Share checker's per-ID lock so a burst of concurrent
            # visits to the same shared/never-before-seen link serializes
            # into one Fass lookup instead of a thundering herd.
            with checker.lock_for(npl_pack_id):
                # Re-check inside the lock -- another request for this same
                # medication may have just resolved it while we were waiting.
                med = get_medication(db, npl_pack_id)
                if not med or med["name"] == npl_pack_id:
                    # Try a live Fass lookup first; it reliably fails here
                    # though, since Fass's package/{id} endpoint only accepts
                    # product-level npl_ids, not package-level npl_pack_ids
                    # like this one -- so fall back to ?name=, which the
                    # search UI already knows at click time and passes along
                    # (same trust level as /api/stock's own ?name= backfill).
                    real_name = fass.lookup_name(npl_pack_id)
                    if not real_name:
                        given_name = request.args.get("name", "").strip()
                        if given_name and given_name != npl_pack_id:
                            real_name = given_name
                    if not real_name:
                        return not_found
                    db.execute(
                        "INSERT INTO medications (npl_pack_id, name) VALUES (?, ?) "
                        "ON CONFLICT(npl_pack_id) DO UPDATE SET name=excluded.name "
                        "WHERE medications.name = medications.npl_pack_id",
                        [npl_pack_id, real_name],
                    )
                    db.commit()
                    med = get_medication(db, npl_pack_id)

        canonical_slug = slugify_medication(med["name"], med["strength"], med["form"])
        if given_slug != canonical_slug:
            return redirect(f"/lakemedel/{npl_pack_id}-{canonical_slug}", code=301)

        indexable = is_medication_indexable(db, npl_pack_id)
        history = _stock_history(db, npl_pack_id)
        siblings = _sibling_packages(db, med)

    try:
        stock = checker.get_stock_info(npl_pack_id)
    except Exception:
        stock = {"pharmacies": [], "checked_at": None, "source": "none"}
    pharmacies = stock["pharmacies"]
    # "none" means we genuinely don't know yet (pharmacy register not loaded,
    # or a live check failed with no cache to fall back on) -- must not be
    # conflated with a confirmed-empty result, or we'd confidently tell
    # users/Google a medication is out of stock everywhere when we simply
    # failed to check it.
    stock_unknown = stock.get("source") == "none"
    in_stock_now = len(pharmacies) > 0
    few_only = in_stock_now and not any(p["status"] == "IN_STOCK" for p in pharmacies)

    omrade = normalize_omrade(request.args.get("omrade", ""))
    nara, region, rest = group_pharmacies_by_omrade(pharmacies, omrade)

    canonical_url = medication_url(SITE_URL, npl_pack_id, med["name"], med["strength"], med["form"])

    offer = {"@type": "Offer", "url": canonical_url}
    if not stock_unknown:
        if in_stock_now:
            offer["availability"] = "https://schema.org/LimitedAvailability" if few_only else "https://schema.org/InStock"
        else:
            offer["availability"] = "https://schema.org/OutOfStock"

    jsonld = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": med["name"],
        "offers": offer,
    }

    return render_template(
        "lakemedel.html",
        med=med,
        npl_pack_id=npl_pack_id,
        pharmacies=pharmacies,
        omrade=omrade,
        nara=nara,
        region=region,
        rest=rest,
        in_stock_now=in_stock_now,
        few_only=few_only,
        stock_unknown=stock_unknown,
        checked_at=stock["checked_at"],
        history=history,
        siblings=siblings,
        indexable=indexable,
        show_partner_guide=npl_pack_id in checker.MENOPAUSE_RELATED_IDS,
        canonical_url=canonical_url,
        jsonld=jsonld,
        site_url=SITE_URL,
    )
