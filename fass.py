"""
Fass.se API helpers.

All calls go through the fass.se reverse-proxy:
  https://fass.se/api/content?endpoint=<url-encoded-cms-url>

The CMS base is https://cms.fass.se/api/vard/
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

FASS_REFERER = "https://fass.se/health/product/20011130000246/stock-status"
IN_STOCK_STATUSES = {"IN_STOCK", "FEW_IN_STOCK"}

# Simple in-memory search cache (query → (timestamp, results))
_search_cache: dict = {}
_CACHE_TTL = 300  # seconds


def _proxy_get(path):
    encoded = urllib.parse.quote(f"https://cms.fass.se/api/vard/{path}", safe="")
    req = urllib.request.Request(
        f"https://fass.se/api/content?endpoint={encoded}",
        headers={"Referer": FASS_REFERER},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _proxy_post(path, body):
    encoded = urllib.parse.quote(f"https://cms.fass.se/api/vard/{path}", safe="")
    req = urllib.request.Request(
        f"https://fass.se/api/content?endpoint={encoded}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Referer": FASS_REFERER},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def search_medications(query):
    """
    Search for medications by name. Returns list of:
      {"npl_id": str, "name": str, "form": str}

    Uses fass.se CMS full-search (same proxy as stock checks).
    Merges with local DB so seeded medications always appear.
    """
    q = query.strip()
    if not q or len(q) < 2:
        return []

    cached = _search_cache.get(q)
    if cached and (time.time() - cached[0]) < _CACHE_TTL:
        return cached[1]

    results = _fass_search(q)

    db_results = _db_search(q)
    seen_ids = {r["npl_id"] for r in results}
    for r in db_results:
        if r["npl_id"] not in seen_ids:
            results.append(r)
            seen_ids.add(r["npl_id"])

    _search_cache[q] = (time.time(), results)
    return results


def _fass_search(q):
    """Search via fass.se CMS endpoint: vard/full-search/{term}."""
    try:
        data = _proxy_get(f"full-search/{urllib.parse.quote(q)}")
        hits = data.get("human-product-index", {}).get("hits", [])
        results = []
        for hit in hits[:15]:
            obj = hit.get("object") or {}
            npl_id = obj.get("nplId") or hit.get("id", "")
            trade = obj.get("tradeName", "")
            strength = obj.get("strength", "")
            form = obj.get("doseForm", "")
            if not (npl_id and trade):
                continue
            name = f"{trade} {strength}".strip() if strength else trade
            results.append({"npl_id": str(npl_id), "name": name, "form": form})
        return results
    except Exception as e:
        print(f"  fass full-search error: {e}")
        return []


def _db_search(q):
    """Search seeded medications in local DB (case-insensitive LIKE)."""
    try:
        from db import get_db
        with get_db() as db:
            rows = db.execute(
                "SELECT npl_pack_id, name, strength, form FROM medications "
                "WHERE name LIKE ? ORDER BY name LIMIT 10",
                [f"%{q}%"],
            ).fetchall()
        return [
            {"npl_id": r["npl_pack_id"], "name": r["name"], "form": r["form"] or ""}
            for r in rows
        ]
    except Exception:
        return []


def lookup_name(npl_pack_id):
    """Best-effort live lookup of a medication's real name from Fass, for
    when a medications row is missing/placeholder and we only have the
    14-digit npl_pack_id (e.g. no ?name= was ever supplied)."""
    try:
        # Strategy 1: search by the npl_pack_id directly (works if Fass indexes by ID)
        results = _fass_search(npl_pack_id)
        if results:
            return results[0]["name"]
    except Exception:
        pass
    try:
        # Strategy 2: call the package endpoint with npl_pack_id as npl_id
        # (returns list; items may include doseForm/tradeName)
        data = _proxy_get(f"package/{npl_pack_id}")
        items = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        for item in items:
            trade = item.get("tradeName") or item.get("name") or ""
            strength = item.get("strength") or ""
            if trade:
                return f"{trade} {strength}".strip() if strength else trade
    except Exception:
        pass
    return None


def get_packages(npl_id):
    """
    Get all available packagings for a medication (identified by nplId).
    Returns list of:
      {"npl_pack_id": str, "name": str, "form": str, "shortage": bool}
    """
    try:
        items = _proxy_get(f"package/{npl_id}?isParallellImported=false")
    except Exception as e:
        print(f"  fass packages error for {npl_id}: {e}")
        return []

    packages = []
    for item in (items if isinstance(items, list) else []):
        if not item.get("isOnTheMarket", False):
            continue
        self_url = item.get("links", {}).get("selfUrl", "")
        pack_id = self_url[len("package/"):] if self_url.startswith("package/") else ""
        if not pack_id:
            continue
        form = item.get("doseForm", "")
        container = item.get("container", "")
        qty = item.get("quantity", "")
        name_parts = [str(qty), container] if qty and container else [container or str(qty)]
        name = f"{form} ({', '.join(p for p in name_parts if p)})" if name_parts[0] else form
        packages.append({
            "npl_pack_id": pack_id,
            "name": name or pack_id,
            "form": form,
            "shortage": bool(item.get("medicinalShortage")),
        })
    return packages


def check_stock(npl_pack_id, gln_codes, pharmacy_map):
    """
    Check stock for nplPackId across a list of GLN codes.
    Returns list of in-stock pharmacies:
      {"name": str, "address": str, "postalcode": str, "gln": str, "status": str, "exchangeable": bool}

    Batches GLN codes in groups of 50. Retries up to 3 times on transient
    errors; 400s are broken into sub-batches of 10 (unknown GLNs).
    Logs coverage so silent data loss is visible in logs.
    """
    results = []
    failed_glns = 0

    for i in range(0, len(gln_codes), 50):
        batch = gln_codes[i:i + 50]
        ok = False

        for attempt in range(3):
            try:
                data = _proxy_post(f"pharmacy/stock/{npl_pack_id}", batch)
                results.extend(data)
                ok = True
                break
            except Exception as e:
                if getattr(e, "code", None) == 400:
                    # Some GLNs unknown to Fass — retry in sub-batches of 10
                    for j in range(0, len(batch), 10):
                        sub = batch[j:j + 10]
                        sub_ok = False
                        for sub_attempt in range(2):
                            try:
                                results.extend(_proxy_post(f"pharmacy/stock/{npl_pack_id}", sub))
                                sub_ok = True
                                break
                            except Exception:
                                time.sleep(0.3)
                        if not sub_ok:
                            failed_glns += len(sub)
                        time.sleep(0.1)
                    ok = True
                    break
                # Transient error — back off and retry
                time.sleep(0.5 * (attempt + 1))

        if not ok:
            failed_glns += len(batch)
        time.sleep(0.2)

    if failed_glns:
        pct = failed_glns / len(gln_codes) * 100
        print(f"  VARNING {npl_pack_id}: {failed_glns}/{len(gln_codes)} "
              f"({pct:.0f}%) apotek kunde inte kollas — siffran är underskattad")

    in_stock = []
    for r in results:
        if r.get("stockInformation") in IN_STOCK_STATUSES:
            ph = pharmacy_map.get(r["glnCode"], {})
            in_stock.append({
                "name": ph.get("name", r["glnCode"]),
                "address": ph.get("address", ""),
                "postalcode": ph.get("postalcode", ""),
                "gln": r["glnCode"],
                "status": r["stockInformation"],
                "exchangeable": r.get("exchangeableProductInStock", False),
            })
    return in_stock
