"""
Regenerate shortage_data.json from a manually downloaded Läkemedelsverket
medicine-shortage XML export.

Why manual: there's no confirmed, stable, machine-readable endpoint for this
data yet (Läkemedelsverket's own site is too JS-heavy to fetch programmatically
-- see diskussionsunderlag/positionering-2026-07-14.md, Fas 0). Until that's
resolved, refresh this by hand periodically:

  1. Download the current XML from Läkemedelsverket's "Sök anmälda
     försäljningsuppehåll" search service (export to XML/Excel), or from
     https://www.lakemedelsverket.se/sv/om-webbplatsen/oppna-data
  2. Run: python scripts/refresh_shortage_snapshot.py path/to/downloaded.xml
  3. Commit the updated shortage_data.json.

Only extracts entries for npl_pack_ids in checker.PRODUCTS -- this is the
Fas 2 scope (actively-bevakade läkemedel only), not the full national
catalogue (that's Fas 3).
"""

import json
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

NS = "http://eservices.lakemedelsverket.se/opendata/medicineshortage/v3/"


def _tag(name):
    return f"{{{NS}}}{name}"


def extract(xml_path, tracked_ids):
    tree = ET.parse(xml_path)
    root = tree.getroot()

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
    if len(sys.argv) != 2:
        print("Usage: python scripts/refresh_shortage_snapshot.py path/to/downloaded.xml")
        sys.exit(1)

    sys.path.insert(0, ".")
    import checker

    tracked_ids = {p["npl_pack_id"] for p in checker.PRODUCTS}
    found = extract(sys.argv[1], tracked_ids)

    snapshot = {
        "snapshot_taken_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "source": "Läkemedelsverkets restsituationsregister (manuellt nedladdad export)",
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
