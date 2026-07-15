"""National shortage category pages -- browse all of Läkemedelsverket's
current shortage reports grouped by ATC code (substance), not just the 10
hardcoded checker.PRODUCTS. Built on top of national_shortages.py's data
layer (get_shortage_categories/get_shortage_category); this module owns no
data of its own beyond the optional editorial copy in category_editorial.py.

Same informational-only tone as routes/lakemedel.py (see its module-level
comment) -- no promotional/purchase-inducing language.
"""

import re

from flask import Blueprint, redirect, render_template

from category_editorial import get_editorial
from config import SITE_URL
from db import get_db, get_medication
from national_shortages import get_shortage_categories, get_shortage_category
from slugs import category_url, medication_url, slugify_category

bp = Blueprint("kategori", __name__)

# ATC codes are short alphanumeric strings (typically 5-7 chars, e.g.
# "N06BA09"), unlike /lakemedel/'s fixed 14-digit npl_pack_id -- same
# "<id>(-<slug>)?" shape otherwise, see routes/lakemedel.py's _ID_SLUG_RE.
_ID_SLUG_RE = re.compile(r"^([A-Za-z0-9]{3,8})(?:-(.*))?$")


@bp.route("/kategorier")
def kategorier():
    with get_db() as db:
        categories = get_shortage_categories(db)
    for c in categories:
        c["url"] = category_url(SITE_URL, c["atc_code"], c["atc_term"])
    return render_template(
        "kategorier.html",
        categories=categories,
        canonical_url=f"{SITE_URL}/kategorier",
    )


@bp.route("/kategori/<path:id_slug>")
def kategori(id_slug):
    not_found = render_template(
        "message.html",
        title="Kategorin hittades inte",
        message="Vi har ingen bristinformation för den här kategorin, eller så är den för liten för att visas separat.",
        icon="❌",
        cta_url="/kategorier",
        cta_text="Till alla kategorier",
    ), 404

    m = _ID_SLUG_RE.match(id_slug)
    if not m:
        return not_found
    atc_code, given_slug = m.group(1), m.group(2) or ""

    with get_db() as db:
        cat = get_shortage_category(db, atc_code)
        if not cat:
            return not_found

        canonical_slug = slugify_category(cat["atc_term"] or atc_code)
        if given_slug != canonical_slug:
            return redirect(f"/kategori/{atc_code}-{canonical_slug}", code=301)

        # cat["products"] is one row per PACKAGE (npl_pack_id), not per
        # distinct product -- a substance sold in several pack sizes shows up
        # as several rows here, each linking to its own /lakemedel/ page.
        # Look up each package's real medications row (name/strength/form)
        # rather than trusting the feed's product_name verbatim, so the link
        # we build matches routes/lakemedel.py's own canonical slug and
        # doesn't need a redirect hop. Catalogue-only rows (no strength/form)
        # are handled fine here -- slugify_medication() already tolerates
        # None for both.
        products = []
        for pkg in cat["products"]:
            med = get_medication(db, pkg["npl_pack_id"])
            name = (med["name"] if med else None) or pkg["product_name"] or pkg["npl_pack_id"]
            strength = med["strength"] if med else None
            form = med["form"] if med else None
            products.append({
                "npl_pack_id": pkg["npl_pack_id"],
                "name": name,
                "url": medication_url(SITE_URL, pkg["npl_pack_id"], name, strength, form),
            })
        products.sort(key=lambda p: p["name"])

    editorial = get_editorial(atc_code) or {}
    # Auto-generated, neutral fallback when there's no hand-written copy for
    # this ATC code yet (the common case -- ~177 categories, one example
    # entry in category_editorial.json so far): just the substance name as a
    # heading, no intro paragraph.
    title = editorial.get("title") or cat["atc_term"] or atc_code
    intro = editorial.get("intro")

    return render_template(
        "kategori.html",
        cat=cat,
        products=products,
        title=title,
        intro=intro,
        canonical_url=category_url(SITE_URL, atc_code, cat["atc_term"]),
    )
