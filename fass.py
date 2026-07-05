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

    Tries Fass CMS first; falls back to local DB (seeded medications).
    """
    q = query.strip()
    if not q or len(q) < 2:
        return []

    cached = _search_cache.get(q)
    if cached and (time.time() - cached[0]) < _CACHE_TTL:
        return cached[1]

    results = []
    try:
        data = _proxy_get(f"product/search?query={urllib.parse.quote(q)}&pageSize=15")
        results = _normalize_search(data, q)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"  fass search error: {e.code} for '{q}'")
    except Exception as e:
        print(f"  fass search error: {e} for '{q}'")

    # Always merge with DB results so seeded medications are always findable
    db_results = _db_search(q)
    seen_ids = {r["npl_id"] for r in results}
    for r in db_results:
        if r["npl_id"] not in seen_ids:
            results.append(r)
            seen_ids.add(r["npl_id"])

    _search_cache[q] = (time.time(), results)
    return results


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
            {
                "npl_id": r["npl_pack_id"],
                "name": r["name"],
                "form": r["form"] or "",
            }
            for r in rows
        ]
    except Exception:
        return []


def _normalize_search(data, query):
    """Normalize varying Fass API response shapes to a consistent list."""
    results = []

    # Shape 1: {"products": [...]}
    items = data if isinstance(data, list) else data.get("products", data.get("items", []))

    for item in items[:15]:
        npl_id = (
            item.get("nplId") or item.get("npl_id") or item.get("id") or ""
        )
        name = (
            item.get("productName") or item.get("name") or item.get("label") or ""
        )
        form = (
            item.get("pharmaceuticalForm") or item.get("form") or ""
        )
        if npl_id and name:
            results.append({"npl_id": str(npl_id), "name": name, "form": form})

    # If API returned nothing useful, log raw shape for debugging
    if not results and data:
        keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
        print(f"  fass search: no results for '{query}', response keys: {keys}")

    return results


def get_packages(npl_id):
    """
    Get all available packagings for a medication (identified by nplId).
    Returns list of:
      {"npl_pack_id": str, "name": str, "strength": str, "form": str}
    """
    try:
        data = _proxy_get(f"product/{npl_id}/packages")
    except Exception as e:
        print(f"  fass packages error for {npl_id}: {e}")
        return []

    packages = []
    items = data if isinstance(data, list) else data.get("packages", data.get("items", []))
    for item in items:
        pack_id = (
            item.get("nplPackId") or item.get("npl_pack_id") or item.get("id") or ""
        )
        name = item.get("productName") or item.get("name") or ""
        strength = item.get("strength") or item.get("dose") or ""
        form = item.get("pharmaceuticalForm") or item.get("form") or ""
        if pack_id:
            packages.append({
                "npl_pack_id": str(pack_id),
                "name": name or f"{npl_id} – {strength}",
                "strength": strength,
                "form": form,
            })
    return packages


def check_stock(npl_pack_id, gln_codes, pharmacy_map):
    """
    Check stock for nplPackId across a list of GLN codes.
    Returns list of in-stock pharmacies:
      {"name": str, "address": str, "status": str, "exchangeable": bool}

    Batches GLN codes in groups of 50; retries 400s in sub-batches of 10
    (LMV has pharmacies that Fass doesn't recognize).
    """
    results = []
    for i in range(0, len(gln_codes), 50):
        batch = gln_codes[i:i + 50]
        try:
            data = _proxy_post(f"pharmacy/stock/{npl_pack_id}", batch)
            results.extend(data)
        except Exception as e:
            if getattr(e, "code", None) == 400:
                for j in range(0, len(batch), 10):
                    sub = batch[j:j + 10]
                    try:
                        results.extend(_proxy_post(f"pharmacy/stock/{npl_pack_id}", sub))
                    except Exception:
                        pass
                    time.sleep(0.1)
            else:
                print(f"  Fass batchfel (offset {i}): {e}")
        time.sleep(0.2)

    in_stock = []
    for r in results:
        if r.get("stockInformation") in IN_STOCK_STATUSES:
            ph = pharmacy_map.get(r["glnCode"], {})
            in_stock.append({
                "name": ph.get("name", r["glnCode"]),
                "address": ph.get("address", ""),
                "status": r["stockInformation"],
                "exchangeable": r.get("exchangeableProductInStock", False),
            })
    return in_stock
