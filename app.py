import json
import os
import threading

from flask import Flask, render_template, Response

import checker
from db import get_db, init_db, list_medications_for_sitemap
from slugs import slugify_medication

SITE_NAME = os.getenv("SITE_NAME", "medicinstatus.se")
SITE_URL  = os.getenv("SITE_URL", "").rstrip("/")

_polling_started = threading.Event()


def _template_vars():
    og_image = f"{SITE_URL}/og-image.png" if SITE_URL else ""
    desc = (
        "Lagerstatus för alla läkemedel på Sveriges apotek. "
        "Är mitt läkemedel restnoterat? Bevaka det — få e-post när det finns igen."
    )

    with checker.state_lock:
        snap = json.loads(json.dumps(checker.state))
    for p in snap.get("products", []):
        # Bare npl_pack_id, no computed slug — routes/lakemedel.py 301-redirects
        # to the canonical slug itself, avoiding any risk of this link computing
        # a different slug than the route's own canonical calculation.
        p["lakemedel_url"] = f"/lakemedel/{p['npl_pack_id']}"

    return dict(
        canonical=SITE_URL,
        og_image=og_image,
        desc=desc,
        poll_min=checker.POLL_INTERVAL // 60,
        product_count=len(checker.PRODUCTS),
        show_limit=checker.SHOW_LIMIT,
        email_active=bool(os.getenv("RESEND_API_KEY")),
        staleness=checker.staleness_tier(snap.get("last_check")),
        snap=snap,
    )


def create_app():
    app = Flask(__name__)

    @app.context_processor
    def inject_globals():
        if SITE_NAME.endswith(".se"):
            name_base, name_tld = SITE_NAME[:-3], ".se"
        else:
            name_base, name_tld = SITE_NAME, ""
        return dict(site_name=SITE_NAME, name_base=name_base, name_tld=name_tld)

    init_db()
    checker.seed_products()

    # Core routes
    @app.route("/")
    def index():
        return render_template("index.html", **_template_vars())

    @app.route("/og-image.png")
    def og_image():
        return app.send_static_file("og-image.png")

    @app.route("/healthz")
    def healthz():
        with checker.state_lock:
            status = checker.state.get("status", "unknown")
        return {"status": status, "polls_done": checker.state.get("polls_done", 0)}

    @app.route("/privacy")
    def privacy():
        return render_template("privacy.html", site_name=SITE_NAME, site_url=SITE_URL)

    @app.route("/robots.txt")
    def robots_txt():
        lines = [
            "User-agent: *",
            "Disallow: /manage/",
            "Disallow: /confirm/",
            "Disallow: /unsubscribe/",
            "Disallow: /extend/",
            "Disallow: /api/",
        ]
        if SITE_URL:
            lines.append(f"Sitemap: {SITE_URL}/sitemap.xml")
        return Response("\n".join(lines) + "\n", mimetype="text/plain")

    @app.route("/sitemap.xml")
    def sitemap_xml():
        with get_db() as db:
            meds = list_medications_for_sitemap(db)
        urls = [SITE_URL + "/"] if SITE_URL else ["/"]
        for m in meds:
            slug = slugify_medication(m["name"], m["strength"], m["form"])
            urls.append(f"{SITE_URL}/lakemedel/{m['npl_pack_id']}-{slug}")
        xml = render_template("sitemap.xml", urls=urls)
        return Response(xml, mimetype="application/xml")

    # Subscription blueprints
    from routes.subscribe import bp as subscribe_bp
    from routes.manage import bp as manage_bp
    from routes.unsubscribe import bp as unsubscribe_bp
    from routes.extend import bp as extend_bp
    from routes.search import bp as search_bp
    from routes.log import bp as log_bp
    from routes.lakemedel import bp as lakemedel_bp
    app.register_blueprint(subscribe_bp)
    app.register_blueprint(manage_bp)
    app.register_blueprint(unsubscribe_bp)
    app.register_blueprint(extend_bp)
    app.register_blueprint(search_bp)
    app.register_blueprint(log_bp)
    app.register_blueprint(lakemedel_bp)

    if not _polling_started.is_set():
        _polling_started.set()
        t = threading.Thread(target=checker.start_polling, daemon=True, name="polling")
        t.start()

    return app
