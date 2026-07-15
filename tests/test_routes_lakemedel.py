import json
import re

import national_shortages as ns
from tests.conftest import SAMPLE_SHORTAGES_XML


def _jsonld_blocks(html):
    return [json.loads(b) for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]


def test_lakemedel_page_faq_html_matches_jsonld(client, ready_db):
    ns.refresh_national_shortages(SAMPLE_SHORTAGES_XML)
    r = client.get("/lakemedel/20040113100574-estradot-25-mikrogram-24-timmar-depotplaster")
    assert r.status_code == 200
    body = r.get_data(as_text=True)

    dl_match = re.search(r'<dl class="faq">(.*?)</dl>', body, re.S)
    assert dl_match, "no visible FAQ <dl> found"
    questions_html = [re.sub("<.*?>", "", q).strip() for q in re.findall(r"<dt>(.*?)</dt>", dl_match.group(1), re.S)]

    blocks = _jsonld_blocks(body)
    faq_block = next(b for b in blocks if b.get("@type") == "FAQPage")
    questions_jsonld = [item["name"] for item in faq_block["mainEntity"]]

    assert questions_html == questions_jsonld
    assert len(questions_html) > 0


def test_lakemedel_page_has_no_product_or_drug_jsonld(client, ready_db):
    """Regression test: Product/Drug JSON-LD was removed 2026-07-15 -- it
    could never validly satisfy Google's Product rich-result requirements
    (no price, no reviews/ratings on a stock checker), and re-adding it in
    any form (bare Product, or Drug -- itself a schema.org subtype of
    Product) risks the same permanent "invalid" Search Console flag."""
    ns.refresh_national_shortages(SAMPLE_SHORTAGES_XML)
    r = client.get("/lakemedel/20040113100574-estradot-25-mikrogram-24-timmar-depotplaster")
    body = r.get_data(as_text=True)
    types = {b.get("@type") for b in _jsonld_blocks(body)}
    assert "Product" not in types
    assert "Drug" not in types
    assert "FAQPage" in types
    assert "BreadcrumbList" in types


def test_partner_guide_shown_for_estradiol_product(client, ready_db):
    ns.refresh_national_shortages(SAMPLE_SHORTAGES_XML)
    r = client.get("/lakemedel/20040113100574-estradot-25-mikrogram-24-timmar-depotplaster")
    assert 'id="partner-guide-link"' in r.get_data(as_text=True)


def test_partner_guide_hidden_for_non_estradiol_product(client, ready_db):
    """Litiumklorid (ATC V04CX11) isn't Estradiol -- must not show the
    klimakteriet/partnerguiden puff, whether curated or not (the puff is
    now driven purely by medications.atc_code, see routes/lakemedel.py's
    ESTRADIOL_ATC_CODE)."""
    ns.refresh_national_shortages(SAMPLE_SHORTAGES_XML)
    import db as dbmod
    import slugs

    with dbmod.get_db() as con:
        row = con.execute(
            "SELECT npl_pack_id, name FROM medications WHERE npl_pack_id=?", ["20040831100191"]
        ).fetchone()
    url = slugs.medication_url("http://testserver", row["npl_pack_id"], row["name"]).replace("http://testserver", "")
    r = client.get(url)
    assert r.status_code == 200
    assert 'id="partner-guide-link"' not in r.get_data(as_text=True)


def test_lakemedel_page_redirects_to_canonical_slug(client, ready_db):
    ns.refresh_national_shortages(SAMPLE_SHORTAGES_XML)
    r = client.get("/lakemedel/20040113100574-wrong-slug-here")
    assert r.status_code == 301
    assert "estradot" in r.headers["Location"].lower()


def test_unknown_medication_returns_404(client, monkeypatch):
    """An id with no medications row falls back to a live fass.lookup_name()
    call (routes/lakemedel.py) -- stub it out so this test stays hermetic
    (no real network call to Fass, matching issue #1's "no live Fass calls
    in CI" constraint) and deterministic (a real Fass response could
    change over time)."""
    import fass

    monkeypatch.setattr(fass, "lookup_name", lambda npl_pack_id: None)
    r = client.get("/lakemedel/99999999999999-nagot")
    assert r.status_code == 404
