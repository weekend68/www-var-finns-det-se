import json
import os
import threading

from flask import Flask, render_template, Response

import checker
from db import init_db

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
    <circle cx="7" cy="7" r="5" fill="#22D3A5">
      <animate attributeName="opacity" values="1;0.3;1" dur="2s" repeatCount="indefinite"/>
    </circle>
    <text x="18" y="12" font-size="16" fill="#3D6480" letter-spacing=".08em">LIVE</text>
  </g>
</svg>"""

_polling_started = threading.Event()


def _template_vars():
    og_image = f"{SITE_URL}/og-image.svg" if SITE_URL else ""
    desc = (
        f"Realtidsövervakning av lagerstatus för {len(checker.PRODUCTS)} utvalda läkemedel "
        "på alla Sveriges apotek. Uppdateras automatiskt."
    )
    if SITE_NAME.endswith(".se"):
        name_base, name_tld = SITE_NAME[:-3], ".se"
    else:
        name_base, name_tld = SITE_NAME, ""

    with checker.state_lock:
        snap = json.loads(json.dumps(checker.state))

    return dict(
        site_name=SITE_NAME,
        name_base=name_base,
        name_tld=name_tld,
        canonical=SITE_URL,
        og_image=og_image,
        desc=desc,
        poll_min=checker.POLL_INTERVAL // 60,
        product_count=len(checker.PRODUCTS),
        show_limit=checker.SHOW_LIMIT,
        email_active=bool(os.getenv("RESEND_API_KEY")),
        snap=snap,
    )


def create_app():
    app = Flask(__name__)

    init_db()

    # Core routes
    @app.route("/")
    def index():
        return render_template("index.html", **_template_vars())

    @app.route("/og-image.svg")
    def og_image():
        return Response(
            OG_IMAGE_SVG,
            mimetype="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.route("/healthz")
    def healthz():
        with checker.state_lock:
            status = checker.state.get("status", "unknown")
        return {"status": status, "polls_done": checker.state.get("polls_done", 0)}

    @app.route("/privacy")
    def privacy():
        return render_template("privacy.html", site_name=SITE_NAME, site_url=SITE_URL)

    # Subscription blueprints
    from routes.subscribe import bp as subscribe_bp
    from routes.manage import bp as manage_bp
    from routes.unsubscribe import bp as unsubscribe_bp
    from routes.extend import bp as extend_bp
    from routes.search import bp as search_bp
    app.register_blueprint(subscribe_bp)
    app.register_blueprint(manage_bp)
    app.register_blueprint(unsubscribe_bp)
    app.register_blueprint(extend_bp)
    app.register_blueprint(search_bp)

    if not _polling_started.is_set():
        _polling_started.set()
        t = threading.Thread(target=checker.start_polling, daemon=True, name="polling")
        t.start()

    return app
