"""Lightweight postal-code-prefix grouping of pharmacies — no geocoding.

Groups on the first 3 digits of a Swedish postal code (postort level, e.g.
171xx = Solna) as the primary "Nära dig" match; falls back to the first 2
digits ("I regionen") if too few pharmacies match at 3-digit precision.
Never hides anything — the full pharmacy list is always nara + region + rest.
"""

import re


def normalize_omrade(raw):
    """Truncate any postal-code-like input down to its first 3 digits — the
    only precision the grouping below actually uses. Returns None if fewer
    than 2 digits are present (nothing usable to group on)."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) < 2:
        return None
    return digits[:3]


def _pharmacy_key(ph):
    # Object identity, not a synthetic gln/name key -- two distinct pharmacy
    # dicts that happen to share a name (or both lack gln) must never collide
    # here. A collision means only ONE of them lands in nara/region while both
    # get excluded from rest, silently dropping the other entirely.
    return id(ph)


def _postal_digits(ph):
    return re.sub(r"\D", "", ph.get("postalcode", "") or "")


def group_pharmacies_by_omrade(pharmacies, omrade, min_nearby=3):
    """
    omrade: a 2-3 digit postal-code prefix (as returned by normalize_omrade).
    Returns (nara, region, rest):
      nara   — exact 3-digit prefix match ("Nära dig")
      region — 2-digit prefix match minus nara ("I regionen"), only
               populated if len(nara) < min_nearby
      rest   — everything else, unfiltered order preserved
    """
    if not omrade:
        return [], [], pharmacies

    p3 = omrade if len(omrade) == 3 else None
    p2 = omrade[:2]

    nara = [ph for ph in pharmacies if p3 and _postal_digits(ph)[:3] == p3]

    if len(nara) >= min_nearby:
        region = []
    else:
        nara_keys = {_pharmacy_key(ph) for ph in nara}
        region = [
            ph for ph in pharmacies
            if _postal_digits(ph)[:2] == p2 and _pharmacy_key(ph) not in nara_keys
        ]

    grouped_keys = {_pharmacy_key(ph) for ph in nara + region}
    rest = [ph for ph in pharmacies if _pharmacy_key(ph) not in grouped_keys]
    return nara, region, rest
