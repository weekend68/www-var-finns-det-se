"""
Medicinstatus polling engine — polls fass.se for stock status.
Runs as a daemon thread inside the Flask/Gunicorn process (started from app.py).

Optional env vars:
  NOTIFY_EMAIL      — legacy direct-notification recipient
  RESEND_API_KEY    — API-nyckel från resend.com (gratis, 100 mail/dag)
  POLL_INTERVAL     — minutes between checks (default: 2)
  CACHE_FILE        — path for persistent state cache (default: /tmp/medicinstatus_cache.json)
  FROM_EMAIL        — sender address for Resend (default: noreply@medicinstatus.se)
"""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

from fass import check_stock, _proxy_post as fass_post

TZ = ZoneInfo("Europe/Stockholm")
CACHE_FILE = os.getenv("CACHE_FILE", "/data/medicinstatus_cache.json")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "2")) * 60
IN_STOCK_STATUSES = {"IN_STOCK", "FEW_IN_STOCK"}
SHOW_LIMIT = 10

# Populated by start_polling(); readable by routes for live stock checks
_pharmacy_map: dict = {}

# Tracks consecutive polls with 0 pharmacies per product.
# Requires 2 in a row before clearing prev_in_stock, to avoid false triggers
# caused by transient API failures.
_consecutive_zeros: dict = {}

PRODUCTS = [
    {"name": "Estradot 25 mcg depotplåster",         "npl_pack_id": "20040113100574"},
    {"name": "Estradot 37,5 mcg depotplåster",       "npl_pack_id": "20011130100489"},
    {"name": "Estradot 50 mcg depotplåster",         "npl_pack_id": "20011130100502"},
    {"name": "Estradot 75 mcg depotplåster",         "npl_pack_id": "20011130100526"},
    {"name": "Estradot 100 mcg depotplåster",        "npl_pack_id": "20011130100564"},
    {"name": "Estrogel transdermal gel 0,75 mg/dos", "npl_pack_id": "20181129100025"},
    {"name": "Lenzetto 1,53 mg/dos transdermal spray (1 × 56 dos)", "npl_pack_id": "20140320100036"},
    {"name": "Lenzetto 1,53 mg/dos transdermal spray (3 × 56 dos)", "npl_pack_id": "20160407100353"},
]


state = {
    "status": "Startar — hämtar apotekslista...",
    "last_check": None,
    "next_check": None,
    "polls_done": 0,
    "products": [{**p, "pharmacies": [], "error": None} for p in PRODUCTS],
}
state_lock = threading.Lock()


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
            "User-Agent": "Mozilla/5.0 (compatible; medicinstatus/1.0)",
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
                pharmacy_map[gln] = {"name": d.get("name", gln), "address": addr}
        total   = data.get("totalMatching", 0)
        fetched = page * page_size + len(docs)
        if fetched >= total or not docs:
            break
        page += 1
    return pharmacy_map


# --- EMAIL via Resend ---

def send_email(newly_available):
    api_key = os.environ["RESEND_API_KEY"]
    notify = os.environ["NOTIFY_EMAIL"]

    lines = []
    for product_name, pharmacies in newly_available:
        lines.append(f"\n{product_name} — {len(pharmacies)} apotek:")
        for ph in pharmacies:
            exch = " (utbytbar vara)" if ph["exchangeable"] else ""
            lines.append(f"  • {ph['name']}  [{ph['status']}]{exch}")

    body = (
        "Följande estradiolpreparat finns nu i lager:\n"
        + "\n".join(lines)
        + f"\n\nKontrollerat: {now_local().strftime('%Y-%m-%d %H:%M')} (svensk tid)\n"
        + "https://fass.se/health/product/20011130000246/stock-status"
    )

    payload = json.dumps({
        "from": os.getenv("FROM_EMAIL", "apoteksvakt@resend.dev"),
        "to": [notify],
        "subject": "🟢 Estradiol i lager på apotek!",
        "text": body,
    }).encode()

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; medicinstatus/1.0)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
        print(f"  Mail skickat till {notify} (id: {result.get('id')})")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"Resend {e.code}: {body}") from e


# --- POLLING LOOP ---

def _get_subscription_products():
    """
    Return extra products from active subscriptions not already in PRODUCTS.
    Safe to call even if DB is not initialised yet.
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
    except Exception:
        return []


def polling_loop(prev_in_stock):
    while True:
        t0 = time.time()
        now = now_local()

        pharmacy_map = _pharmacy_map
        gln_codes = list(pharmacy_map.keys())

        # Merge hardcoded PRODUCTS with active subscription medications
        extra = _get_subscription_products()
        all_products = PRODUCTS + extra

        print(f"\n[{now:%Y-%m-%d %H:%M:%S}] Kollar {len(gln_codes)} apotek, "
              f"{len(PRODUCTS)} fasta + {len(extra)} via prenumeration (parallellt)...")

        def check_one(product):
            try:
                pharmacies = check_stock(product["npl_pack_id"], gln_codes, pharmacy_map)
                return product, pharmacies, None
            except Exception as e:
                return product, [], str(e)

        with ThreadPoolExecutor(max_workers=max(len(all_products), 1)) as executor:
            future_map = {executor.submit(check_one, p): p for p in all_products}
            result_map = {}
            for future in as_completed(future_map):
                product, pharmacies, error = future.result()
                result_map[product["npl_pack_id"]] = (product, pharmacies, error)

        newly_available = []
        updated_products = []

        for product in all_products:
            npl_pack_id = product["npl_pack_id"]
            p, pharmacies, error = result_map[npl_pack_id]
            name = p["name"]
            if error:
                print(f"  {name}: FEL — {error}")
                if product in PRODUCTS:
                    updated_products.append({**product, "pharmacies": [], "error": error})
            else:
                current_glns = {ph["name"] for ph in pharmacies}
                prev_glns = prev_in_stock.get(npl_pack_id)  # None = aldrig sedd, set() = känt restnoterad

                if pharmacies:
                    _consecutive_zeros.pop(npl_pack_id, None)
                    # Alert only when previously confirmed out of stock (prev_glns == set())
                    # prev_glns is None means first poll for this product — establish baseline silently
                    if prev_glns is not None and not prev_glns:
                        newly_available.append((name, pharmacies, npl_pack_id))
                    prev_in_stock[npl_pack_id] = current_glns
                else:
                    # Require 2 consecutive zeros before clearing prev_in_stock.
                    # A single failed/empty poll won't reset the "seen in stock" state.
                    zeros = _consecutive_zeros.get(npl_pack_id, 0) + 1
                    _consecutive_zeros[npl_pack_id] = zeros
                    if zeros >= 2:
                        prev_in_stock[npl_pack_id] = set()

                print(f"  {name}: {len(pharmacies)} i lager")
                if product in PRODUCTS:
                    updated_products.append({**product, "pharmacies": pharmacies, "error": None})

        notified_ids = {nid for _, _, nid in newly_available}
        _log_poll(now, all_products, result_map, notified_ids, len(gln_codes))

        if newly_available:
            for name, pharmacies, npl_pack_id in newly_available:
                _notify_subscribers(npl_pack_id, name, pharmacies)

        _send_renewal_reminders()

        elapsed = time.time() - t0
        sleep_time = max(0, POLL_INTERVAL - elapsed)
        next_check = datetime.fromtimestamp(time.time() + sleep_time, tz=TZ)

        with state_lock:
            state["status"] = "ok"
            state["last_check"] = now.strftime("%Y-%m-%d %H:%M:%S")
            state["next_check"] = next_check.strftime("%H:%M:%S")
            state["polls_done"] += 1
            state["products"] = updated_products

        save_cache(prev_in_stock)
        print(f"  Koll tog {elapsed:.0f}s, sover {sleep_time:.0f}s till nästa")
        time.sleep(sleep_time)


def _notify_subscribers(npl_pack_id, medication_name, pharmacies):
    try:
        import mail
        from db import get_db, get_or_create_token
    except ImportError:
        return

    site_url = os.getenv("SITE_URL", "").rstrip("/")
    try:
        with get_db() as db:
            subs = db.execute("""
                SELECT s.id, s.expires_at, s.last_notified_at, sub.email, sub.id AS sub_id
                FROM subscriptions s
                JOIN subscribers sub ON s.subscriber_id = sub.id
                WHERE s.npl_pack_id = ? AND s.active = 1
                  AND sub.confirmed_at IS NOT NULL AND sub.deleted_at IS NULL
                  AND s.expires_at > datetime('now')
            """, [npl_pack_id]).fetchall()

            for sub in subs:
                if sub["last_notified_at"]:
                    last = datetime.fromisoformat(sub["last_notified_at"])
                    if (datetime.utcnow() - last).total_seconds() < 3600:
                        continue

                unsub_token = get_or_create_token(db, "unsubscribe", sub["sub_id"], sub["id"], ttl_hours=30 * 24)
                manage_token = get_or_create_token(db, "manage", sub["sub_id"], None, ttl_hours=30 * 24)
                db.commit()

                try:
                    mail.send_notification(
                        sub["email"], medication_name, pharmacies,
                        unsub_token, manage_token, sub["expires_at"], site_url,
                    )
                    db.execute(
                        "UPDATE subscriptions SET last_notified_at=datetime('now') WHERE id=?",
                        [sub["id"]],
                    )
                    db.commit()
                except Exception as e:
                    print(f"  Notismejl till {sub['email']} misslyckades: {e}")
    except Exception as e:
        print(f"  _notify_subscribers fel: {e}")


def _send_renewal_reminders():
    try:
        import mail
        from db import get_db, create_token, get_or_create_token
    except ImportError:
        return

    site_url = os.getenv("SITE_URL", "").rstrip("/")
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
                manage_token = get_or_create_token(db, "manage", sub["sub_id"], None, ttl_hours=30 * 24)
                db.commit()

                try:
                    mail.send_renewal_reminder(
                        sub["email"], sub["expires_at"], extend_token, manage_token, site_url,
                    )
                except Exception as e:
                    print(f"  Förlängningsmejl till {sub['email']} misslyckades: {e}")
    except Exception as e:
        print(f"  _send_renewal_reminders fel: {e}")


def _log_poll(polled_at, all_products, result_map, notified_ids, total_glns):
    try:
        from db import get_db
        ts = polled_at.strftime("%Y-%m-%dT%H:%M:%S")
        with get_db() as db:
            for product in all_products:
                npl = product["npl_pack_id"]
                _, pharmacies, error = result_map.get(npl, (None, [], None))
                if error:
                    continue
                db.execute(
                    "INSERT INTO poll_log (polled_at, npl_pack_id, name, pharmacy_count, "
                    "glns_checked, notified) VALUES (?, ?, ?, ?, ?, ?)",
                    [ts, npl, product["name"], len(pharmacies), total_glns,
                     1 if npl in notified_ids else 0],
                )
            # Keep rolling window of 2000 rows
            db.execute(
                "DELETE FROM poll_log WHERE id NOT IN "
                "(SELECT id FROM poll_log ORDER BY id DESC LIMIT 2000)"
            )
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
