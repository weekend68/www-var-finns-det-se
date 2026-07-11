"""Shared message.html response builders, used identically across routes."""

from flask import render_template


def invalid_link(message="Länken hittades inte.", status=404):
    return render_template("message.html",
        title="Ogiltig länk",
        message=message,
        icon="❌",
    ), status
