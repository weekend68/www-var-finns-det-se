import sqlite3
from datetime import timedelta

from flask import Blueprint, render_template, request
from markupsafe import escape

import checker
import mail
from config import SITE_URL, SUBSCRIPTION_TTL_DAYS, token_url
from db import create_token, get_db, get_medication, get_or_create_token, get_token, utcnow_str
from responses import invalid_link

bp = Blueprint("subscribe", __name__)


def _lookup_med_name(npl_pack_id):
    med_name = next((p["name"] for p in checker.PRODUCTS if p["npl_pack_id"] == npl_pack_id), "")
    if not med_name and npl_pack_id:
        try:
            with get_db() as db:
                m = get_medication(db, npl_pack_id)
            if m and m["name"] != npl_pack_id:
                med_name = m["name"]
        except Exception:
            pass
    return med_name


@bp.route("/subscribe", methods=["GET", "POST"])
def subscribe():
    if request.method == "GET":
        npl = request.args.get("npl", "").strip()
        return render_template("subscribe.html", npl_pack_id=npl, med_name=_lookup_med_name(npl), error=None)

    email = request.form.get("email", "").strip().lower()
    npl_pack_id = request.form.get("npl_pack_id", "").strip()
    consent = request.form.get("consent")

    def form_error(msg):
        return render_template(
            "subscribe.html", npl_pack_id=npl_pack_id, med_name=_lookup_med_name(npl_pack_id), error=msg
        ), 400

    if not email or "@" not in email or "." not in email.split("@")[-1]:
        return form_error("Ogiltig e-postadress.")
    if not consent:
        return form_error("Du måste godkänna integritetspolicyn för att prenumerera.")
    if not npl_pack_id:
        return form_error("Inget läkemedel valt.")

    with get_db() as db:
        med = get_medication(db, npl_pack_id)
        if not med:
            return render_template("message.html",
                title="Okänt läkemedel",
                message="Det valda läkemedlet hittades inte.",
                icon="❌"), 404

        # Get or create subscriber. A double-submit of this form for a brand
        # new email can race two requests past the "no existing row" check
        # before either commits its INSERT -- catch the resulting UNIQUE
        # violation and fall back to the row the other request just created,
        # instead of letting it surface as an uncaught 500.
        row = db.execute("SELECT id FROM subscribers WHERE email=?", [email]).fetchone()
        if row:
            subscriber_id = row["id"]
            db.execute("UPDATE subscribers SET deleted_at=NULL WHERE id=?", [subscriber_id])
        else:
            try:
                cur = db.execute("INSERT INTO subscribers (email) VALUES (?)", [email])
                subscriber_id = cur.lastrowid
            except sqlite3.IntegrityError:
                row = db.execute("SELECT id FROM subscribers WHERE email=?", [email]).fetchone()
                subscriber_id = row["id"]
                db.execute("UPDATE subscribers SET deleted_at=NULL WHERE id=?", [subscriber_id])

        # Create or reactivate subscription -- same race/recovery as above.
        expires_at = utcnow_str(timedelta(days=SUBSCRIPTION_TTL_DAYS))
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
            try:
                cur = db.execute(
                    "INSERT INTO subscriptions (subscriber_id, npl_pack_id, expires_at) VALUES (?,?,?)",
                    [subscriber_id, npl_pack_id, expires_at],
                )
                subscription_id = cur.lastrowid
            except sqlite3.IntegrityError:
                existing = db.execute(
                    "SELECT id FROM subscriptions WHERE subscriber_id=? AND npl_pack_id=?",
                    [subscriber_id, npl_pack_id],
                ).fetchone()
                subscription_id = existing["id"]
                db.execute(
                    "UPDATE subscriptions SET active=1, expires_at=? WHERE id=?",
                    [expires_at, subscription_id],
                )

        # Invalidate any pending confirm tokens (re-subscribe case)
        db.execute(
            "UPDATE tokens SET used_at=datetime('now') WHERE subscriber_id=? AND type='confirm' AND used_at IS NULL",
            [subscriber_id],
        )

        # Create confirm token (48h TTL)
        token = create_token(db, "confirm", subscriber_id, subscription_id, ttl_hours=48)

        # Pre-create unsubscribe token for this subscription (idempotent, 30d default TTL)
        get_or_create_token(db, "unsubscribe", subscriber_id, subscription_id)

        db.commit()

    try:
        sent = mail.send_confirmation(email, token, SITE_URL, medication_name=med["name"])
    except Exception as e:
        sent = False
        print(f"  Bekräftelse-e-post misslyckades: {e}")

    if not sent:
        # send_confirmation returns False (daily mail cap reached, no
        # exception) as well as raising on a hard failure -- either way the
        # confirm token was already committed above, but if we said "we sent
        # it" here the subscriber would wait forever for an email that never
        # went out, with no way to retry.
        return render_template("message.html",
            title="Något gick fel",
            message="Vi kunde inte skicka bekräftelsen just nu. "
                    "Försök igen om en stund — din adress är inte sparad.",
            icon="❌",
            cta_url="/",
            cta_text="Tillbaka till startsidan",
        ), 503

    return render_template("message.html",
        title="Kontrollera din e-post",
        message=f"Vi har skickat en bekräftelse via e-post till <strong>{escape(email)}</strong>. "
                "Klicka på länken i e-postmeddelandet för att aktivera bevakningen. "
                "<strong>Länken är giltig i 48 timmar</strong> — kolla skräpposten om du inte hittar det.",
        icon="✉️",
        cta_url="/",
        cta_text="Tillbaka till startsidan",
    )


@bp.route("/confirm/<token>")
def confirm(token):
    with get_db() as db:
        row = get_token(db, token, "confirm")

        if not row:
            return invalid_link()

        if row["used_at"]:
            manage_row = db.execute(
                "SELECT token FROM tokens WHERE subscriber_id=? AND type='manage' "
                "AND used_at IS NULL AND expires_at > datetime('now') "
                "ORDER BY expires_at DESC LIMIT 1",
                [row["subscriber_id"]],
            ).fetchone()
            cta_url = token_url(SITE_URL, "manage", manage_row["token"]) if manage_row else "/"
            return render_template("message.html",
                title="Redan aktiverad",
                message="Din bevakning är redan aktiverad.",
                icon="✅",
                cta_url=cta_url,
                cta_text="Hantera dina bevakningar",
            )

        if row["expires_at"] < utcnow_str():
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

        # Create manage token (30d default TTL)
        manage_token = get_or_create_token(db, "manage", row["subscriber_id"], None)
        db.commit()

    return render_template("message.html",
        title="Bevakning aktiverad!",
        message="Din e-postadress är bekräftad och bevakningen är nu aktiv. "
                "Du får ett e-postmeddelande när läkemedlet finns i lager.",
        icon="✅",
        cta_url=token_url(SITE_URL, "manage", manage_token),
        cta_text="Hantera dina bevakningar",
        umami_event="prenumeration-bekraftad",
    )
