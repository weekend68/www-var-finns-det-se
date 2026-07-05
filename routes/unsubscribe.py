from flask import Blueprint, render_template

from db import get_db

bp = Blueprint("unsubscribe", __name__)


@bp.route("/unsubscribe/<token>")
def unsubscribe(token):
    with get_db() as db:
        row = db.execute(
            "SELECT t.subscriber_id, t.subscription_id, sub.email "
            "FROM tokens t JOIN subscribers sub ON t.subscriber_id=sub.id "
            "WHERE t.token=? AND t.type='unsubscribe'",
            [token],
        ).fetchone()

        if not row:
            return render_template("message.html",
                title="Ogiltig länk",
                message="Länken hittades inte.",
                icon="❌"), 404

        # Idempotent — deactivate subscription, don't mark token used
        if row["subscription_id"]:
            db.execute(
                "UPDATE subscriptions SET active=0 WHERE id=? AND subscriber_id=?",
                [row["subscription_id"], row["subscriber_id"]],
            )
        else:
            # Unsubscribe all subscriptions for this subscriber
            db.execute(
                "UPDATE subscriptions SET active=0 WHERE subscriber_id=?",
                [row["subscriber_id"]],
            )
        db.commit()

    return render_template("message.html",
        title="Avregistrerad",
        message="Du är nu avregistrerad och kommer inte att få fler notiser. "
                "Bevakningen avslutas automatiskt.",
        icon="✅",
        cta_url="/",
        cta_text="Till startsidan",
    )
