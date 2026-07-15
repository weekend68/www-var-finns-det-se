import re
from datetime import datetime

from flask import Blueprint, redirect, render_template, request

import checker
import fass
import shortage
from config import HISTORY_RELIABLE_SINCE, MIN_CONSECUTIVE_POLLS, SITE_URL, SUBSCRIPTION_TTL_DAYS
from db import escape_like, get_db, get_medication, is_medication_indexable
from national_shortages import get_shortage_category
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
    status.

    Only ever looks at rows at/after HISTORY_RELIABLE_SINCE -- older rows were
    recorded before this run-length filtering existed, so we don't actually
    know whether an old "boundary" in that data was a real change or just
    two-plus consecutive noisy polls the current threshold would have let
    through too. Rather than retroactively trust pre-fix data, a missing
    confirmed boundary within the reliable window is reported as "monitored
    since HISTORY_RELIABLE_SINCE, no change seen" (see at_least below) instead
    of guessing a specific day count from data we can't vouch for."""
    rows = db.execute(
        "SELECT polled_at, pharmacy_count FROM poll_log WHERE npl_pack_id=? AND polled_at >= ? "
        "ORDER BY polled_at DESC LIMIT ?",
        [npl_pack_id, HISTORY_RELIABLE_SINCE, limit],
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

    # No confirmed boundary within the reliable window -- whether that's
    # because we hit `limit` or simply ran out of rows at HISTORY_RELIABLE_SINCE,
    # either way we can't vouch for a specific transition date. Report this
    # as "monitored since HISTORY_RELIABLE_SINCE, no change seen" (the
    # template uses reliable_since_date for that) rather than a specific,
    # possibly-wrong day count.
    at_least = not found_boundary

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
        "reliable_since_date": HISTORY_RELIABLE_SINCE[:10],
    }


def _sibling_packages(db, med):
    """Other packages/strengths of the same medication. medications.npl_id is
    never populated by any current code path, so name-prefix matching on the
    trade name is the only DB-only signal available today.

    This pulls in both curated checker.PRODUCTS rows and national-shortage-
    catalogue-backfilled rows, which frequently use different naming
    conventions for what's sometimes the exact same strength and sometimes a
    genuinely different package (see national_shortages.py's
    _backfill_medications docstring) -- package_description is included so
    the template can show it as a distinguishing subtext, same treatment as
    the main subtitle/search results."""
    base = (med["name"] or "").strip().split(" ")[0]
    if len(base) < 3:
        return []
    escaped_base = escape_like(base)
    rows = db.execute(
        "SELECT npl_pack_id, name, strength, form, package_description FROM medications "
        "WHERE name LIKE ? ESCAPE '\\' AND npl_pack_id != ? AND name != npl_pack_id "
        "ORDER BY name LIMIT 10",
        [f"{escaped_base}%", med["npl_pack_id"]],
    ).fetchall()
    return [
        {
            "npl_pack_id": r["npl_pack_id"],
            "name": r["name"],
            "package_description": r["package_description"],
            "slug": slugify_medication(r["name"], r["strength"], r["form"]),
        }
        for r in rows
    ]


def _category_breadcrumb(db, npl_pack_id):
    """If this package is part of a national-shortage category that reaches
    get_shortage_category()'s min_products threshold (see national_shortages.py),
    return {"atc_code", "atc_term"} for a discreet breadcrumb link back to that
    category's page (routes/kategori.py). None for the common case -- most
    products either aren't in national_shortages at all, or their category is
    too small to have its own page -- and the template must render nothing
    in that case, not an empty link."""
    row = db.execute(
        "SELECT atc_code FROM national_shortages WHERE npl_pack_id=? "
        "AND atc_code IS NOT NULL AND atc_code != ''",
        [npl_pack_id],
    ).fetchone()
    if not row:
        return None
    return get_shortage_category(db, row["atc_code"])


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
        category = _category_breadcrumb(db, npl_pack_id)

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

    # Postnummer lives in a cookie (set client-side, see templates/lakemedel.html),
    # not the URL -- keeps it out of browser history, copy-pasted links, and
    # third-party Referer headers. ?omrade= is still accepted as a fallback
    # (e.g. a manually-typed URL), taking priority if present.
    #
    # Keep the raw value separate from the normalized (3-digit) omrade --
    # normalize_omrade() truncates a full postnummer down to its matching
    # precision, but redisplaying that truncated value in the postnummer
    # input field looks broken to someone who just typed a full 5-digit code.
    omrade_input = request.args.get("omrade", "").strip() or request.cookies.get("postnummer", "").strip()
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
        category=category,
        indexable=indexable,
        show_partner_guide=npl_pack_id in checker.MENOPAUSE_RELATED_IDS,
        canonical_url=canonical_url,
        jsonld=jsonld,
        ttl_days=SUBSCRIPTION_TTL_DAYS,
    )
