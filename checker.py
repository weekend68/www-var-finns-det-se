"""
Estradiol stock checker — polls fass.se and sends email when in stock.
Serves a status page on $PORT (set by Railway).

Required env vars:
  NOTIFY_EMAIL      — recipient email
  RESEND_API_KEY    — API-nyckel från resend.com (gratis, 100 mail/dag)

Optional:
  PORT              — HTTP port for status page (default: 8080)
  POLL_INTERVAL     — minutes between checks (default: 2)
"""

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

PRODUCTS = [
    {"name": "Estradot 37,5 mcg depotplåster",      "npl_pack_id": "20011130100489"},
    {"name": "Lenzetto spray 1,53 mg (liten förp)",  "npl_pack_id": "20140320100036"},
    {"name": "Lenzetto spray 1,53 mg (stor förp)",   "npl_pack_id": "20160407100353"},
    {"name": "Estrogel transdermal gel 0,75 mg/dos", "npl_pack_id": "20181129100025"},
]

CITIES = [
    ("Stockholm",   18.07,  59.33), ("Göteborg",    11.97,  57.71),
    ("Malmö",       13.00,  55.60), ("Uppsala",      17.65,  59.86),
    ("Linköping",   15.62,  58.41), ("Örebro",       15.21,  59.27),
    ("Västerås",    16.55,  59.62), ("Helsingborg",  12.69,  56.05),
    ("Norrköping",  16.19,  58.60), ("Jönköping",    14.16,  57.78),
    ("Umeå",        20.26,  63.83), ("Luleå",        22.14,  65.58),
    ("Sundsvall",   17.31,  62.39), ("Gävle",        17.14,  60.67),
    ("Östersund",   14.64,  63.18), ("Växjö",        14.81,  56.88),
    ("Borås",       12.93,  57.73), ("Karlstad",     13.51,  59.38),
    ("Kalmar",      16.36,  56.66), ("Halmstad",     12.86,  56.67),
]

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "2")) * 60
PORT = int(os.getenv("PORT", "8080"))
FASS_REFERER = "https://fass.se/health/product/20011130000246/stock-status"
IN_STOCK_STATUSES = {"IN_STOCK", "FEW_IN_STOCK"}

state = {
    "status": "Startar — hämtar apotekslista...",
    "last_check": None,
    "next_check": None,
    "polls_done": 0,
    "products": [{**p, "pharmacies": [], "error": None} for p in PRODUCTS],
}
state_lock = threading.Lock()


# --- FASS API ---

def fass_get(path):
    encoded = urllib.parse.quote(f"https://cms.fass.se/api/vard/{path}", safe="")
    req = urllib.request.Request(
        f"https://fass.se/api/content?endpoint={encoded}",
        headers={"Referer": FASS_REFERER},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def fass_post(path, body):
    encoded = urllib.parse.quote(f"https://cms.fass.se/api/vard/{path}", safe="")
    req = urllib.request.Request(
        f"https://fass.se/api/content?endpoint={encoded}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Referer": FASS_REFERER},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def fetch_all_pharmacies():
    pharmacies = {}
    for city, lon, lat in CITIES:
        try:
            for p in fass_get(f"pharmacy?longitude={lon}&latitude={lat}&limit=50"):
                gln = p.get("glnCode")
                if gln and gln not in pharmacies:
                    pharmacies[gln] = p
        except Exception as e:
            print(f"[{city}] fel vid apotekshämtning: {e}")
    return pharmacies


def check_product_stock(npl_pack_id, gln_codes, pharmacy_map):
    results = []
    for i in range(0, len(gln_codes), 50):
        batch = gln_codes[i:i + 50]
        try:
            data = fass_post(f"pharmacy/stock/{npl_pack_id}", batch)
            results.extend(data)
        except Exception as e:
            print(f"  Batchfel (offset {i}): {e}")
        time.sleep(0.2)

    in_stock = []
    for r in results:
        if r.get("stockInformation") in IN_STOCK_STATUSES:
            p = pharmacy_map.get(r["glnCode"], {})
            in_stock.append({
                "name": p.get("name", r["glnCode"]),
                "address": p.get("address", ""),
                "status": r["stockInformation"],
                "exchangeable": r.get("exchangeableProductInStock", False),
            })
    return in_stock


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
        + f"\n\nKontrollerat: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        + "https://fass.se/health/product/20011130000246/stock-status"
    )

    payload = json.dumps({
        "from": "apoteksvakt@resend.dev",
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
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
    print(f"  Mail skickat till {notify} (id: {result.get('id')})")


# --- POLLING LOOP ---

def polling_loop(pharmacy_map):
    gln_codes = list(pharmacy_map.keys())
    prev_in_stock = {p["npl_pack_id"]: set() for p in PRODUCTS}

    while True:
        t0 = time.time()
        now = datetime.now()
        print(f"\n[{now:%Y-%m-%d %H:%M:%S}] Kollar {len(gln_codes)} apotek, {len(PRODUCTS)} produkter...")

        newly_available = []
        updated_products = []

        for product in PRODUCTS:
            npl_pack_id = product["npl_pack_id"]
            name = product["name"]
            try:
                pharmacies = check_product_stock(npl_pack_id, gln_codes, pharmacy_map)
                current_glns = {ph["name"] for ph in pharmacies}
                prev_glns = prev_in_stock[npl_pack_id]

                if pharmacies and not prev_glns:
                    newly_available.append((name, pharmacies))

                prev_in_stock[npl_pack_id] = current_glns
                print(f"  {name}: {len(pharmacies)} i lager")
                updated_products.append({**product, "pharmacies": pharmacies, "error": None})
            except Exception as e:
                print(f"  {name}: FEL — {e}")
                updated_products.append({**product, "pharmacies": [], "error": str(e)})

        if newly_available:
            if os.getenv("RESEND_API_KEY"):
                try:
                    send_email(newly_available)
                except Exception as e:
                    print(f"  Mailfel: {e}")
            else:
                print("  Mail hoppas över (RESEND_API_KEY saknas)")

        elapsed = time.time() - t0
        sleep_time = max(0, POLL_INTERVAL - elapsed)
        next_check = datetime.fromtimestamp(time.time() + sleep_time)

        with state_lock:
            state["status"] = "ok"
            state["last_check"] = now.strftime("%Y-%m-%d %H:%M:%S")
            state["next_check"] = next_check.strftime("%H:%M:%S")
            state["polls_done"] += 1
            state["products"] = updated_products

        print(f"  Koll tog {elapsed:.0f}s, sover {sleep_time:.0f}s till nästa")
        time.sleep(sleep_time)


# --- WEB STATUS PAGE ---

def render_html():
    with state_lock:
        snap = json.loads(json.dumps(state))

    if snap["status"] != "ok":
        body = f"<p class='waiting'>⏳ {snap['status']}</p>"
    else:
        cards = []
        for p in snap["products"]:
            name = p["name"]
            pharmacies = p["pharmacies"]
            error = p["error"]

            if error:
                icon, label = "🔴", f"Fel: {error}"
                rows = ""
            elif not pharmacies:
                icon, label = "🔴", "Inte i lager (restnoterat)"
                rows = ""
            elif any(ph["status"] == "IN_STOCK" for ph in pharmacies):
                icon, label = "🟢", f"{len(pharmacies)} apotek har varan"
                rows = "".join(
                    f"<tr><td>{ph['name']}</td>"
                    f"<td class='status {ph['status'].lower()}'>{ph['status'].replace('_', ' ')}</td>"
                    f"<td>{'✓' if ph['exchangeable'] else ''}</td></tr>"
                    for ph in pharmacies
                )
            else:
                icon, label = "🟡", f"{len(pharmacies)} apotek — få kvar"
                rows = "".join(
                    f"<tr><td>{ph['name']}</td>"
                    f"<td class='status few_in_stock'>FEW IN STOCK</td>"
                    f"<td>{'✓' if ph['exchangeable'] else ''}</td></tr>"
                    for ph in pharmacies
                )

            table = (
                f"<table><thead><tr><th>Apotek</th><th>Status</th><th>Utbytbar</th></tr></thead>"
                f"<tbody>{rows}</tbody></table>"
                if rows else ""
            )
            cards.append(
                f"<div class='card'>"
                f"<h2>{icon} {name}</h2>"
                f"<p class='label'>{label}</p>"
                f"{table}"
                f"</div>"
            )

        email_status = "✉️ Mail aktiverat" if os.getenv("RESEND_API_KEY") else "⚠️ Mail ej konfigurerat (RESEND_API_KEY saknas)"
        meta = (
            f"<p class='meta'>Senaste koll: {snap['last_check']} · "
            f"Nästa: {snap['next_check']} · "
            f"Antal körningar: {snap['polls_done']} · "
            f"{email_status}</p>"
        )
        body = meta + "\n".join(cards)

    return f"""<!DOCTYPE html>
<html lang="sv">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="60">
  <title>Apoteksvakt — lagerstatus</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: system-ui, sans-serif; background: #f5f5f5; color: #222; padding: 1.5rem; }}
    h1 {{ font-size: 1.4rem; margin-bottom: 0.25rem; }}
    .subtitle {{ color: #666; font-size: 0.9rem; margin-bottom: 1.5rem; }}
    .meta {{ font-size: 0.8rem; color: #888; margin-bottom: 1.25rem; }}
    .card {{ background: #fff; border-radius: 8px; padding: 1.25rem; margin-bottom: 1rem;
             box-shadow: 0 1px 3px rgba(0,0,0,.08); max-width: 720px; }}
    .card h2 {{ font-size: 1.05rem; margin-bottom: 0.4rem; }}
    .label {{ font-size: 0.9rem; color: #555; margin-bottom: 0.75rem; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
    th {{ text-align: left; padding: 0.4rem 0.5rem; border-bottom: 2px solid #eee; color: #888; font-weight: 600; }}
    td {{ padding: 0.35rem 0.5rem; border-bottom: 1px solid #f0f0f0; }}
    .status {{ font-weight: 600; }}
    .in_stock {{ color: #2a7d2a; }}
    .few_in_stock {{ color: #b07d00; }}
    .waiting {{ color: #888; font-style: italic; padding: 2rem 0; }}
  </style>
</head>
<body>
  <h1>💊 Apoteksvakt — lagerstatus</h1>
  <p class="subtitle">Uppdateras automatiskt var 60:e sekund · Söker {len(CITIES)} städer · {len(PRODUCTS)} produkter</p>
  {body}
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        html = render_html().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(html))
        self.end_headers()
        self.wfile.write(html)

    def log_message(self, *args):
        pass


def start_web_server():
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Statussida: http://0.0.0.0:{PORT}")
    server.serve_forever()


# --- MAIN ---

def main():
    if not os.getenv("NOTIFY_EMAIL"):
        raise SystemExit("Saknar miljövariabel: NOTIFY_EMAIL")
    if not os.getenv("RESEND_API_KEY"):
        print("OBS: RESEND_API_KEY saknas — statussida fungerar men inga mail skickas")

    # Start web server immediately so Railway sees a live port
    web_thread = threading.Thread(target=start_web_server, daemon=True)
    web_thread.start()

    print(f"Hämtar apotekslista ({len(CITIES)} städer)...")
    with state_lock:
        state["status"] = f"Startar — hämtar apotekslista för {len(CITIES)} städer..."
    pharmacy_map = fetch_all_pharmacies()
    print(f"Hittade {len(pharmacy_map)} unika apotek i Sverige")
    print(f"Pollar var {POLL_INTERVAL // 60} minut(er)\n")

    polling_loop(pharmacy_map)


if __name__ == "__main__":
    main()
