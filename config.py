"""Shared environment-derived configuration used across multiple modules."""

import os

SITE_URL = os.getenv("SITE_URL", "").rstrip("/")

# How long a subscription stays active before it needs renewing, and the TTL
# for the unsubscribe/manage tokens tied to it. One constant instead of the
# same "30 * 24" / "days=30" literal repeated independently across files.
SUBSCRIPTION_TTL_DAYS = 30

# Minimum time between two notification emails for the same subscription --
# a pharmacy's live stock status can flicker in/out several times within a
# single day (per Fass's own docs), so a plain "notify once per restock"
# rule sends far too many emails. Replaces the old approach of clearing
# last_notified_at as soon as a product was confirmed out of stock again.
NOTIFY_COOLDOWN_HOURS = 24

# Minimum number of consecutive polls with a new status before a stock-status
# flip is trusted as real, rather than a single noisy measurement -- fass.py's
# own check_stock() regularly logs incomplete per-poll coverage (e.g. "50/1453
# apotek kunde inte kollas"), so any one poll's pharmacy_count can swing to/
# from 0 even though the medication's actual stock status hasn't changed.
# Shared between two different mechanisms that both apply this same principle:
#   - checker.py's polling_loop(): a streaming state machine filtering one
#     live poll at a time (_consecutive_zeros/_consecutive_positives).
#   - routes/lakemedel.py's _stock_history(): a batch analysis replaying
#     already-stored poll_log rows to find the same kind of confirmed flip.
MIN_CONSECUTIVE_POLLS = 2

# poll_log rows recorded before this timestamp predate MIN_CONSECUTIVE_POLLS
# existing at all -- routes/lakemedel.py's _stock_history() must not treat
# them as trustworthy just because the new run-length scan is now applied to
# them retroactively. We don't actually know whether an old boundary reflects
# a real status change or two-plus consecutive noisy polls the new threshold
# would still have let through. Rather than guess, history only ever looks at
# rows at/after this cutoff -- if that means no confirmed transition is found
# yet, _stock_history() says "monitored since this date, no change seen",
# not a specific (possibly wrong) day count.
#
# Format matches poll_log.polled_at exactly (checker.py's _log_poll(), NOT
# db.utcnow_str()'s space-separated convention) -- "%Y-%m-%dT%H:%M:%S".
HISTORY_RELIABLE_SINCE = "2026-07-15T01:15:00"


def token_url(site_url, kind, token):
    """Build a token-bearing URL (manage/confirm/unsubscribe/extend) --
    the one place this "{site_url}/{kind}/{token}" shape is formatted,
    instead of independently in routes/subscribe.py, routes/extend.py and
    every mail.py send_* function."""
    return f"{site_url}/{kind}/{token}"
