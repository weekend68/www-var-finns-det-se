import json
import os
import threading

from flask import Flask, render_template, Response

import checker
import faq as faq_builder
from config import SITE_URL, SUBSCRIPTION_TTL_DAYS
from db import get_db, init_db, list_medications_for_sitemap
from national_shortages import get_shortage_categories
from seo import truncate_title
from slugs import category_url, medication_url

SITE_NAME = os.getenv("SITE_NAME", "varfinnsdet.se")

_polling_started = threading.Event()


def _template_vars():
    og_image = f"{SITE_URL}/og-image.png" if SITE_URL else ""
    desc = (
        "Lagerstatus för alla läkemedel på Sveriges apotek. "
        "Är mitt läkemedel restnoterat? Bevaka det — få e-post när det finns igen."
    )

    # The homepage template only ever reads snap.status/last_check/products --
    # never state["all_stock"] (which grows with every actively-polled
    # medication). Copying just the small "products" list instead of the
    # whole state dict avoids needlessly serializing that on every single
    # homepage view while holding the same lock the poll loop needs.
    with checker.state_lock:
        status = checker.state["status"]
        last_check = checker.state["last_check"]
        products = json.loads(json.dumps(checker.state["products"]))
    for p in products:
        # Bare npl_pack_id, no computed slug — routes/lakemedel.py 301-redirects
        # to the canonical slug itself, avoiding any risk of this link computing
        # a different slug than the route's own canonical calculation.
        p["lakemedel_url"] = f"/lakemedel/{p['npl_pack_id']}"
    snap = {"status": status, "last_check": last_check, "products": products}

    # Small, static homepage FAQ -- no per-item data needed (see
    # faq.py's build_homepage_faq()). Built once here and reused for both
    # the visible FAQ section and the FAQPage JSON-LD block.
    faq_list = faq_builder.build_homepage_faq()
    jsonld_faq = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": item["question"],
                "acceptedAnswer": {"@type": "Answer", "text": item["answer"]},
            }
            for item in faq_list
        ],
    }

    return dict(
        canonical=SITE_URL,
        og_image=og_image,
        desc=desc,
        poll_min=checker.POLL_INTERVAL // 60,
        product_count=len(checker.PRODUCTS),
        show_limit=checker.SHOW_LIMIT,
        email_active=bool(os.getenv("RESEND_API_KEY")),
        staleness=checker.staleness_tier(last_check),
        show_partner_guide=True,
        snap=snap,
        faq_list=faq_list,
        jsonld_faq=jsonld_faq,
    )


def create_app():
    app = Flask(__name__)
    app.jinja_env.filters["truncate_title"] = truncate_title

    @app.after_request
    def set_referrer_policy(response):
        # Token-bearing pages (/manage/<token>, /unsubscribe/<token>,
        # /extend/<token>) load a third-party script (Umami) as a
        # subresource. Modern browsers already default to
        # strict-origin-when-cross-origin (stripping the path, and thus
        # the token, before sending Referer cross-origin), but set it
        # explicitly rather than relying on a browser default.
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return response

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

    @app.route("/favicon.ico")
    def favicon_ico():
        # Browsers request this root path directly regardless of the
        # <link rel="icon"> tags in <head> -- without this route it just 404s.
        return app.send_static_file("favicon.ico")

    @app.route("/healthz")
    def healthz():
        with checker.state_lock:
            status = checker.state.get("status", "unknown")
            polls_done = checker.state.get("polls_done", 0)
            last_check = checker.state.get("last_check")
        return {
            "status": status,
            "polls_done": polls_done,
            "last_check": last_check,
            "staleness": checker.staleness_tier(last_check),
        }

    @app.route("/privacy")
    def privacy():
        return render_template("privacy.html", site_name=SITE_NAME, site_url=SITE_URL, ttl_days=SUBSCRIPTION_TTL_DAYS)

    @app.route("/om")
    def om():
        return render_template("om.html", site_name=SITE_NAME, site_url=SITE_URL)

    @app.route("/jamforelse-lagerstatustjanster")
    def jamforelse():
        # Deliberately not linked from _nav.html or sitemap.xml yet -- and
        # noindex in its own <head> -- until the linking strategy is decided
        # (see GitHub issue #7). Reachable by direct URL only for now.
        return render_template("jamforelse.html", site_name=SITE_NAME, site_url=SITE_URL)

    @app.route("/robots.txt")
    def robots_txt():
        lines = [
            "User-agent: *",
            "Disallow: /subscribe",
            "Disallow: /manage/",
            "Disallow: /confirm/",
            "Disallow: /unsubscribe/",
            "Disallow: /extend/",
            "Disallow: /api/",
            "Disallow: /admin",
            "Disallow: /log",
        ]
        if SITE_URL:
            lines.append(f"Sitemap: {SITE_URL}/sitemap.xml")
        return Response("\n".join(lines) + "\n", mimetype="text/plain")

    @app.route("/llms.txt")
    def llms_txt():
        url = SITE_URL or ""
        body = f"""# {SITE_NAME}

> Realtidsbevakning av läkemedelslager på Sveriges apotek. Sök ett restnoterat
> läkemedel, se aktuell lagerstatus per apotek, och bevaka det gratis för att
> få e-post så fort det finns i lager igen.

Lagerstatus hämtas löpande från Fass.se (i samarbete med Sveriges
Apoteksförening). Nationella restnoteringsprognoser kommer från
Läkemedelsverkets öppna data. Ingen inloggning krävs för att söka eller se
lagerstatus — endast för att starta en bevakning (e-post, dubbel opt-in).

## Huvudsidor

- [Startsida]({url}/): sök läkemedel, se lagerstatus för de mest bevakade
- [Bristsituationer per läkemedelsgrupp]({url}/kategorier): restnoterade läkemedel grupperade per ATC-kod/substans
- [Om tjänsten]({url}/om): vad varfinnsdet.se gör och hur den fungerar
- [Integritetspolicy]({url}/privacy): vilka uppgifter som sparas vid en bevakning och varför

## Hur en bevakning fungerar

- Sök läkemedlet, välj förpackning, ange e-post — bekräftelsemejl skickas (double opt-in)
- Så fort läkemedlet är i lager igen skickas ett mejl med vilka apotek som har det
- Bevakningen löper ut automatiskt efter 30 dagar om den inte förlängs

## Optional

- [Sitemap]({url}/sitemap.xml): fullständig lista över alla läkemedelssidor
"""
        return Response(body, mimetype="text/markdown")

    @app.route("/sitemap.xml")
    def sitemap_xml():
        with get_db() as db:
            meds = list_medications_for_sitemap(db)
            categories = get_shortage_categories(db)
        urls = [SITE_URL + "/"] if SITE_URL else ["/"]
        urls.append(f"{SITE_URL}/om")
        for m in meds:
            urls.append(medication_url(SITE_URL, m["npl_pack_id"], m["name"], m["strength"], m["form"]))
        urls.append(f"{SITE_URL}/kategorier")
        for c in categories:
            urls.append(category_url(SITE_URL, c["atc_code"], c["atc_term"]))
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
    from routes.kategori import bp as kategori_bp
    from routes.admin import bp as admin_bp
    app.register_blueprint(subscribe_bp)
    app.register_blueprint(manage_bp)
    app.register_blueprint(unsubscribe_bp)
    app.register_blueprint(extend_bp)
    app.register_blueprint(search_bp)
    app.register_blueprint(log_bp)
    app.register_blueprint(lakemedel_bp)
    app.register_blueprint(kategori_bp)
    app.register_blueprint(admin_bp)

    if not _polling_started.is_set():
        _polling_started.set()
        t = threading.Thread(target=checker.start_polling, daemon=True, name="polling")
        t.start()

    return app
