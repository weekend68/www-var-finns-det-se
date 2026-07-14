"""National shortage forecast data from Läkemedelsverkets restsituationsregister.

This is a COMPLEMENT to our own polling-based history (see routes/lakemedel.py's
_stock_history()): our polling can only ever say what HAS been in/out of stock
according to our own measurements, never what's forecasted going forward. This
module surfaces Läkemedelsverket's own national shortage registration + forecast
instead, for the handful of tracked medications currently in it.

The data itself is a manually-refreshed snapshot (shortage_data.json, repo root),
not live -- see scripts/refresh_shortage_snapshot.py for why and how it's updated.
"""

import json
import os

_SNAPSHOT_PATH = os.path.join(os.path.dirname(__file__), "shortage_data.json")

_snapshot = None  # module-level cache, populated on first load_snapshot() call


def load_snapshot():
    """Load and cache shortage_data.json. Safe to call repeatedly -- only reads
    the file once. Missing/corrupt file degrades to an empty snapshot instead
    of crashing the app (this is a nice-to-have complement, not core data)."""
    global _snapshot
    if _snapshot is not None:
        return _snapshot

    try:
        with open(_SNAPSHOT_PATH, encoding="utf-8") as f:
            _snapshot = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"  shortage.py: kunde inte läsa {_SNAPSHOT_PATH}: {e}")
        _snapshot = {"medicines": {}}

    return _snapshot


def get_shortage_info(npl_pack_id):
    """Return the shortage dict for this npl_pack_id, or None if it's not
    currently registered as a shortage in the snapshot."""
    snapshot = load_snapshot()
    return snapshot.get("medicines", {}).get(npl_pack_id)
