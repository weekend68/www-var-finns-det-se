"""Shared environment-derived configuration used across multiple modules."""

import os

SITE_URL = os.getenv("SITE_URL", "").rstrip("/")

# How long a subscription stays active before it needs renewing, and the TTL
# for the unsubscribe/manage tokens tied to it. One constant instead of the
# same "30 * 24" / "days=30" literal repeated independently across files.
SUBSCRIPTION_TTL_DAYS = 30


def token_url(site_url, kind, token):
    """Build a token-bearing URL (manage/confirm/unsubscribe/extend) --
    the one place this "{site_url}/{kind}/{token}" shape is formatted,
    instead of independently in routes/subscribe.py, routes/extend.py and
    every mail.py send_* function."""
    return f"{site_url}/{kind}/{token}"
