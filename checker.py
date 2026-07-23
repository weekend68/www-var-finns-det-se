"""
Varfinnsdet.se polling engine — polls fass.se for stock status.
Runs as a daemon thread inside the Flask/Gunicorn process (started from app.py).

Optional env vars:
  RESEND_API_KEY      — API-nyckel från resend.com (gratis, 100 mail/dag)
  POLL_INTERVAL       — minutes between checks (default: 2)
  CACHE_FILE          — path for persistent state cache (default: /data/medicinstatus_cache.json)
  FROM_EMAIL          — sender address for Resend (default: noreply@varfinnsdet.se)
  NOTIFICATIONS_PAUSED — kill switch for restock notification emails (default: true --
                         temporary, while a Divigel false-restock flap recurred despite the
                         cooldown/blip filter in config.NOTIFY_COOLDOWN_HOURS/MIN_CONSECUTIVE_POLLS;
                         set to "false" to resume sending)
"""

import json
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import fass
from config import MIN_CONSECUTIVE_POLLS, NOTIFY_COOLDOWN_HOURS, POLL_LOG_RETENTION_DAYS, SITE_URL
from fass import check_stock

TZ = ZoneInfo("Europe/Stockholm")
CACHE_FILE = os.getenv("CACHE_FILE", "/data/medicinstatus_cache.json")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "2")) * 60
NOTIFICATIONS_PAUSED = os.getenv("NOTIFICATIONS_PAUSED", "true").strip().lower() in ("1", "true", "yes")
# Delay between submitting each product's check_stock() to the thread pool in
# polling_loop(), instead of firing all of them at Fass simultaneously. See
# the ThreadPoolExecutor submission loop below for why.
STAGGER_SECONDS = float(os.getenv("STAGGER_SECONDS", "1"))
# A "polled" state["all_stock"] entry is only trustworthy if it was actually
# refreshed recently -- a product that starts erroring every cycle (e.g. a
# retired/renamed npl_pack_id) otherwise keeps serving its last successful
# result forever, with nothing but a growing "checked_at" to betray it.
STALE_AFTER = POLL_INTERVAL * 3
SHOW_LIMIT = 3

# Populated by start_polling(); readable by routes for live stock checks
_pharmacy_map: dict = {}

# Tracks consecutive polls with 0 pharmacies per product.
# Requires 2 in a row before clearing prev_in_stock, to avoid false triggers
# caused by transient API failures.
_consecutive_zeros: dict = {}

# Tracks consecutive polls with >0 pharmacies per product, but ONLY while
# transitioning from confirmed-out back to in-stock. Requires 2 in a row
# before flipping prev_in_stock/newly_available, symmetric with
# _consecutive_zeros above -- avoids a one-off single-poll blip (a pharmacy's
# live stock status flickering for a moment) counting as a genuine restock.
_consecutive_positives: dict = {}

# Single source of truth for the hardcoded "always polled, always on the
# homepage" medications. This is CURATION-only: which npl_pack_ids are always
# polled against Fass and shown on the homepage. All display data (name,
# strength, form, npl_id, manufacturer, atc_code, package) lives in the
# medications table like any other catalogue product -- seed_products() only
# guarantees a placeholder row exists here so FK dependents (subscriptions,
# national_shortages) are always satisfiable, and national_shortages.py's
# daily catalogue sync (or fass.py's self-healing lookup) fills in the real
# values, exactly as for any non-curated product.
#
# There used to be a "menopause_related" flag here too, for the
# partner-guide link -- removed since it was redundant with these products'
# real ATC code (Estradiol, G03CA03), which national_shortages.py's backfill
# already learns and persists on medications.atc_code. See
# routes/lakemedel.py's ESTRADIOL_ATC_CODE check.
PRODUCTS = [
    {"npl_pack_id": "20040113100574"},
    {"npl_pack_id": "20011130100489"},
    {"npl_pack_id": "20011130100502"},
    {"npl_pack_id": "20011130100526"},
    {"npl_pack_id": "20011130100564"},
    {"npl_pack_id": "20181129100025"},
    {"npl_pack_id": "20140320100036"},
    {"npl_pack_id": "20160407100353"},
    {"npl_pack_id": "19961001100275"},
    {"npl_pack_id": "20001018100021"},
]


def staleness_tier(timestamp_str):
    """Classify how stale a "YYYY-MM-DD HH:MM:SS" local-time timestamp
    (as written by now_local().strftime(...)) is, for surfacing a manual
    reload prompt instead of blindly auto-reloading the page. Returns
    None (fresh), "1h", "3h", or "1d"."""
    if not timestamp_str:
        return None
    try:
        checked_at = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
    except ValueError:
        return None
    age = datetime.now(TZ) - checked_at
    if age > timedelta(days=1):
        return "1d"
    if age > timedelta(hours=3):
        return "3h"
    if age > timedelta(hours=1):
        return "1h"
    return None


def seed_products():
    """Ensure every PRODUCTS entry has a matching medications row -- just a
    placeholder (name == npl_pack_id) if one doesn't already exist, so
    foreign-key dependents (subscriptions, national_shortages) are always
    satisfiable even on a brand new deploy, before the daily catalogue sync
    (national_shortages.py) or a self-healing lookup has had a chance to fill
    in the real name/strength/form/npl_id/manufacturer. Called once at
    startup, after init_db(). Never overwrites an existing row -- unlike
    before, PRODUCTS is no longer a source of truth for display data."""
    try:
        from db import get_db
        with get_db() as db:
            for p in PRODUCTS:
                db.execute(
                    "INSERT INTO medications (npl_pack_id, name) VALUES (?, ?) "
                    "ON CONFLICT(npl_pack_id) DO NOTHING",
                    [p["npl_pack_id"], p["npl_pack_id"]],
                )
            db.commit()
    except Exception as e:
        print(f"  seed_products fel: {e}")


state = {
    "status": "Startar — hämtar apotekslista...",
    "last_check": None,
    "next_check": None,
    "polls_done": 0,
    # Placeholder name (== npl_pack_id) until the first poll cycle enriches
    # this with the real DB name -- same convention as everywhere else a
    # medication is displayed before its real name is known/resolved.
    "products": [{**p, "name": p["npl_pack_id"], "pharmacies": [], "error": None} for p in PRODUCTS],
    # Latest successful poll result for EVERY actively-polled product (hardcoded
    # PRODUCTS + active subscriptions), keyed by npl_pack_id -> {"pharmacies", "checked_at"}.
    # Superset of "products" above, which stays PRODUCTS-only for the curated
    # homepage display. This is what get_stock_info() checks first.
    "all_stock": {},
}
state_lock = threading.Lock()

# Short-lived cache for live (non-actively-polled) stock checks, keyed by
# npl_pack_id -> (checked_at_unix, pharmacies). Shares TTL with POLL_INTERVAL
# so a burst of page views (human or bot) never checks Fass more often than
# the main polling loop itself does. Used by get_stock_info() for medications
# nobody is actively polling yet (e.g. only ever searched, never subscribed).
_live_stock_cache: dict = {}

# Per-npl_pack_id locks guarding the live-check cache miss path, so concurrent
# requests for the same never-before-cached medication don't each independently
# hit the rate-limit-sensitive Fass API (thundering herd).
_stock_fetch_locks: dict = {}
_stock_fetch_locks_lock = threading.Lock()


def now_local():
    return datetime.now(TZ)


# --- CACHE ---

def save_cache(prev_in_stock=None):
    try:
        with state_lock:
            data = json.loads(json.dumps(state))
        if prev_in_stock is not None:
            data["prev_in_stock"] = {k: list(v) for k, v in prev_in_stock.items()}
        with open(CACHE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"  Cache-skrivfel: {e}")


def load_cache():
    """Load cached state into state dict. Returns prev_in_stock dict for change detection."""
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
        with state_lock:
            state.update({k: v for k, v in data.items() if k != "prev_in_stock"})
            state["status"] = "ok (från cache — första koll pågår)"
        if "prev_in_stock" in data:
            prev = {k: set(v) for k, v in data["prev_in_stock"].items()}
        else:
            # Bakåtkompatibilitet med gamla cache-filer
            prev = {p["npl_pack_id"]: {ph["name"] for ph in p.get("pharmacies", [])}
                    for p in data.get("products", [])}
        print(f"Cache laddad: {data.get('last_check', '?')}, {len(prev)} produkters tillstånd")
        return prev
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"Cache-läsfel: {e}")
        return {}


def _save_pharmacy_cache(pharmacy_map):
    try:
        from db import get_db
        with get_db() as db:
            db.execute(
                "INSERT INTO pharmacy_cache (id, data, saved_at) VALUES (1, ?, datetime('now')) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data, saved_at=excluded.saved_at",
                [json.dumps(pharmacy_map)],
            )
            db.commit()
    except Exception as e:
        print(f"  Apotekscache-skrivfel: {e}")


def _load_pharmacy_cache():
    try:
        from db import get_db
        with get_db() as db:
            row = db.execute("SELECT data, saved_at FROM pharmacy_cache WHERE id=1").fetchone()
        if row:
            data = json.loads(row["data"])
            print(f"Apoteksregister laddad från DB-cache ({len(data)} apotek, sparat {row['saved_at'][:10]})")
            return data
    except Exception as e:
        print(f"  Apotekscache-läsfel: {e}")
    return {}


def fetch_all_pharmacies():
    """Fetch all authorized Swedish pharmacies from Läkemedelsverket's open API."""
    pharmacy_map = {}
    page, page_size = 0, 200
    while True:
        url = f"https://www.lakemedelsverket.se/api/pharmacy/search?pageSize={page_size}&pageIndex={page}"
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; varfinnsdet/1.0)",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        docs = data.get("documents", [])
        for d in docs:
            gln = d.get("glnCode")
            if gln:
                street = d.get("streetAddress", "")
                postal = d.get("postalcode", "")
                city   = d.get("city", "")
                addr = f"{street}, {postal} {city}".strip(", ")
                pharmacy_map[gln] = {
                    "name": d.get("name", gln),
                    "address": addr,
                    "postalcode": postal,
                    "city": city,
                }
        total   = data.get("totalMatching", 0)
        fetched = page * page_size + len(docs)
        if fetched >= total or not docs:
            break
        page += 1
    return pharmacy_map


# --- POLLING LOOP ---

def _get_subscription_products():
    """
    Return extra products from active subscriptions not already in PRODUCTS,
    or None if the lookup itself failed (e.g. a transient DB error) --
    callers must NOT treat None the same as "no active subscriptions right
    now", since polling_loop()'s pruning step keys off this list to decide
    what's no longer actively watched. Silently returning [] on error used
    to make every subscription-only medication look abandoned for that one
    cycle, wrongly evicting their fresh all_stock/_consecutive_zeros state
    even though the subscriptions are still fully active in the DB.
    """
    seed_ids = {p["npl_pack_id"] for p in PRODUCTS}
    try:
        from db import get_db
        with get_db() as db:
            rows = db.execute("""
                SELECT DISTINCT s.npl_pack_id, m.name
                FROM subscriptions s
                JOIN subscribers sub ON s.subscriber_id = sub.id
                LEFT JOIN medications m ON s.npl_pack_id = m.npl_pack_id
                WHERE s.active = 1 AND s.expires_at > datetime('now')
                  AND sub.confirmed_at IS NOT NULL AND sub.deleted_at IS NULL
            """).fetchall()
        return [
            {"name": r["name"] or r["npl_pack_id"], "npl_pack_id": r["npl_pack_id"]}
            for r in rows
            if r["npl_pack_id"] not in seed_ids
        ]
    except Exception as e:
        print(f"  _get_subscription_products fel: {e}")
        return None


def lock_for(npl_pack_id):
    """Per-ID lock guarding any Fass fetch keyed by npl_pack_id -- both
    get_stock_info()'s live check and routes/lakemedel.py's self-healing
    name lookup share this, so a burst of concurrent requests for the same
    never-before-seen medication serializes into one Fass call instead of
    a thundering herd. Entries are pruned in polling_loop() -- routes/
    lakemedel.py accepts any 14-digit id_slug, including ones that don't
    exist, so without pruning this grows one entry per distinct id ever
    requested (bots/crawlers included), not just the finite real catalog."""
    with _stock_fetch_locks_lock:
        entry = _stock_fetch_locks.get(npl_pack_id)
        if entry is None:
            entry = [threading.Lock(), time.time()]
            _stock_fetch_locks[npl_pack_id] = entry
        else:
            entry[1] = time.time()
        return entry[0]


def get_stock_info(npl_pack_id, sample_size=300):
    """
    Current stock for npl_pack_id, preferring the freshest source available:
    1. state["all_stock"] — populated every poll cycle for every actively
       polled product (hardcoded PRODUCTS + active subscriptions).
    2. A short-lived, per-process cache of a live sampled check (TTL =
       POLL_INTERVAL), used for medications nobody is actively polling yet.

    Returns {"pharmacies": [...], "checked_at": "YYYY-MM-DD HH:MM" or None,
    "source": "polled"|"live_cache"|"live"|"stale"|"none"}. Raises on a live
    check failure with no usable cache — callers decide how to degrade.
    """
    with state_lock:
        hit = state["all_stock"].get(npl_pack_id)
    if hit is not None and (time.time() - hit.get("checked_ts", 0)) >= STALE_AFTER:
        hit = None
    if hit is not None:
        return {"pharmacies": hit["pharmacies"], "checked_at": hit["checked_at"], "source": "polled"}

    ttl = POLL_INTERVAL
    cached = _live_stock_cache.get(npl_pack_id)
    if cached and (time.time() - cached[0]) < ttl:
        checked_at = datetime.fromtimestamp(cached[0], tz=TZ).strftime("%Y-%m-%d %H:%M")
        return {"pharmacies": cached[1], "checked_at": checked_at, "source": "live_cache"}

    with lock_for(npl_pack_id):
        # Re-check inside the lock — another thread may have just populated it
        # while we were waiting, in which case skip the redundant Fass call.
        cached = _live_stock_cache.get(npl_pack_id)
        if cached and (time.time() - cached[0]) < ttl:
            checked_at = datetime.fromtimestamp(cached[0], tz=TZ).strftime("%Y-%m-%d %H:%M")
            return {"pharmacies": cached[1], "checked_at": checked_at, "source": "live_cache"}

        pharmacy_map = _pharmacy_map
        if not pharmacy_map:
            return {"pharmacies": [], "checked_at": None, "source": "none"}
        sample_glns = list(pharmacy_map.keys())[:sample_size]
        try:
            pharmacies, _ = check_stock(npl_pack_id, sample_glns, pharmacy_map)
        except Exception:
            if cached:
                checked_at = datetime.fromtimestamp(cached[0], tz=TZ).strftime("%Y-%m-%d %H:%M")
                return {"pharmacies": cached[1], "checked_at": checked_at, "source": "stale"}
            raise

        checked_ts = time.time()
        _live_stock_cache[npl_pack_id] = (checked_ts, pharmacies)
        checked_at = datetime.fromtimestamp(checked_ts, tz=TZ).strftime("%Y-%m-%d %H:%M")
        return {"pharmacies": pharmacies, "checked_at": checked_at, "source": "live"}


def polling_loop(prev_in_stock):
    while True:
        t0 = time.time()
        now = now_local()

        pharmacy_map = _pharmacy_map
        gln_codes = list(pharmacy_map.keys())

        # Merge hardcoded PRODUCTS with active subscription medications.
        # None (lookup failed) must not be treated as "no subscriptions" --
        # see _get_subscription_products' docstring. subscription_lookup_ok
        # gates the active_ids-based pruning below.
        extra = _get_subscription_products()
        subscription_lookup_ok = extra is not None
        extra = extra or []
        all_products = PRODUCTS + extra
        curated_ids = {p["npl_pack_id"] for p in PRODUCTS}

        # PRODUCTS entries carry curation data only (npl_pack_id) -- no
        # display name. One combined DB lookup for ALL actively-polled ids
        # (curated + subscription-only) turns all_products into enriched
        # dicts carrying "name" from the medications table, same as any
        # other catalogue product. Falls back to the raw id if the row is
        # still a name==npl_pack_id placeholder
        # (e.g. right after a fresh deploy, before the daily catalogue sync
        # or a self-healing lookup has filled in the real name) -- logging/
        # notifications below must never crash on this, they'll just show
        # the raw id for a cycle or two.
        names_by_id = {}
        all_ids = [p["npl_pack_id"] for p in all_products]
        if all_ids:
            try:
                from db import get_db
                placeholders = ",".join("?" for _ in all_ids)
                with get_db() as db:
                    rows = db.execute(
                        f"SELECT npl_pack_id, name FROM medications WHERE npl_pack_id IN ({placeholders})",
                        all_ids,
                    ).fetchall()
                names_by_id = {r["npl_pack_id"]: r["name"] for r in rows}
            except Exception as e:
                print(f"  Namnuppslagning fel: {e}")
        all_products = [
            {**p, "name": names_by_id.get(p["npl_pack_id"], p["npl_pack_id"])}
            for p in all_products
        ]

        print(f"\n[{now:%Y-%m-%d %H:%M:%S}] Kollar {len(gln_codes)} apotek, "
              f"{len(PRODUCTS)} fasta + {len(extra)} via prenumeration (parallellt)...")

        def check_one(product):
            try:
                pharmacies, failed_glns = check_stock(product["npl_pack_id"], gln_codes, pharmacy_map)
                return product, pharmacies, None, failed_glns
            except Exception as e:
                return product, [], str(e), 0

        with ThreadPoolExecutor(max_workers=max(len(all_products), 1)) as executor:
            future_map = {}
            for i, p in enumerate(all_products):
                # Stagger submission instead of firing every product's check
                # at Fass simultaneously -- an experiment (2026-07-23) to see
                # whether that burst of concurrent requests contributes to
                # the "X/Y apotek kunde inte kollas" coverage failures logged
                # below, now that poll_log.glns_failed makes the effect
                # measurable instead of a guess. Cheap and reversible either
                # way: still fully parallel, just not all starting at once.
                if i:
                    time.sleep(STAGGER_SECONDS)
                future_map[executor.submit(check_one, p)] = p
            result_map = {}
            for future in as_completed(future_map):
                product, pharmacies, error, failed_glns = future.result()
                result_map[product["npl_pack_id"]] = (product, pharmacies, error, failed_glns)

        newly_available = []
        currently_in_stock = []
        updated_products = []
        all_stock_updates = {}
        checked_at_str = now.strftime("%Y-%m-%d %H:%M")

        for product in all_products:
            npl_pack_id = product["npl_pack_id"]
            p, pharmacies, error, failed_glns = result_map[npl_pack_id]
            name = p["name"]
            if error:
                print(f"  {name}: FEL — {error}")
                if npl_pack_id in curated_ids:
                    updated_products.append({**product, "pharmacies": [], "error": error})
            else:
                # Populate for ALL actively-polled products (not just the
                # hardcoded PRODUCTS), so get_stock_info()'s fast path also
                # covers subscription-only medications — exactly the ones
                # that qualify as SEO-indexable, per db.is_medication_indexable.
                all_stock_updates[npl_pack_id] = {
                    "pharmacies": pharmacies, "checked_at": checked_at_str, "checked_ts": time.time(),
                }
                current_glns = {ph["name"] for ph in pharmacies}
                prev_glns = prev_in_stock.get(npl_pack_id)  # None = aldrig sedd, set() = känt restnoterad

                if pharmacies:
                    _consecutive_zeros.pop(npl_pack_id, None)
                    # prev_glns is None means first poll for this product — establish baseline silently
                    if prev_glns is not None and not prev_glns:
                        # Confirmed-out -> now seeing stock. Require 2
                        # consecutive positive polls before treating this as
                        # a genuine restock, symmetric with the "2 consecutive
                        # zeros" rule below -- a single-poll blip otherwise
                        # triggers a notification for a restock that doesn't
                        # actually last.
                        positives = _consecutive_positives.get(npl_pack_id, 0) + 1
                        if positives >= MIN_CONSECUTIVE_POLLS:
                            _consecutive_positives.pop(npl_pack_id, None)
                            newly_available.append((name, pharmacies, npl_pack_id))
                            currently_in_stock.append((name, pharmacies, npl_pack_id))
                            prev_in_stock[npl_pack_id] = current_glns
                        else:
                            _consecutive_positives[npl_pack_id] = positives
                    else:
                        currently_in_stock.append((name, pharmacies, npl_pack_id))
                        prev_in_stock[npl_pack_id] = current_glns
                else:
                    # Require 2 consecutive zeros before clearing prev_in_stock.
                    # A single failed/empty poll won't reset the "seen in stock" state.
                    zeros = _consecutive_zeros.get(npl_pack_id, 0) + 1
                    _consecutive_zeros[npl_pack_id] = zeros
                    _consecutive_positives.pop(npl_pack_id, None)
                    if zeros >= MIN_CONSECUTIVE_POLLS:
                        prev_in_stock[npl_pack_id] = set()

                print(f"  {name}: {len(pharmacies)} i lager")
                if npl_pack_id in curated_ids:
                    updated_products.append({**product, "pharmacies": pharmacies, "error": None})

        notified_ids = {nid for _, _, nid in newly_available}
        _log_poll(now, all_products, result_map, notified_ids, len(gln_codes))

        # Check every currently-in-stock product for subscribers who still
        # haven't been successfully notified -- not just ones newly
        # transitioning this cycle (see _notify_subscribers' docstring for why).
        for name, pharmacies, npl_pack_id in currently_in_stock:
            _notify_subscribers(npl_pack_id, name, pharmacies, checked_at_str)

        _send_renewal_reminders()
        _cleanup_old_tokens()
        _cleanup_expired_subscriptions()
        _refresh_national_shortage_catalog()

        elapsed = time.time() - t0
        sleep_time = max(0, POLL_INTERVAL - elapsed)
        next_check = datetime.fromtimestamp(time.time() + sleep_time, tz=TZ)

        # Prune per-product state for anything no longer actively polled.
        # _consecutive_zeros, state["all_stock"] and _live_stock_cache are
        # all keyed by npl_pack_id and otherwise grow for the entire process
        # lifetime -- every medication ever searched or subscribed to, never
        # trimmed back down once it drops out of PRODUCTS/subscriptions.
        active_ids = {p["npl_pack_id"] for p in all_products}

        with state_lock:
            state["status"] = "ok"
            state["last_check"] = now.strftime("%Y-%m-%d %H:%M:%S")
            state["next_check"] = next_check.strftime("%H:%M:%S")
            state["polls_done"] += 1
            state["products"] = updated_products
            state["all_stock"].update(all_stock_updates)
            if subscription_lookup_ok:
                for stale_id in set(state["all_stock"]) - active_ids:
                    del state["all_stock"][stale_id]

        if subscription_lookup_ok:
            for stale_id in set(_consecutive_zeros) - active_ids:
                _consecutive_zeros.pop(stale_id, None)
            for stale_id in set(_consecutive_positives) - active_ids:
                _consecutive_positives.pop(stale_id, None)

        # _live_stock_cache serves ad-hoc lookups for products that were
        # searched but never subscribed to (not in active_ids at all) -- age
        # out anything old enough that its own TTL logic would refetch it
        # anyway, so an abandoned one-off search doesn't linger forever.
        now_ts = time.time()
        for stale_id in [k for k, (ts, _) in list(_live_stock_cache.items()) if now_ts - ts > STALE_AFTER]:
            _live_stock_cache.pop(stale_id, None)

        # _stock_fetch_locks isn't scoped to active_ids at all (it also
        # guards routes/lakemedel.py's self-healing lookup for arbitrary
        # 14-digit ids, valid or not) -- only prune locks old enough AND
        # currently unheld, so an in-progress request's lock is never
        # pulled out from under it.
        with _stock_fetch_locks_lock:
            stale_locks = [
                k for k, (lock, ts) in _stock_fetch_locks.items()
                if now_ts - ts > STALE_AFTER and not lock.locked()
            ]
            for stale_id in stale_locks:
                del _stock_fetch_locks[stale_id]

        # fass.py's own search/packages caches and per-key locks have the
        # same unbounded-growth shape, one entry per distinct query string
        # or medication id ever requested.
        fass.prune_caches()

        save_cache(prev_in_stock)
        print(f"  Koll tog {elapsed:.0f}s, sover {sleep_time:.0f}s till nästa")
        time.sleep(sleep_time)


def _notify_subscribers(npl_pack_id, medication_name, pharmacies, checked_at):
    """Called every poll cycle a product is in stock (not just the cycle a
    restock transition is first detected) -- the SQL below only ever selects
    subscriptions with last_notified_at IS NULL, so this is a cheap no-op
    once everyone's been notified. That's what makes a failed send (e.g. the
    daily mail cap) a genuine retry-next-cycle instead of a silent,
    permanent miss: gating solely on the transition meant a cap-hit on the
    very cycle the product restocked left last_notified_at NULL forever,
    since prev_in_stock had already flipped to "seen in stock" and the
    transition (the only thing that used to trigger this function) never
    fires again until the product goes out of stock and back in."""
    try:
        import mail
        from db import get_db, get_medication, get_or_create_token, utcnow_str
        from slugs import medication_url as build_medication_url
    except ImportError:
        return

    if NOTIFICATIONS_PAUSED:
        # Temporary kill switch (see module docstring). The state machine
        # above (prev_in_stock/_consecutive_*) keeps running regardless of
        # this flag, so every poll cycle re-qualifies the same "due"
        # subscribers -- if we just skip the send and leave last_notified_at
        # untouched, they all pile up and blast out as one burst the moment
        # this is flipped back off (this happened for real the last time
        # this switch was toggled). Consume the due notification instead --
        # stamp last_notified_at now, without sending -- so nothing
        # accumulates. This does mean a genuine restock during the pause
        # window goes unnotified, which is the intended trade-off while
        # sends are paused.
        try:
            cooldown_cutoff = utcnow_str(timedelta(hours=-NOTIFY_COOLDOWN_HOURS))
            with get_db() as db:
                db.execute("""
                    UPDATE subscriptions SET last_notified_at = datetime('now')
                    WHERE id IN (
                        SELECT s.id
                        FROM subscriptions s
                        JOIN subscribers sub ON s.subscriber_id = sub.id
                        WHERE s.npl_pack_id = ? AND s.active = 1
                          AND sub.confirmed_at IS NOT NULL AND sub.deleted_at IS NULL
                          AND s.expires_at > datetime('now')
                          AND (s.last_notified_at IS NULL OR s.last_notified_at < ?)
                    )
                """, [npl_pack_id, cooldown_cutoff])
                db.commit()
        except Exception as e:
            print(f"  _notify_subscribers (pausad, konsumerar) fel: {e}")
        return

    try:
        cooldown_cutoff = utcnow_str(timedelta(hours=-NOTIFY_COOLDOWN_HOURS))
        with get_db() as db:
            # Build the deep link via the same shared helper routes/lakemedel.py
            # uses for its canonical slug, so the emailed URL matches the
            # canonical one and never needs a redirect.
            # Skip the link entirely if the row is still a name==npl_pack_id
            # placeholder -- fass.lookup_name() can't resolve a package-level
            # id (confirmed live: Fass's package/{id} endpoint only accepts
            # product-level ids), so a link built from a placeholder name
            # would just 404 for the recipient instead of not being there.
            medication_url = None
            med = get_medication(db, npl_pack_id)
            if SITE_URL and med and med["name"] != npl_pack_id:
                medication_url = build_medication_url(SITE_URL, npl_pack_id, med["name"], med["strength"], med["form"])

            subs = db.execute("""
                SELECT s.id, s.expires_at, s.last_notified_at, sub.email, sub.id AS sub_id
                FROM subscriptions s
                JOIN subscribers sub ON s.subscriber_id = sub.id
                WHERE s.npl_pack_id = ? AND s.active = 1
                  AND sub.confirmed_at IS NOT NULL AND sub.deleted_at IS NULL
                  AND s.expires_at > datetime('now')
                  AND (s.last_notified_at IS NULL OR s.last_notified_at < ?)
            """, [npl_pack_id, cooldown_cutoff]).fetchall()

            for sub in subs:
                unsub_token = get_or_create_token(db, "unsubscribe", sub["sub_id"], sub["id"])
                manage_token = get_or_create_token(db, "manage", sub["sub_id"], None)
                db.commit()

                try:
                    sent = mail.send_notification(
                        sub["email"], medication_name, pharmacies,
                        unsub_token, manage_token, sub["expires_at"], SITE_URL,
                        medication_url=medication_url, checked_at=checked_at,
                    )
                except Exception as e:
                    sent = False
                    print(f"  Notismejl till {sub['email']} misslyckades: {e}")

                if sent:
                    db.execute(
                        "UPDATE subscriptions SET last_notified_at=datetime('now') WHERE id=?",
                        [sub["id"]],
                    )
                    db.commit()
                else:
                    # send_notification returns False (daily mail cap reached,
                    # no exception) as well as raising on a hard failure --
                    # leave last_notified_at untouched either way so the next
                    # poll cycle retries instead of silently losing this
                    # subscriber's one-time restock notification for good.
                    print(f"  Notismejl till {sub['email']} inte skickat -- försöker igen nästa pollning")
    except Exception as e:
        print(f"  _notify_subscribers fel: {e}")


def _send_renewal_reminders():
    try:
        import mail
        from db import get_db, create_token, get_or_create_token
    except ImportError:
        return

    try:
        with get_db() as db:
            subs = db.execute("""
                SELECT s.id, s.expires_at, sub.email, sub.id AS sub_id
                FROM subscriptions s
                JOIN subscribers sub ON s.subscriber_id = sub.id
                WHERE s.active = 1 AND sub.confirmed_at IS NOT NULL AND sub.deleted_at IS NULL
                  AND s.expires_at BETWEEN datetime('now', '+5 days') AND datetime('now', '+6 days')
                  AND NOT EXISTS (
                    SELECT 1 FROM tokens
                    WHERE type = 'extend' AND subscription_id = s.id
                      AND created_at > datetime('now', '-1 day')
                  )
            """).fetchall()

            for sub in subs:
                extend_token = create_token(db, "extend", sub["sub_id"], sub["id"], ttl_hours=7 * 24)
                manage_token = get_or_create_token(db, "manage", sub["sub_id"], None)

                try:
                    sent = mail.send_renewal_reminder(
                        sub["email"], sub["expires_at"], extend_token, manage_token, SITE_URL,
                    )
                except Exception as e:
                    sent = False
                    print(f"  Förlängningsmejl till {sub['email']} misslyckades: {e}")

                if sent:
                    # Only now persist the extend token -- its created_at is
                    # what the NOT EXISTS guard above uses to skip this
                    # subscription for the next 24h. Since the reminder
                    # window is only a day wide, committing it before a
                    # confirmed send would burn this subscriber's one chance
                    # to ever get reminded before expiry if the send failed
                    # (e.g. daily mail cap reached).
                    db.commit()
                else:
                    db.rollback()
                    print(f"  Förlängningsmejl till {sub['email']} inte skickat -- försöker igen nästa pollning")
    except Exception as e:
        print(f"  _send_renewal_reminders fel: {e}")


def _cleanup_old_tokens():
    try:
        from db import get_db, cleanup_old_tokens
        with get_db() as db:
            cleanup_old_tokens(db)
            db.commit()
    except Exception as e:
        print(f"  _cleanup_old_tokens fel: {e}")


def _cleanup_expired_subscriptions():
    try:
        from db import cleanup_expired_subscriptions, get_db
        with get_db() as db:
            cleanup_expired_subscriptions(db)
            db.commit()
    except Exception as e:
        print(f"  _cleanup_expired_subscriptions fel: {e}")


def _refresh_national_shortage_catalog():
    """Daily (not per-cycle) refresh of the Fas 3 national shortage
    catalogue -- broad restsituation data covering the whole Läkemedelsverket
    feed, not just the polled PRODUCTS above. Gated to at most once per day
    inside national_shortages.refresh_national_shortages_if_due() itself,
    since the underlying feed is a ~19MB daily snapshot and must not be
    re-fetched every POLL_INTERVAL cycle. Deliberately separate from (and
    never feeds into) all_products/_get_subscription_products() -- this only
    surfaces national forecast data, never live pharmacy stock."""
    try:
        import national_shortages
        national_shortages.refresh_national_shortages_if_due()
    except Exception as e:
        print(f"  _refresh_national_shortage_catalog fel: {e}")


def _log_poll(polled_at, all_products, result_map, notified_ids, total_glns):
    try:
        from db import get_db
        ts = polled_at.strftime("%Y-%m-%dT%H:%M:%S")
        with get_db() as db:
            for product in all_products:
                npl = product["npl_pack_id"]
                _, pharmacies, error, failed_glns = result_map.get(npl, (None, [], None, 0))
                if error:
                    continue
                db.execute(
                    "INSERT INTO poll_log (polled_at, npl_pack_id, name, pharmacy_count, "
                    "glns_checked, glns_failed, notified) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [ts, npl, product["name"], len(pharmacies), total_glns, failed_glns,
                     1 if npl in notified_ids else 0],
                )
            # Keep POLL_LOG_RETENTION_DAYS of history -- see config.py for why
            # this is time-based rather than a row-count cap.
            cutoff = (polled_at - timedelta(days=POLL_LOG_RETENTION_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
            db.execute("DELETE FROM poll_log WHERE polled_at < ?", [cutoff])
            db.commit()
    except Exception as e:
        print(f"  Poll-loggfel: {e}")


def start_polling():
    """Start the pharmacy fetch + polling loop. Called from app.py as a daemon thread."""
    global _pharmacy_map

    if not os.getenv("RESEND_API_KEY"):
        print("OBS: RESEND_API_KEY saknas — inga mail skickas")

    prev_in_stock = load_cache()

    # Load pharmacy registry from DB cache for immediate start (no waiting message)
    cached = _load_pharmacy_cache()
    if cached:
        _pharmacy_map = cached
        with state_lock:
            state["status"] = "ok (apoteksregister från cache)"
    else:
        print("Ingen apotekscache — hämtar från Läkemedelsverket...")
        with state_lock:
            state["status"] = "Startar — hämtar apoteksregister..."
        while not _pharmacy_map:
            try:
                _pharmacy_map = fetch_all_pharmacies()
                _save_pharmacy_cache(_pharmacy_map)
            except Exception as e:
                print(f"  Apotekshämtning misslyckades ({e}) — försöker om 30s")
                time.sleep(30)
        print(f"Hittade {len(_pharmacy_map)} apotek i Sverige (Läkemedelsverket)")

    print(f"Pollar var {POLL_INTERVAL // 60} minut(er)\n")

    # Refresh pharmacy registry in background thread (keeps cache fresh without blocking)
    def _refresh_pharmacies():
        try:
            fresh = fetch_all_pharmacies()
            if fresh:
                global _pharmacy_map
                _pharmacy_map = fresh
                _save_pharmacy_cache(fresh)
                print(f"  Apoteksregister uppdaterat: {len(fresh)} apotek")
        except Exception as e:
            print(f"  Apoteksregister-uppdatering misslyckades: {e}")

    if cached:
        t = threading.Thread(target=_refresh_pharmacies, daemon=True, name="pharmacy-refresh")
        t.start()

    polling_loop(prev_in_stock)
