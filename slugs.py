"""SEO slug generation for medication deep-link URLs (/lakemedel/<npl_pack_id>-<slug>)."""

import re
import unicodedata


def _transliterate(s):
    """Strip all diacritics (å/ä/ö, é/ü/ñ, etc.) via Unicode decomposition."""
    decomposed = unicodedata.normalize("NFKD", s)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _to_slug_part(s):
    s = _transliterate(s).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def slugify_medication(name, strength=None, form=None):
    """
    Build a cosmetic, SEO-friendly slug from a medication's name (+ strength/form
    if not already embedded in the name, since seeded/Fass names usually already
    include strength and form, e.g. "Estradot 25 mcg depotplåster").
    """
    name = name or ""
    parts = [name]

    name_lower = name.lower()
    if strength and strength.lower() not in name_lower:
        parts.append(strength)
    if form and form.lower() not in name_lower:
        parts.append(form)

    combined = " ".join(p for p in parts if p)
    slug = _to_slug_part(combined)

    # Collapse CONSECUTIVE repeated tokens (e.g. strength digits re-appearing
    # back-to-back because the name and strength fields format the same
    # value slightly differently). Deliberately not a global dedup: a digit
    # like "1" can legitimately reappear far apart for unrelated reasons
    # (e.g. a package multiplier "1 x 56 dos" after a "1,53 mg" strength
    # earlier in the same name) -- deduping those away as if they were the
    # same token silently drops meaningful package info from the slug.
    tokens = []
    for tok in slug.split("-"):
        if not tokens or tokens[-1] != tok:
            tokens.append(tok)
    return "-".join(tokens)


def medication_url(site_url, npl_pack_id, name, strength=None, form=None):
    """Absolute canonical /lakemedel/ deep link -- the single place that
    combines slugify_medication() with the URL shape, so app.py's sitemap,
    checker.py's notification emails and routes/lakemedel.py's own
    canonical_url all agree by construction instead of independently
    formatting the same "{site_url}/lakemedel/{id}-{slug}" string."""
    slug = slugify_medication(name, strength, form)
    return f"{site_url}/lakemedel/{npl_pack_id}-{slug}"


def slugify_category(atc_term_or_code):
    """
    Build a cosmetic, SEO-friendly slug for a /kategori/ page from its ATC
    substance term (e.g. "Atomoxetin"), analogous to slugify_medication()
    above. Callers should pass the ATC term when available and fall back to
    the ATC code itself otherwise (get_shortage_category() always has at
    least one of the two for a qualifying category).
    """
    return _to_slug_part(atc_term_or_code or "")


def category_url(site_url, atc_code, atc_term):
    """Absolute canonical /kategori/ deep link -- same pattern as
    medication_url() above, so app.py's sitemap, routes/kategori.py's own
    canonical_url and every internal link to a category page all agree by
    construction instead of independently formatting the same
    "{site_url}/kategori/{atc_code}-{slug}" string."""
    slug = slugify_category(atc_term or atc_code)
    return f"{site_url}/kategori/{atc_code}-{slug}"
