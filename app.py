import json
import os
import threading

from flask import Flask, render_template, Response

import checker
from db import init_db

SITE_NAME = os.getenv("SITE_NAME", "medicinstatus.se")
SITE_URL  = os.getenv("SITE_URL", "").rstrip("/")

OG_IMAGE_SVG = """<svg viewBox="0 0 1200 630" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <pattern id="dots" x="0" y="0" width="40" height="40" patternUnits="userSpaceOnUse">
      <circle cx="20" cy="20" r="1" fill="#1E3A52" opacity="0.7"/>
    </pattern>
    <radialGradient id="glow" cx="52%" cy="50%" r="45%">
      <stop offset="0%" stop-color="#22D3A5" stop-opacity="0.1"/>
      <stop offset="100%" stop-color="#0D1B2A" stop-opacity="0"/>
    </radialGradient>
    <filter id="blur"><feGaussianBlur stdDeviation="18"/></filter>
  </defs>
  <rect width="1200" height="630" fill="#0D1B2A"/>
  <rect width="1200" height="630" fill="url(#dots)"/>
  <ellipse cx="620" cy="320" rx="380" ry="280" fill="url(#glow)"/>

  <!-- Blurred shadow pills (depth) -->
  <rect x="280" y="268" width="340" height="116" rx="58" fill="#22D3A5" opacity="0.18" filter="url(#blur)" transform="rotate(-10 450 326)"/>
  <rect x="610" y="278" width="280" height="100" rx="50" fill="#22D3A5" opacity="0.12" filter="url(#blur)" transform="rotate(14 750 328)"/>

  <!-- Back pill — top right, faint -->
  <rect x="-130" y="-40" width="280" height="88" rx="44"
        fill="none" stroke="#22D3A5" stroke-width="2" opacity="0.22"
        transform="translate(940 210) rotate(22)"/>

  <!-- Back pill — bottom left, faint -->
  <rect x="-110" y="-36" width="230" height="78" rx="39"
        fill="#22D3A5" opacity="0.12"
        transform="translate(230 470) rotate(-18)"/>

  <!-- Mid pill — right side -->
  <rect x="-140" y="-46" width="290" height="94" rx="47"
        fill="#1A4A6A" stroke="#22D3A5" stroke-width="1.5" opacity="0.7"
        transform="translate(870 355) rotate(12)"/>

  <!-- Main pill — center, dominant -->
  <rect x="-190" y="-62" width="380" height="124" rx="62"
        fill="#22D3A5"
        transform="translate(540 305) rotate(-8)"/>
  <!-- Main pill divider line -->
  <line x1="0" y1="-62" x2="0" y2="62"
        stroke="#0D1B2A" stroke-width="3" opacity="0.4"
        transform="translate(540 305) rotate(-8)"/>
  <!-- Main pill right half overlay (lighter) -->
  <rect x="0" y="-62" width="190" height="124"
        fill="#EEF4F8" opacity="0.15" rx="0"
        style="clip-path:none"
        transform="translate(540 305) rotate(-8)"/>

  <!-- Small pill — upper left -->
  <rect x="-80" y="-28" width="160" height="56" rx="28"
        fill="#22D3A5" opacity="0.45"
        transform="translate(310 195) rotate(30)"/>

  <!-- Tiny pill — lower right -->
  <rect x="-55" y="-20" width="115" height="42" rx="21"
        fill="#EEF4F8" opacity="0.18"
        transform="translate(1010 440) rotate(-5)"/>

  <rect x="0" y="0" width="5" height="630" fill="#22D3A5"/>
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

    @app.context_processor
    def inject_globals():
        return dict(site_name=SITE_NAME)

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
    from routes.log import bp as log_bp
    app.register_blueprint(subscribe_bp)
    app.register_blueprint(manage_bp)
    app.register_blueprint(unsubscribe_bp)
    app.register_blueprint(extend_bp)
    app.register_blueprint(search_bp)
    app.register_blueprint(log_bp)

    if not _polling_started.is_set():
        _polling_started.set()
        t = threading.Thread(target=checker.start_polling, daemon=True, name="polling")
        t.start()

    return app
