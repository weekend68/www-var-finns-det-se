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
# Raised from 24 to 168 (a week) 2026-07-23 after a week-long poll_log
# analysis (see GitHub issue #6) confirmed a handful of near-zero-stock
# products (e.g. Lenzetto, pendling 0-3 pharmacies nationally) flip status
# often enough that even MIN_CONSECUTIVE_POLLS=2 occasionally reads the
# noise as a genuine restock. This doesn't stop a false-positive email from
# firing at all -- it only limits how often one can repeat for the same
# subscription, deliberately chosen over adding a minimum-pharmacy-count
# threshold (which would fix correctness but was decided against for now).
NOTIFY_COOLDOWN_HOURS = 168

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

# Same principle as HISTORY_RELIABLE_SINCE above, for poll_log.glns_failed
# (added 2026-07-23, alongside checker.STAGGER_SECONDS). The column's
# db.py migration backfills existing rows with 0 -- that's a schema
# default, not a real measurement of "no fetch failures that day". Rows
# before this cutoff would silently understate historical failure rates
# as if they were perfect. routes/admin.py's _fetch_quality() only shows
# days at/after this timestamp.
GLNS_FAILED_RELIABLE_SINCE = "2026-07-23T21:00:00"

# How long checker.py's _log_poll() keeps poll_log rows before pruning them.
# Was a flat "keep the last 2000 rows" cap (global, shared across every
# actively-polled product), which at production's POLL_INTERVAL and product
# count only retained a few days of history -- far too short to analyze
# flapping patterns (run lengths, isolated blips) over a meaningful window.
# Time-based instead of row-count-based so retention doesn't shrink as more
# products get polled (subscriptions grow the product count over time).
POLL_LOG_RETENTION_DAYS = 90

# How long Cloudflare/browsers may cache the public, non-personalized HTML
# pages (index, om, privacy, jamforelse, lakemedel, kategori*, log) at the
# edge. Deliberately short relative to POLL_INTERVAL (30 min in production)
# -- the underlying stock data is never fresher than that anyway, and the
# homepage's own staleness banner re-checks /healthz client-side regardless
# of how old the cached HTML is (see templates/index.html). A short cache
# still meaningfully cuts origin hits from bots/repeat visitors without
# adding noticeable extra staleness.
CONTENT_MAX_AGE = 120
CONTENT_STALE_WHILE_REVALIDATE = 300

# /admin (routes/admin.py) is HTTP Basic Auth-gated by this password. Empty
# (unset) disables the route entirely (404) rather than serving a login
# prompt nobody can pass -- a deploy that forgets to set this must fail
# closed, not expose an unguessable-but-technically-reachable login form.
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")


def token_url(site_url, kind, token):
    """Build a token-bearing URL (manage/confirm/unsubscribe/extend) --
    the one place this "{site_url}/{kind}/{token}" shape is formatted,
    instead of independently in routes/subscribe.py, routes/extend.py and
    every mail.py send_* function."""
    return f"{site_url}/{kind}/{token}"
