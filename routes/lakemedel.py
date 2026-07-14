import re
from datetime import datetime

from flask import Blueprint, redirect, render_template, request

import checker
import fass
import shortage
from config import MIN_CONSECUTIVE_POLLS, SITE_URL, SUBSCRIPTION_TTL_DAYS
from db import escape_like, get_db, get_medication, is_medication_indexable
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
    stores an aggregate pharmacy_count per poll, not per-pharmacy detail).

    This replays already-stored poll_log rows looking for a status flip,
    same as checker.py's polling_loop() does live, poll by poll -- and needs
    the same noise filter for the same reason: fass.py's check_stock() itself
    regularly logs incomplete per-poll coverage (e.g. "50/1453 apotek kunde
    inte kollas"), so a single poll's pharmacy_count can swing to/from 0 even
    though the medication's real stock status never changed. Without
    filtering, one bad poll wedged in the middle of an otherwise-continuous
    run would show up as a (false) "back in stock 0 days ago"/"restnoterat
    sedan idag" -- see MIN_CONSECUTIVE_POLLS' docstring in config.py.
    A flip is only trusted once MIN_CONSECUTIVE_POLLS consecutive rows in a
    row show the new status; a shorter run is skipped over as a blip and the
    scan continues past it as if those rows had matched the surrounding
    status."""
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
    n = len(rows)
    i = 1
    while i < n:
        r_status = rows[i]["pharmacy_count"] > 0
        if r_status == in_stock:
            since = rows[i]["polled_at"]
            i += 1
            continue

        # Status differs from the current run. Count how long this run of
        # the opposite status actually is before deciding whether it's a
        # genuine transition or just noise.
        run_len = 1
        j = i + 1
        while j < n and (rows[j]["pharmacy_count"] > 0) == r_status:
            run_len += 1
            j += 1

        if run_len >= MIN_CONSECUTIVE_POLLS:
            # Confirmed transition -- `since` already holds polled_at of the
            # last row that still matched the current status, right before
            # this (now-confirmed) flip.
            found_boundary = True
            break

        if j >= n:
            # This run of the opposite status reaches all the way to the
            # edge of the fetched window without ever accumulating enough
            # rows to confirm (or rule out) a real transition -- there could
            # be more rows of the same status just past `limit` that would
            # tip it over the threshold. Don't guess either way; stop here
            # with an unresolved boundary (same as running out of rows).
            break

        # A short run of the opposite status, bracketed by rows of the
        # current status further back in time -- a blip. Skip over it
        # entirely (leaving `since` untouched) and keep scanning as if it
        # had matched the current status, so one bad poll doesn't truncate
        # "since" or get reported as a false transition.
        i = j

    # "at_least" only means something when we genuinely don't know the exact
    # boundary: the loop ran through the whole fetched window without
    # confirming a status change, AND that window was capped by `limit` (so
    # there could be more history before it we didn't fetch). If the loop
    # found a confirmed boundary, `since` is precise regardless of how many
    # rows were fetched.
    at_least = not found_boundary and n >= limit

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
    escaped_base = escape_like(base)
    rows = db.execute(
        "SELECT npl_pack_id, name, strength, form FROM medications "
        "WHERE name LIKE ? ESCAPE '\\' AND npl_pack_id != ? AND name != npl_pack_id "
        "ORDER BY name LIMIT 10",
        [f"{escaped_base}%", med["npl_pack_id"]],
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

    shortage_info = shortage.get_shortage_info(npl_pack_id)

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

    # Keep the raw query value separate from the normalized (3-digit) omrade --
    # normalize_omrade() truncates a full postnummer down to its matching
    # precision, but redisplaying that truncated value in the postnummer
    # input field looks broken to someone who just typed a full 5-digit code.
    omrade_input = request.args.get("omrade", "").strip()
    omrade = normalize_omrade(omrade_input)
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
        omrade_input=omrade_input,
        nara=nara,
        region=region,
        rest=rest,
        in_stock_now=in_stock_now,
        few_only=few_only,
        stock_unknown=stock_unknown,
        checked_at=stock["checked_at"],
        history=history,
        shortage_info=shortage_info,
        siblings=siblings,
        indexable=indexable,
        show_partner_guide=npl_pack_id in checker.MENOPAUSE_RELATED_IDS,
        canonical_url=canonical_url,
        jsonld=jsonld,
        ttl_days=SUBSCRIPTION_TTL_DAYS,
    )
