"""
Estradiol stock checker — polls fass.se and sends email when in stock.
Serves a status page on $PORT (set by Railway).

Required env vars:
  NOTIFY_EMAIL      — recipient email

Optional:
  RESEND_API_KEY    — API-nyckel från resend.com (gratis, 100 mail/dag)
  PORT              — HTTP port for status page (default: 8080)
  POLL_INTERVAL     — minutes between checks (default: 2)
  CACHE_FILE        — path for persistent state cache (default: /tmp/medicinstatus_cache.json)
  SITE_URL          — public URL, used for og:url and og:image (e.g. https://medicinstatus.se)
  SITE_NAME         — site name shown in header (default: medicinstatus.se)
  FROM_EMAIL        — sender address for Resend (default: apoteksvakt@resend.dev)
"""

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Stockholm")
CACHE_FILE = os.getenv("CACHE_FILE", "/tmp/medicinstatus_cache.json")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "2")) * 60
PORT = int(os.getenv("PORT", "8080"))
FASS_REFERER = "https://fass.se/health/product/20011130000246/stock-status"
IN_STOCK_STATUSES = {"IN_STOCK", "FEW_IN_STOCK"}
SHOW_LIMIT = 10  # pharmacies shown before "Visa alla"-button
SITE_NAME = os.getenv("SITE_NAME", "medicinstatus.se")
SITE_URL  = os.getenv("SITE_URL", "").rstrip("/")

OG_IMAGE_SVG = """<svg viewBox="0 0 1200 630" xmlns="http://www.w3.org/2000/svg" font-family="system-ui,-apple-system,'Helvetica Neue',sans-serif">
  <defs>
    <pattern id="dots" x="0" y="0" width="40" height="40" patternUnits="userSpaceOnUse">
      <circle cx="20" cy="20" r="1" fill="#1E3A52" opacity="0.8"/>
    </pattern>
  </defs>
  <rect width="1200" height="630" fill="#0D1B2A"/>
  <rect width="1200" height="630" fill="url(#dots)"/>
  <rect x="0" y="0" width="5" height="630" fill="#22D3A5"/>
  <g transform="translate(110, 200)">
    <rect x="0" y="0" width="160" height="70" rx="35" fill="none" stroke="#22D3A5" stroke-width="4"/>
    <line x1="80" y1="0" x2="80" y2="70" stroke="#22D3A5" stroke-width="4"/>
    <clipPath id="lh"><rect x="0" y="0" width="80" height="70"/></clipPath>
    <rect x="0" y="0" width="160" height="70" rx="35" fill="#22D3A5" clip-path="url(#lh)"/>
  </g>
  <text x="110" y="352" font-size="88" font-weight="700" letter-spacing="-2">
    <tspan fill="#EEF4F8">medicinstatus</tspan><tspan fill="#22D3A5">.se</tspan>
  </text>
  <text x="112" y="408" font-size="28" font-weight="400" fill="#6B8CA6">Lagerstatus för läkemedel på apotek i Sverige</text>
  <g transform="translate(110, 460)">
    <rect x="0" y="0" width="980" height="6" rx="3" fill="#162A3E"/>
    <rect x="0" y="0" width="90" height="6" rx="3" fill="#22D3A5"/>
    <rect x="100" y="0" width="60" height="6" fill="#22D3A5"/>
    <rect x="170" y="0" width="40" height="6" fill="#22D3A5" opacity="0.6"/>
    <rect x="220" y="0" width="20" height="6" rx="3" fill="#22D3A5" opacity="0.4"/>
    <g transform="translate(0,22)">
      <circle cx="8"   cy="8" r="6" fill="#22D3A5"/>
      <circle cx="28"  cy="8" r="6" fill="#22D3A5" opacity=".5"/>
      <circle cx="48"  cy="8" r="6" fill="#1E3A52"/>
      <circle cx="68"  cy="8" r="6" fill="#1E3A52"/>
      <circle cx="88"  cy="8" r="6" fill="#1E3A52"/>
      <circle cx="108" cy="8" r="6" fill="#1E3A52"/>
    </g>
  </g>
  <g transform="translate(1060, 565)">
    <circle cx="7" cy="7" r="5" fill="#22D3A5"/>
    <text x="18" y="12" font-size="16" fill="#3D6480" letter-spacing=".08em">LIVE</text>
  </g>
</svg>"""

PRODUCTS = [
    {"name": "Estradot 25 mcg depotplåster",         "npl_pack_id": "20040113100574"},
    {"name": "Estradot 37,5 mcg depotplåster",       "npl_pack_id": "20011130100489"},
    {"name": "Estradot 50 mcg depotplåster",         "npl_pack_id": "20011130100502"},
    {"name": "Estradot 75 mcg depotplåster",         "npl_pack_id": "20011130100526"},
    {"name": "Estradot 100 mcg depotplåster",        "npl_pack_id": "20011130100564"},
    {"name": "Estrogel transdermal gel 0,75 mg/dos", "npl_pack_id": "20181129100025"},
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

def save_cache():
    try:
        with state_lock:
            data = json.loads(json.dumps(state))
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
            state.update(data)
            state["status"] = "ok (från cache — första koll pågår)"
        prev = {p["npl_pack_id"]: {ph["name"] for ph in p.get("pharmacies", [])}
                for p in data.get("products", [])}
        print(f"Cache laddad: {data.get('last_check', '?')}")
        return prev
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"Cache-läsfel: {e}")
        return {}


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


def check_product_stock(npl_pack_id, gln_codes, pharmacy_map):
    results = []
    for i in range(0, len(gln_codes), 50):
        batch = gln_codes[i:i + 50]
        try:
            data = fass_post(f"pharmacy/stock/{npl_pack_id}", batch)
            results.extend(data)
        except Exception as e:
            if getattr(e, "code", None) == 400:
                # Batch contains GLN codes unknown to Fass — retry in sub-batches of 10
                for j in range(0, len(batch), 10):
                    sub = batch[j:j + 10]
                    try:
                        results.extend(fass_post(f"pharmacy/stock/{npl_pack_id}", sub))
                    except Exception:
                        pass
                    time.sleep(0.1)
            else:
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
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
    print(f"  Mail skickat till {notify} (id: {result.get('id')})")


# --- POLLING LOOP ---

def polling_loop(pharmacy_map, prev_in_stock):
    gln_codes = list(pharmacy_map.keys())

    def check_one(product):
        try:
            pharmacies = check_product_stock(product["npl_pack_id"], gln_codes, pharmacy_map)
            return product, pharmacies, None
        except Exception as e:
            return product, [], str(e)

    while True:
        t0 = time.time()
        now = now_local()
        print(f"\n[{now:%Y-%m-%d %H:%M:%S}] Kollar {len(gln_codes)} apotek, {len(PRODUCTS)} produkter (parallellt)...")

        # Run all product checks concurrently — each does batched HTTP calls internally
        with ThreadPoolExecutor(max_workers=len(PRODUCTS)) as executor:
            future_map = {executor.submit(check_one, p): p for p in PRODUCTS}
            result_map = {}
            for future in as_completed(future_map):
                product, pharmacies, error = future.result()
                result_map[product["npl_pack_id"]] = (product, pharmacies, error)

        newly_available = []
        updated_products = []

        for product in PRODUCTS:
            npl_pack_id = product["npl_pack_id"]
            p, pharmacies, error = result_map[npl_pack_id]
            name = p["name"]
            if error:
                print(f"  {name}: FEL — {error}")
                updated_products.append({**product, "pharmacies": [], "error": error})
            else:
                current_glns = {ph["name"] for ph in pharmacies}
                prev_glns = prev_in_stock.get(npl_pack_id, set())

                # Only alert when going from 0 → >0
                if pharmacies and not prev_glns:
                    newly_available.append((name, pharmacies))

                prev_in_stock[npl_pack_id] = current_glns
                print(f"  {name}: {len(pharmacies)} i lager")
                updated_products.append({**product, "pharmacies": pharmacies, "error": None})

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
        next_check = datetime.fromtimestamp(time.time() + sleep_time, tz=TZ)

        with state_lock:
            state["status"] = "ok"
            state["last_check"] = now.strftime("%Y-%m-%d %H:%M:%S")
            state["next_check"] = next_check.strftime("%H:%M:%S")
            state["polls_done"] += 1
            state["products"] = updated_products

        save_cache()
        print(f"  Koll tog {elapsed:.0f}s, sover {sleep_time:.0f}s till nästa")
        time.sleep(sleep_time)


# --- WEB STATUS PAGE ---

def pharmacy_rows(pharmacies):
    def row(ph):
        exch = "✓" if ph["exchangeable"] else ""
        return (
            f"<tr><td>{ph['name']}</td>"
            f"<td class='status {ph['status'].lower()}'>{ph['status'].replace('_', ' ')}</td>"
            f"<td>{exch}</td></tr>"
        )

    if len(pharmacies) <= SHOW_LIMIT:
        rows = "".join(row(ph) for ph in pharmacies)
        return (
            f"<table><thead><tr><th>Apotek</th><th>Status</th><th>Utbytbar</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )

    visible = "".join(row(ph) for ph in pharmacies[:SHOW_LIMIT])
    hidden = "".join(row(ph) for ph in pharmacies[SHOW_LIMIT:])
    return (
        f"<table><thead><tr><th>Apotek</th><th>Status</th><th>Utbytbar</th></tr></thead>"
        f"<tbody>{visible}</tbody></table>"
        f"<details><summary>Visa alla {len(pharmacies)} apotek</summary>"
        f"<table><tbody>{hidden}</tbody></table></details>"
    )


def render_html():
    with state_lock:
        snap = json.loads(json.dumps(state))

    if snap["status"] != "ok" and "cache" not in snap["status"]:
        body = f"<p class='waiting'>⏳ {snap['status']}</p>"
    else:
        cards = []
        for p in snap["products"]:
            name = p["name"]
            pharmacies = p["pharmacies"]
            error = p["error"]

            if error:
                icon, label, table = "🔴", f"Fel: {error}", ""
            elif not pharmacies:
                icon, label, table = "🔴", "Inte i lager (restnoterat)", ""
            elif any(ph["status"] == "IN_STOCK" for ph in pharmacies):
                icon = "🟢"
                label = f"{len(pharmacies)} apotek har varan"
                table = pharmacy_rows(pharmacies)
            else:
                icon = "🟡"
                label = f"{len(pharmacies)} apotek — få kvar"
                table = pharmacy_rows(pharmacies)

            fresh = "" if snap["status"] == "ok" else "<span class='stale'> (från cache)</span>"
            cards.append(
                f"<div class='card'>"
                f"<h2>{icon} {name}{fresh}</h2>"
                f"<p class='label'>{label}</p>"
                f"{table}"
                f"</div>"
            )

        email_status = "✉️ Mail aktiverat" if os.getenv("RESEND_API_KEY") else "⚠️ Mail ej konfigurerat"
        meta = (
            f"<p class='meta'>Senaste koll: {snap['last_check']} · "
            f"Nästa: {snap['next_check']} · "
            f"Körningar: {snap['polls_done']} · "
            f"{email_status}</p>"
        )
        body = meta + "\n".join(cards)

    og_image = f"{SITE_URL}/og-image.svg" if SITE_URL else ""
    canonical = SITE_URL or ""
    desc = f"Realtidsövervakning av lagerstatus för {len(PRODUCTS)} utvalda läkemedel på alla Sveriges apotek. Uppdateras automatiskt."
    poll_min = POLL_INTERVAL // 60
    if SITE_NAME.endswith(".se"):
        name_base, name_tld = SITE_NAME[:-3], ".se"
    else:
        name_base, name_tld = SITE_NAME, ""

    return f"""<!DOCTYPE html>
<html lang="sv">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="60">
  <title>{SITE_NAME} — lagerstatus för läkemedel</title>
  <meta name="description" content="{desc}">
  <meta name="robots" content="index, follow">
  {f'<link rel="canonical" href="{canonical}">' if canonical else ""}
  <meta property="og:type"        content="website">
  <meta property="og:site_name"   content="{SITE_NAME}">
  <meta property="og:title"       content="{SITE_NAME} — lagerstatus för läkemedel">
  <meta property="og:description" content="{desc}">
  {f'<meta property="og:url"         content="{canonical}">' if canonical else ""}
  {f'<meta property="og:image"       content="{og_image}">' if og_image else ""}
  <meta property="og:image:width"  content="1200">
  <meta property="og:image:height" content="630">
  <meta property="og:locale"      content="sv_SE">
  <meta name="twitter:card"        content="summary_large_image">
  <meta name="twitter:title"       content="{SITE_NAME} — lagerstatus för läkemedel">
  <meta name="twitter:description" content="{desc}">
  {f'<meta name="twitter:image"      content="{og_image}">' if og_image else ""}
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: system-ui, sans-serif; background: #f0f4f8; color: #222;
            min-height: 100vh; display: flex; flex-direction: column; }}
    /* --- hero header --- */
    .hero {{
      background: #0D1B2A;
      background-image: radial-gradient(#1E3A52 1px, transparent 1px);
      background-size: 40px 40px;
      border-left: 5px solid #22D3A5;
      padding: 2rem 1.5rem 1.75rem;
      position: relative;
    }}
    .hero-inner {{ max-width: 720px; }}
    .hero-brand {{ display: flex; align-items: center; gap: 0.6rem; margin-bottom: 0.4rem; }}
    .hero-pill {{ flex-shrink: 0; }}
    .hero-name {{ font-size: 1.45rem; font-weight: 700; letter-spacing: -0.02em; line-height: 1; }}
    .hero-name-main {{ color: #EEF4F8; }}
    .hero-name-tld  {{ color: #22D3A5; }}
    .hero-tagline {{ color: #6B8CA6; font-size: 0.8rem; letter-spacing: 0.06em;
                     text-transform: uppercase; margin-bottom: 0.9rem; }}
    .hero-desc {{ color: #8CAFC7; font-size: 0.875rem; line-height: 1.65; max-width: 560px; }}
    .hero-desc a {{ color: #22D3A5; text-decoration: none; }}
    .live-badge {{ position: absolute; top: 1.25rem; right: 1.5rem;
                   display: flex; align-items: center; gap: 0.4rem;
                   color: #3D6480; font-size: 0.7rem; letter-spacing: .12em; }}
    .live-dot {{ width: 7px; height: 7px; border-radius: 50%; background: #22D3A5;
                 animation: pulse 2s infinite; }}
    @keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.3; }} }}
    /* --- main content --- */
    main {{ flex: 1; padding: 1.5rem; }}
    .meta {{ font-size: 0.8rem; color: #888; margin-bottom: 1.25rem; }}
    .stale {{ font-size: 0.75rem; color: #aaa; font-weight: normal; }}
    .card {{ background: #fff; border-radius: 8px; padding: 1.25rem; margin-bottom: 1rem;
             box-shadow: 0 1px 3px rgba(0,0,0,.08); max-width: 720px; }}
    .card h2 {{ font-size: 1.05rem; margin-bottom: 0.4rem; }}
    .label {{ font-size: 0.9rem; color: #555; margin-bottom: 0.75rem; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; margin-bottom: 0.5rem; }}
    th {{ text-align: left; padding: 0.4rem 0.5rem; border-bottom: 2px solid #eee; color: #888; font-weight: 600; }}
    td {{ padding: 0.35rem 0.5rem; border-bottom: 1px solid #f0f0f0; }}
    .status {{ font-weight: 600; }}
    .in_stock {{ color: #2a7d2a; }}
    .few_in_stock {{ color: #b07d00; }}
    details summary {{ cursor: pointer; font-size: 0.85rem; color: #555; padding: 0.4rem 0;
                       list-style: none; }}
    details summary::before {{ content: "▸ "; }}
    details[open] summary::before {{ content: "▾ "; }}
    .waiting {{ color: #888; font-style: italic; padding: 2rem 0; }}
    footer {{ max-width: 720px; margin: 2rem auto 0; padding: 1rem 1.5rem 1.5rem;
              font-size: 0.75rem; color: #999; line-height: 1.6;
              border-top: 1px solid #e0e0e0; }}
    footer a {{ color: #999; }}
  </style>
</head>
<body>
  <header class="hero">
    <div class="hero-inner">
      <div class="hero-brand">
        <svg class="hero-pill" width="36" height="16" viewBox="0 0 36 16" aria-hidden="true">
          <rect x="0" y="0" width="36" height="16" rx="8" fill="none" stroke="#22D3A5" stroke-width="1.5"/>
          <line x1="18" y1="0" x2="18" y2="16" stroke="#22D3A5" stroke-width="1.5"/>
          <clipPath id="pill-lh"><rect x="0" y="0" width="18" height="16"/></clipPath>
          <rect x="0" y="0" width="36" height="16" rx="8" fill="#22D3A5" clip-path="url(#pill-lh)"/>
        </svg>
        <span class="hero-name">
          <span class="hero-name-main">{name_base}</span><span class="hero-name-tld">{name_tld}</span>
        </span>
      </div>
      <p class="hero-tagline">Lagerstatus för läkemedel på apotek i Sverige</p>
      <p class="hero-desc">
        Sidan bevakar lagerstatus för {len(PRODUCTS)} utvalda läkemedel
        på alla Sveriges apotek. Informationen hämtas direkt från
        <a href="https://fass.se" target="_blank" rel="noopener">Fass.se</a>
        och uppdateras automatiskt var {poll_min}. minut.
        Du kan prenumerera på e-postaviseringar när ett läkemedel återfås i lager.
      </p>
    </div>
    <div class="live-badge" aria-label="Live">
      <span class="live-dot"></span>LIVE
    </div>
  </header>
  <main>
    {body}
  </main>
  <footer>
    Lagerstatus hämtas från <a href="https://fass.se" target="_blank" rel="noopener">Fass.se</a>
    i samarbete med Sveriges Apoteksförening. Informationen kan vara fördröjd — kontakta alltid
    ditt apotek för aktuell status. {SITE_NAME} är inte kopplat till Fass, LIF eller Sveriges Apoteksförening.
  </footer>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/og-image.svg":
            data = OG_IMAGE_SVG.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", len(data))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
            return
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

    # Load cached state so page shows data right away after a restart
    prev_in_stock = load_cache()

    print("Hämtar apoteksregister från Läkemedelsverket...")
    with state_lock:
        state["status"] = "Startar — hämtar apoteksregister..."
    pharmacy_map = fetch_all_pharmacies()
    print(f"Hittade {len(pharmacy_map)} apotek i Sverige (Läkemedelsverket)")
    print(f"Pollar var {POLL_INTERVAL // 60} minut(er)\n")

    polling_loop(pharmacy_map, prev_in_stock)


if __name__ == "__main__":
    main()
