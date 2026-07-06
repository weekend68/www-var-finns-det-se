import os
from datetime import datetime, timedelta

from flask import Blueprint, redirect, render_template, request, url_for

import checker
import mail
from db import create_token, get_db, get_or_create_token

bp = Blueprint("subscribe", __name__)
SITE_URL = os.getenv("SITE_URL", "").rstrip("/")


@bp.route("/subscribe", methods=["GET", "POST"])
def subscribe():
    if request.method == "GET":
        npl = request.args.get("npl", "").strip()
        med_name = next((p["name"] for p in checker.PRODUCTS if p["npl_pack_id"] == npl), "")
        if not med_name and npl:
            try:
                with get_db() as db:
                    m = db.execute("SELECT name FROM medications WHERE npl_pack_id=?", [npl]).fetchone()
                if m and m["name"] != npl:
                    med_name = m["name"]
            except Exception:
                pass
        return render_template("subscribe.html", npl_pack_id=npl, med_name=med_name, error=None)

    email = request.form.get("email", "").strip().lower()
    npl_pack_id = request.form.get("npl_pack_id", "").strip()
    consent = request.form.get("consent")

    def form_error(msg):
        med_name = next((p["name"] for p in checker.PRODUCTS if p["npl_pack_id"] == npl_pack_id), "")
        if not med_name and npl_pack_id:
            try:
                with get_db() as db:
                    m = db.execute("SELECT name FROM medications WHERE npl_pack_id=?", [npl_pack_id]).fetchone()
                if m and m["name"] != npl_pack_id:
                    med_name = m["name"]
            except Exception:
                pass
        return render_template("subscribe.html", npl_pack_id=npl_pack_id, med_name=med_name, error=msg), 400

    if not email or "@" not in email or "." not in email.split("@")[-1]:
        return form_error("Ogiltig e-postadress.")
    if not consent:
        return form_error("Du måste godkänna integritetspolicyn för att prenumerera.")
    if not npl_pack_id:
        return form_error("Inget läkemedel valt.")

    with get_db() as db:
        med = db.execute("SELECT name FROM medications WHERE npl_pack_id=?", [npl_pack_id]).fetchone()
        if not med:
            return render_template("message.html",
                title="Okänt läkemedel",
                message="Det valda läkemedlet hittades inte.",
                icon="❌"), 404

        # Get or create subscriber
        row = db.execute("SELECT id FROM subscribers WHERE email=?", [email]).fetchone()
        if row:
            subscriber_id = row["id"]
            db.execute("UPDATE subscribers SET deleted_at=NULL WHERE id=?", [subscriber_id])
        else:
            cur = db.execute("INSERT INTO subscribers (email) VALUES (?)", [email])
            subscriber_id = cur.lastrowid

        # Create or reactivate subscription
        expires_at = (datetime.utcnow() + timedelta(days=30)).isoformat()
        existing = db.execute(
            "SELECT id FROM subscriptions WHERE subscriber_id=? AND npl_pack_id=?",
            [subscriber_id, npl_pack_id],
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE subscriptions SET active=1, expires_at=? WHERE id=?",
                [expires_at, existing["id"]],
            )
            subscription_id = existing["id"]
        else:
            cur = db.execute(
                "INSERT INTO subscriptions (subscriber_id, npl_pack_id, expires_at) VALUES (?,?,?)",
                [subscriber_id, npl_pack_id, expires_at],
            )
            subscription_id = cur.lastrowid

        # Invalidate any pending confirm tokens (re-subscribe case)
        db.execute(
            "UPDATE tokens SET used_at=datetime('now') WHERE subscriber_id=? AND type='confirm' AND used_at IS NULL",
            [subscriber_id],
        )

        # Create confirm token (48h TTL)
        token = create_token(db, "confirm", subscriber_id, subscription_id, ttl_hours=48)

        # Pre-create unsubscribe token for this subscription (idempotent, 30d)
        get_or_create_token(db, "unsubscribe", subscriber_id, subscription_id, ttl_hours=30 * 24)

        db.commit()

    try:
        mail.send_confirmation(email, token, SITE_URL)
    except Exception as e:
        print(f"  Bekräftelsemejl misslyckades: {e}")
        return render_template("message.html",
            title="Något gick fel",
            message="Vi kunde inte skicka bekräftelsemailet just nu. "
                    "Försök igen om en stund — din adress är inte sparad.",
            icon="❌",
            cta_url="/",
            cta_text="Tillbaka till startsidan",
        ), 503

    return render_template("message.html",
        title="Kontrollera din e-post",
        message=f"Vi har skickat ett bekräftelsemejl till <strong>{email}</strong>. "
                "Klicka på länken i mailet för att aktivera bevakningen. "
                "<strong>Länken är giltig i 48 timmar</strong> — kolla skräpposten om du inte hittar mailet.",
        icon="✉️",
        cta_url="/",
        cta_text="Tillbaka till startsidan",
    )


@bp.route("/confirm/<token>")
def confirm(token):
    with get_db() as db:
        row = db.execute(
            "SELECT t.*, sub.email, sub.confirmed_at "
            "FROM tokens t JOIN subscribers sub ON t.subscriber_id=sub.id "
            "WHERE t.token=? AND t.type='confirm'",
            [token],
        ).fetchone()

        if not row:
            return render_template("message.html",
                title="Ogiltig länk",
                message="Länken hittades inte.",
                icon="❌"), 404

        if row["used_at"]:
            manage_row = db.execute(
                "SELECT token FROM tokens WHERE subscriber_id=? AND type='manage' "
                "AND used_at IS NULL AND expires_at > datetime('now') "
                "ORDER BY expires_at DESC LIMIT 1",
                [row["subscriber_id"]],
            ).fetchone()
            cta_url = f"{SITE_URL}/manage/{manage_row['token']}" if manage_row else "/"
            return render_template("message.html",
                title="Redan aktiverad",
                message="Din bevakning är redan aktiverad.",
                icon="✅",
                cta_url=cta_url,
                cta_text="Hantera dina bevakningar",
            )

        if row["expires_at"] < datetime.utcnow().isoformat():
            return render_template("message.html",
                title="Länken har gått ut",
                message="Bekräftelselänken gäller i 48 timmar och har nu gått ut. "
                        "Prenumerera igen för att få en ny länk.",
                icon="⏰",
                cta_url="/",
                cta_text="Till startsidan",
            ), 410

        # Mark token used + confirm subscriber + activate subscription
        db.execute("UPDATE tokens SET used_at=datetime('now') WHERE token=?", [token])
        db.execute(
            "UPDATE subscribers SET confirmed_at=datetime('now') WHERE id=? AND confirmed_at IS NULL",
            [row["subscriber_id"]],
        )
        if row["subscription_id"]:
            db.execute("UPDATE subscriptions SET active=1 WHERE id=?", [row["subscription_id"]])

        # Create manage token (30d)
        manage_token = get_or_create_token(db, "manage", row["subscriber_id"], None, ttl_hours=30 * 24)
        db.commit()

    return render_template("message.html",
        title="Bevakning aktiverad!",
        message="Din e-postadress är bekräftad och bevakningen är nu aktiv. "
                "Du får ett mail när läkemedlet finns i lager.",
        icon="✅",
        cta_url=f"{SITE_URL}/manage/{manage_token}",
        cta_text="Hantera dina bevakningar",
        umami_event="prenumeration-bekraftad",
    )
