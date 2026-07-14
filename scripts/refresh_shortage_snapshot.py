"""
Regenerate shortage_data.json from Läkemedelsverket's medicine-shortage feed.

Fetches directly from the confirmed, public, unauthenticated open-data
endpoint (no session/cookie required, verified working):

  https://docetp.mpa.se/LMF/Reports/opendata-medicine-shortages-current-3-0.xml

That page's own site (lakemedelsverket.se) is too JS-heavy to browse
programmatically, but this docetp.mpa.se endpoint underneath it is a plain,
directly-fetchable static file, updated daily.

Usage:
  python scripts/refresh_shortage_snapshot.py                  # fetch live
  python scripts/refresh_shortage_snapshot.py path/to/local.xml # use a local file instead (offline/testing)

Then commit the updated shortage_data.json.

Only extracts entries for npl_pack_ids in checker.PRODUCTS -- this is the
Fas 2 scope (actively-bevakade läkemedel only), not the full national
catalogue (that's Fas 3, which would ingest the whole feed).

Not yet wired into a scheduled/cron job -- run by hand for now. Wiring this
into automatic periodic refresh is a Fas 3 decision (how often, where it
runs, whether it's worth adding to the always-on polling process or a
separate lightweight job).
"""

import json
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

SHORTAGE_FEED_URL = "https://docetp.mpa.se/LMF/Reports/opendata-medicine-shortages-current-3-0.xml"
NS = "http://eservices.lakemedelsverket.se/opendata/medicineshortage/v3/"


def _tag(name):
    return f"{{{NS}}}{name}"


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; varfinnsdet/1.0)"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def extract(xml_source, tracked_ids):
    """xml_source: a file path (str) or raw bytes."""
    if isinstance(xml_source, (bytes, bytearray)):
        root = ET.fromstring(xml_source)
    else:
        root = ET.parse(xml_source).getroot()

    found = {}
    for shortage in root.iter(_tag("MedicineShortage")):
        type_of_shortage = shortage.findtext(_tag("TypeOfShortage"))
        for pkg in shortage.iter(_tag("PackagedMedicinalProduct")):
            pack_id = pkg.findtext(_tag("NPLPackId"))
            if pack_id not in tracked_ids:
                continue
            interval = pkg.find(_tag("Interval"))
            found[pack_id] = {
                "type_of_shortage": type_of_shortage,
                "forecasted_start": interval.findtext(_tag("ForecastedStartDate")) if interval is not None else None,
                "forecasted_end": interval.findtext(_tag("ForecastedEndDate")) if interval is not None else None,
                "actual_end": interval.findtext(_tag("ActualEndDate")) if interval is not None else None,
                "last_updated": interval.findtext(_tag("LastUpdated")) if interval is not None else None,
            }
    return found


def main():
    sys.path.insert(0, ".")
    import checker

    tracked_ids = {p["npl_pack_id"] for p in checker.PRODUCTS}

    if len(sys.argv) > 1:
        source_desc = f"lokal fil ({sys.argv[1]})"
        found = extract(sys.argv[1], tracked_ids)
    else:
        print(f"Hämtar {SHORTAGE_FEED_URL} ...")
        data = _fetch(SHORTAGE_FEED_URL)
        source_desc = f"live hämtning från {SHORTAGE_FEED_URL}"
        found = extract(data, tracked_ids)

    snapshot = {
        "snapshot_taken_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "source": f"Läkemedelsverkets restsituationsregister ({source_desc})",
        "medicines": found,
    }

    with open("shortage_data.json", "w") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"Skrev {len(found)}/{len(tracked_ids)} bevakade läkemedel till shortage_data.json")
    missing = tracked_ids - set(found)
    if missing:
        print(f"Saknas i källan (troligen inte i bristläge just nu): {missing}")


if __name__ == "__main__":
    main()
