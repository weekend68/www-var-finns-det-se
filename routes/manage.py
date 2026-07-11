from flask import Blueprint, redirect, render_template, request, url_for

from db import get_db, get_token, utcnow_str
from responses import invalid_link

bp = Blueprint("manage", __name__)


def _get_subscriber(db, token):
    row = get_token(db, token, "manage")
    if not row or row["used_at"] or row["expires_at"] < utcnow_str():
        return None
    return row


@bp.route("/manage/<token>")
def manage(token):
    with get_db() as db:
        auth = _get_subscriber(db, token)
        if not auth:
            return invalid_link("Länken hittades inte eller har gått ut.")

        subs = db.execute(
            "SELECT s.id, s.npl_pack_id, s.expires_at, s.last_notified_at, s.active, m.name "
            "FROM subscriptions s JOIN medications m ON s.npl_pack_id=m.npl_pack_id "
            "WHERE s.subscriber_id=? AND s.active=1 ORDER BY s.created_at",
            [auth["subscriber_id"]],
        ).fetchall()

    subscriptions = [dict(s) for s in subs]

    return render_template("manage.html",
        token=token,
        email=auth["email"],
        subscriptions=subscriptions,
    )


@bp.route("/manage/<token>/remove", methods=["POST"])
def remove(token):
    subscription_id = request.form.get("subscription_id", type=int)
    with get_db() as db:
        auth = _get_subscriber(db, token)
        if not auth:
            return invalid_link("Länken hittades inte eller har gått ut.", status=403)

        if subscription_id:
            db.execute(
                "UPDATE subscriptions SET active=0 WHERE id=? AND subscriber_id=?",
                [subscription_id, auth["subscriber_id"]],
            )
            db.commit()

    return redirect(url_for("manage.manage", token=token))
