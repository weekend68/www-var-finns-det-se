import os

from flask import Blueprint, redirect, render_template, request, url_for

from db import get_db

bp = Blueprint("manage", __name__)
SITE_URL = os.getenv("SITE_URL", "").rstrip("/")


def _get_subscriber(db, token):
    return db.execute(
        "SELECT t.subscriber_id, sub.email "
        "FROM tokens t JOIN subscribers sub ON t.subscriber_id=sub.id "
        "WHERE t.token=? AND t.type='manage' AND t.used_at IS NULL AND t.expires_at > datetime('now')",
        [token],
    ).fetchone()


@bp.route("/manage/<token>")
def manage(token):
    with get_db() as db:
        auth = _get_subscriber(db, token)
        if not auth:
            return render_template("message.html",
                title="Ogiltig länk",
                message="Länken hittades inte eller har gått ut.",
                icon="❌"), 404

        subs = db.execute(
            "SELECT s.id, s.npl_pack_id, s.expires_at, s.last_notified_at, s.active, m.name "
            "FROM subscriptions s JOIN medications m ON s.npl_pack_id=m.npl_pack_id "
            "WHERE s.subscriber_id=? AND s.active=1 ORDER BY s.created_at",
            [auth["subscriber_id"]],
        ).fetchall()

    return render_template("manage.html",
        token=token,
        email=auth["email"],
        subscriptions=[dict(s) for s in subs],
        site_url=SITE_URL,
    )


@bp.route("/manage/<token>/remove", methods=["POST"])
def remove(token):
    subscription_id = request.form.get("subscription_id", type=int)
    with get_db() as db:
        auth = _get_subscriber(db, token)
        if not auth:
            return render_template("message.html",
                title="Ogiltig länk",
                message="Länken hittades inte eller har gått ut.",
                icon="❌"), 403

        if subscription_id:
            db.execute(
                "UPDATE subscriptions SET active=0 WHERE id=? AND subscriber_id=?",
                [subscription_id, auth["subscriber_id"]],
            )
            db.commit()

    return redirect(url_for("manage.manage", token=token))
