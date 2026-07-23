"""Cache-Control helper for cacheable-at-the-edge GET routes.

Only apply set_cache() to public, non-personalized responses. Never apply
it to routes/admin.py (Basic Auth) or token-bearing routes (subscribe/
confirm/manage/extend/unsubscribe) -- a shared cache (Cloudflare) serving
one visitor's cached response to another there would leak private content,
not just serve something stale.
"""

from flask import make_response


def set_cache(response, max_age, stale_while_revalidate=None):
    # render_template() returns a plain str, not a Response, until Flask
    # itself wraps a view function's return value -- make_response() handles
    # both that and an already-built Response (e.g. from Response(...))
    # uniformly.
    response = make_response(response)
    directive = f"public, max-age={max_age}"
    if stale_while_revalidate:
        directive += f", stale-while-revalidate={stale_while_revalidate}"
    response.headers["Cache-Control"] = directive
    return response
