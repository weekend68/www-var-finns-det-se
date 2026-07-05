import os
from datetime import datetime, timedelta

from flask import Blueprint, render_template

from db import get_db, get_or_create_token

bp = Blueprint("extend", __name__)
SITE_URL = os.getenv("SITE_URL", "").rstrip("/")


@bp.route("/extend/<token>")
def extend(token):
    with get_db() as db:
        row = db.execute(
            "SELECT t.*, sub.email "
            "FROM tokens t JOIN subscribers sub ON t.subscriber_id=sub.id "
            "WHERE t.token=? AND t.type='extend'",
            [token],
        ).fetchone()

        if not row:
            return render_template("message.html",
                title="Ogiltig länk",
                message="Länken hittades inte.",
                icon="❌"), 404

        if row["used_at"]:
            return render_template("message.html",
                title="Redan förlängd",
                message="Den här länken har redan använts. Bevakningen är förlängd.",
                icon="✅",
                cta_url="/",
                cta_text="Till startsidan",
            )

        if row["expires_at"] < datetime.utcnow().isoformat():
            return render_template("message.html",
                title="Länken har gått ut",
                message="Förlängningslänken har gått ut. Prenumerera igen om du vill fortsätta bevaka.",
                icon="⏰",
                cta_url="/",
                cta_text="Till startsidan",
            ), 410

        new_expires = (datetime.utcnow() + timedelta(days=30)).isoformat()
        db.execute("UPDATE tokens SET used_at=datetime('now') WHERE token=?", [token])
        if row["subscription_id"]:
            db.execute(
                "UPDATE subscriptions SET active=1, expires_at=? WHERE id=?",
                [new_expires, row["subscription_id"]],
            )

        manage_token = get_or_create_token(db, "manage", row["subscriber_id"], None, ttl_hours=30 * 24)
        db.commit()

    return render_template("message.html",
        title="Bevakning förlängd!",
        message=f"Din bevakning är förlängd till {new_expires[:10]}.",
        icon="✅",
        cta_url=f"{SITE_URL}/manage/{manage_token}",
        cta_text="Hantera dina bevakningar",
    )
