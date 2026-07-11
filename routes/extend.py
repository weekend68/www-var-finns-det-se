from datetime import timedelta

from flask import Blueprint, render_template

from config import SITE_URL, SUBSCRIPTION_TTL_DAYS, token_url
from db import get_db, get_or_create_token, get_token, utcnow_str
from responses import invalid_link

bp = Blueprint("extend", __name__)


@bp.route("/extend/<token>")
def extend(token):
    with get_db() as db:
        row = get_token(db, token, "extend")

        if not row:
            return invalid_link()

        if row["used_at"]:
            return render_template("message.html",
                title="Redan förlängd",
                message="Den här länken har redan använts. Bevakningen är förlängd.",
                icon="✅",
                cta_url="/",
                cta_text="Till startsidan",
            )

        if row["expires_at"] < utcnow_str():
            return render_template("message.html",
                title="Länken har gått ut",
                message="Förlängningslänken har gått ut. Prenumerera igen om du vill fortsätta bevaka.",
                icon="⏰",
                cta_url="/",
                cta_text="Till startsidan",
            ), 410

        new_expires = utcnow_str(timedelta(days=SUBSCRIPTION_TTL_DAYS))
        db.execute("UPDATE tokens SET used_at=datetime('now') WHERE token=?", [token])
        if row["subscription_id"]:
            db.execute(
                "UPDATE subscriptions SET active=1, expires_at=? WHERE id=? AND subscriber_id=?",
                [new_expires, row["subscription_id"], row["subscriber_id"]],
            )

        manage_token = get_or_create_token(db, "manage", row["subscriber_id"], None)
        db.commit()

    return render_template("message.html",
        title="Bevakning förlängd!",
        message=f"Din bevakning är förlängd till {new_expires[:10]}.",
        icon="✅",
        cta_url=token_url(SITE_URL, "manage", manage_token),
        cta_text="Hantera dina bevakningar",
    )
