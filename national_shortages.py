"""
Fas 3: broad national shortage catalogue from Läkemedelsverkets open-data
medicine-shortage feed -- ALL currently reported shortages, not just the 10
hardcoded checker.PRODUCTS (that narrower scope is Fas 2's shortage.py /
shortage_data.json, which this module does not touch or replace).

This is the data layer for an upcoming category-browsing UI (built in a
separate task on top of this one): group active shortages by ATC code
(substance) and surface categories with enough distinct affected products to
be worth a page.

Data source (same endpoint Fas 2's scripts/refresh_shortage_snapshot.py
uses, verified public + unauthenticated, ~19MB, updated daily):
  https://docetp.mpa.se/LMF/Reports/opendata-medicine-shortages-current-3-0.xml

Deliberately NOT wired into the proactive Fass stock-polling list
(checker.py's PRODUCTS / _get_subscription_products()) -- this only ever
surfaces Läkemedelsverket's own national forecast data, never live pharmacy
stock, so ingesting the whole feed doesn't multiply load against Fass. If a
user later subscribes to a catalogue product, checker.py's existing
_get_subscription_products() mechanism picks it up automatically -- nothing
here needs to know about that.
"""

import urllib.request
import xml.etree.ElementTree as ET

SHORTAGE_FEED_URL = "https://docetp.mpa.se/LMF/Reports/opendata-medicine-shortages-current-3-0.xml"
NS = "http://eservices.lakemedelsverket.se/opendata/medicineshortage/v3/"

# Minimum time between two full catalogue refreshes -- the feed is a ~19MB
# file that only changes once a day at the source, so this must NOT be
# re-fetched every checker.py poll cycle (as often as every few minutes in
# production). See refresh_national_shortages_if_due().
REFRESH_INTERVAL_HOURS = 24

# Category threshold: an ATC code needs at least this many DISTINCT PRODUCTS
# (not packages) currently short to be worth a category page. Verified
# against real feed data: product-level counting yields ~594 substances
# total, ~176 with >=3 products -- package-level counting would inflate
# both numbers and make the threshold meaningless.
DEFAULT_MIN_PRODUCTS = 3

# SQLite's default bound-parameter limit (SQLITE_MAX_VARIABLE_NUMBER) --
# chunk size for "WHERE npl_pack_id IN (...)" lookups over potentially
# thousands of ids.
_SQL_VAR_CHUNK = 500


def _tag(name):
    return f"{{{NS}}}{name}"


def _fetch(url=SHORTAGE_FEED_URL):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; varfinnsdet/1.0)"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def _parse(xml_source):
    """xml_source: a file path (str) or raw bytes. Returns a dict keyed by
    npl_pack_id (deduping any package that -- unexpectedly -- appears under
    more than one MedicineShortage entry; last one wins), one entry per
    package, carrying its parent product's identity (npl_id/name/ATC) along
    with it.

    Unlike scripts/refresh_shortage_snapshot.py (Fas 2, scoped to
    checker.PRODUCTS' pack ids only and indifferent to product-level
    fields), this walks every MedicineShortage in the feed and needs the
    product-level ATC/name to group by substance later."""
    if isinstance(xml_source, (bytes, bytearray)):
        root = ET.fromstring(xml_source)
    else:
        root = ET.parse(xml_source).getroot()

    rows_by_pack = {}
    for shortage in root.iter(_tag("MedicineShortage")):
        type_of_shortage = shortage.findtext(_tag("TypeOfShortage"))
        for product in shortage.iter(_tag("MedicinalProduct")):
            npl_id = product.findtext(_tag("NPLId"))
            product_name = product.findtext(_tag("ProductName"))
            atc_el = product.find(_tag("ATC"))
            atc_code = atc_el.text.strip() if atc_el is not None and atc_el.text else None
            atc_term = atc_el.get("term") if atc_el is not None else None

            for pkg in product.iter(_tag("PackagedMedicinalProduct")):
                pack_id = pkg.findtext(_tag("NPLPackId"))
                if not pack_id:
                    continue
                interval = pkg.find(_tag("Interval"))
                actual_end = interval.findtext(_tag("ActualEndDate")) if interval is not None else None
                rows_by_pack[pack_id] = {
                    "npl_pack_id": pack_id,
                    "npl_id": npl_id,
                    "product_name": product_name,
                    # A single product (npl_id) commonly has several packages
                    # in shortage at once (e.g. Estradot 37,5 mikrogram/24
                    # timmar: 8-pack AND 24-pack, both real, both short) --
                    # they share the exact same product_name, so without this
                    # they'd look like indistinguishable duplicates in search
                    # results and on medications.form. See _backfill_medications.
                    "package_description": pkg.findtext(_tag("PackageDescription")),
                    "atc_code": atc_code,
                    "atc_term": atc_term,
                    "type_of_shortage": type_of_shortage,
                    "forecasted_start": interval.findtext(_tag("ForecastedStartDate")) if interval is not None else None,
                    "forecasted_end": interval.findtext(_tag("ForecastedEndDate")) if interval is not None else None,
                    "actual_end": actual_end,
                    "last_updated": interval.findtext(_tag("LastUpdated")) if interval is not None else None,
                    # Derived + stored explicitly (rather than recomputed from
                    # actual_end on every read) so callers can index/filter on
                    # it directly -- see national_shortages_atc_active index.
                    "is_active": 0 if actual_end else 1,
                }
    return rows_by_pack


def _backfill_medications(db, rows):
    """Insert a real medications row for any npl_pack_id in `rows` that's
    missing entirely, or update it (name + package_description) if it
    exists only as a name==npl_pack_id placeholder, or if an earlier run of
    this function already set the name but left package_description unset
    (see below). We already have the real ProductName/PackageDescription
    from this feed, so this deliberately avoids ever needing a live
    fass.lookup_name() call for catalogue products.

    Deliberately does NOT set `form` -- that column means "dosage form"
    (e.g. "depotplåster") for curated checker.PRODUCTS rows, populated
    explicitly by seed_products(). There's no reliable way to extract just
    the dosage form from arbitrary freeform ProductName strings across
    ~2000 different real medications without fragile per-product parsing,
    so this leaves `form` unset for catalogue rows rather than guess wrong.
    package_description ("Påse, 8 x 1 depotplåster") is a separate,
    genuinely different piece of information (packaging/pack-size, not
    dosage form) -- a single product commonly has multiple packages short
    at once sharing the exact same product_name (e.g. Estradot 37,5
    mikrogram/24 timmar: an 8-pack and a 24-pack, each its own
    npl_pack_id), and this is what actually distinguishes them; see its
    own display treatment in routes/lakemedel.py and fass.py's search.

    Returns the number of rows backfilled."""
    pack_ids = [r["npl_pack_id"] for r in rows]
    existing = {}
    for i in range(0, len(pack_ids), _SQL_VAR_CHUNK):
        chunk = pack_ids[i:i + _SQL_VAR_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        for row in db.execute(
            f"SELECT npl_pack_id, name, package_description FROM medications WHERE npl_pack_id IN ({placeholders})", chunk
        ):
            existing[row["npl_pack_id"]] = (row["name"], row["package_description"])

    to_backfill = []
    for r in rows:
        if not r["product_name"]:
            continue
        name, package_description = existing.get(r["npl_pack_id"], (r["npl_pack_id"], None))
        is_placeholder = name == r["npl_pack_id"]
        is_our_earlier_backfill_missing_package = name == r["product_name"] and not package_description
        if is_placeholder or is_our_earlier_backfill_missing_package:
            to_backfill.append(r)

    if to_backfill:
        db.executemany(
            "INSERT INTO medications (npl_pack_id, name, package_description) VALUES (?, ?, ?) "
            "ON CONFLICT(npl_pack_id) DO UPDATE SET name=excluded.name, package_description=excluded.package_description, "
            # Clears out `form` for these rows -- only ever reached for
            # catalogue-only entries (curated rows never match the
            # is_placeholder/is_our_earlier_backfill_missing_package check
            # above), so this also self-heals a previous version of this
            # function that mistakenly wrote package_description-like text
            # into `form` instead of this dedicated column.
            "form=NULL",
            [(r["npl_pack_id"], r["product_name"], r["package_description"]) for r in to_backfill],
        )
    return len(to_backfill)


def refresh_national_shortages(xml_source=None):
    """Fetch (unless xml_source is given, mainly for tests) Läkemedelsverket's
    full medicine-shortage feed and fully replace national_shortages with
    today's snapshot, one row per package. This is a full replace, not an
    incremental diff -- the source itself is a complete daily snapshot every
    time, so re-deriving the whole table each run is simpler and more
    correct than trying to diff it. Also backfills medications with real
    product names (see _backfill_medications).

    Idempotent and safe to call repeatedly. Catches its own errors (network,
    parse, DB) and returns None on failure instead of raising, matching the
    defensive style of checker.py's other periodic helpers -- a failed
    refresh must never crash the caller (checker.py's polling_loop).

    Returns on success: {"packages": int, "products": int,
    "medications_backfilled": int}."""
    try:
        if xml_source is None:
            print(f"  Hämtar {SHORTAGE_FEED_URL} (nationell restkatalog)...")
            xml_source = _fetch()
        rows_by_pack = _parse(xml_source)
    except Exception as e:
        print(f"  refresh_national_shortages: hämtning/parsning misslyckades: {e}")
        return None

    rows = list(rows_by_pack.values())

    try:
        from db import get_db
        with get_db() as db:
            # Backfill medications BEFORE replacing national_shortages, so
            # the latter's FK on npl_pack_id -> medications is always
            # satisfied within this same transaction.
            backfilled = _backfill_medications(db, rows)

            db.execute("DELETE FROM national_shortages")
            db.executemany(
                "INSERT INTO national_shortages "
                "(npl_pack_id, npl_id, product_name, atc_code, atc_term, type_of_shortage, "
                "forecasted_start, forecasted_end, actual_end, last_updated, is_active) "
                "VALUES (:npl_pack_id, :npl_id, :product_name, :atc_code, :atc_term, :type_of_shortage, "
                ":forecasted_start, :forecasted_end, :actual_end, :last_updated, :is_active)",
                rows,
            )
            db.commit()
    except Exception as e:
        print(f"  refresh_national_shortages: DB-uppdatering misslyckades: {e}")
        return None

    stats = {
        "packages": len(rows),
        "products": len({r["npl_id"] or r["product_name"] for r in rows}),
        "medications_backfilled": backfilled,
    }
    print(f"  Nationell restkatalog uppdaterad: {stats['packages']} förpackningar / "
          f"{stats['products']} produkter, {stats['medications_backfilled']} nya produktnamn i medications")
    return stats


def _last_refreshed_at(db):
    row = db.execute("SELECT last_refreshed_at FROM national_shortages_meta WHERE id=1").fetchone()
    return row["last_refreshed_at"] if row else None


def _mark_refreshed(db):
    db.execute(
        "INSERT INTO national_shortages_meta (id, last_refreshed_at) VALUES (1, datetime('now')) "
        "ON CONFLICT(id) DO UPDATE SET last_refreshed_at=excluded.last_refreshed_at"
    )
    db.commit()


def refresh_national_shortages_if_due(min_interval_hours=REFRESH_INTERVAL_HOURS):
    """Daily gate around refresh_national_shortages() -- call this (not
    refresh_national_shortages() directly) from checker.py's polling_loop().
    Cheap no-op on every cycle that isn't due yet; mirrors checker.py's
    pharmacy_cache "single-row saved_at" pattern for the same reason: the
    underlying feed is a ~19MB daily snapshot, and POLL_INTERVAL cycles far
    more often than that in production.

    The refreshed timestamp is only persisted on a successful refresh, so a
    failed attempt (network hiccup, etc.) is retried on the next poll cycle
    rather than waiting out a full day.

    Returns refresh_national_shortages()'s stats dict if a refresh actually
    ran, or None if it was skipped (not due yet) or failed."""
    from db import get_db

    try:
        with get_db() as db:
            last = _last_refreshed_at(db)
            if last is not None:
                row = db.execute(
                    "SELECT (julianday('now') - julianday(?)) * 24 AS hours_since", [last]
                ).fetchone()
                if row["hours_since"] is not None and row["hours_since"] < min_interval_hours:
                    return None
    except Exception as e:
        print(f"  refresh_national_shortages_if_due: kunde inte läsa senaste körning: {e}")
        return None

    stats = refresh_national_shortages()
    if stats is not None:
        try:
            with get_db() as db:
                _mark_refreshed(db)
        except Exception as e:
            print(f"  refresh_national_shortages_if_due: kunde inte spara tidsstämpel: {e}")
    return stats


def get_shortage_categories(db, min_products=DEFAULT_MIN_PRODUCTS):
    """Group active (is_active=1) national_shortages rows by ATC code, for
    the upcoming category-browsing UI. Counts DISTINCT PRODUCTS (npl_id,
    falling back to product_name if npl_id is missing) -- NOT distinct
    packages/npl_pack_id -- so a product sold in 5 pack sizes counts once,
    not five (see module-level DEFAULT_MIN_PRODUCTS docstring for the
    verified real-data counts this threshold is calibrated against).

    Returns a list of dicts, one per qualifying category:
      {"atc_code": str, "atc_term": str, "product_count": int,
       "products": [{"npl_pack_id", "npl_id", "product_name"}, ...]}
    sorted by product_count descending. ATC codes with fewer than
    min_products distinct products are omitted entirely."""
    rows = db.execute(
        "SELECT atc_code, atc_term, npl_id, product_name, npl_pack_id "
        "FROM national_shortages WHERE is_active = 1 AND atc_code IS NOT NULL AND atc_code != ''"
    ).fetchall()

    by_atc = {}
    for r in rows:
        entry = by_atc.setdefault(r["atc_code"], {"atc_term": r["atc_term"], "product_keys": set(), "packages": []})
        entry["product_keys"].add(r["npl_id"] or r["product_name"])
        entry["packages"].append({
            "npl_pack_id": r["npl_pack_id"], "npl_id": r["npl_id"], "product_name": r["product_name"],
        })

    categories = [
        {
            "atc_code": atc,
            "atc_term": entry["atc_term"],
            "product_count": len(entry["product_keys"]),
            "products": entry["packages"],
        }
        for atc, entry in by_atc.items()
        if len(entry["product_keys"]) >= min_products
    ]
    categories.sort(key=lambda c: c["product_count"], reverse=True)
    return categories


def get_shortage_category(db, atc_code, min_products=DEFAULT_MIN_PRODUCTS):
    """Detail lookup for one ATC category (for a future category detail
    page). Same product-level counting as get_shortage_categories(). Returns
    None if atc_code has no active shortages, or doesn't reach the
    min_products threshold -- so route handlers can 404 cleanly instead of
    rendering a near-empty category page."""
    for cat in get_shortage_categories(db, min_products=min_products):
        if cat["atc_code"] == atc_code:
            return cat
    return None
