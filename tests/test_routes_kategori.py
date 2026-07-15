import national_shortages as ns
from slugs import category_url
from tests.conftest import SAMPLE_SHORTAGES_XML


def _seed_category_with_n_products(db_path, n, atc_code="Q99ZZ01", atc_term="Testkategori"):
    """national_shortages.npl_pack_id has a FK into medications, so a
    synthetic multi-product category needs a matching medications row per
    package too -- the sample fixture only carries 1-2 products per ATC
    code (below DEFAULT_MIN_PRODUCTS=3), so kategori.html's actual detail
    route can't be reached through it alone."""
    import sqlite3

    con = sqlite3.connect(db_path)
    for i in range(n):
        npl_pack_id = f"9999999999999{i}"
        con.execute(
            "INSERT INTO medications (npl_pack_id, name) VALUES (?, ?)",
            [npl_pack_id, f"Testmedel {i}"],
        )
        con.execute(
            "INSERT INTO national_shortages "
            "(npl_pack_id, npl_id, product_name, atc_code, atc_term, forecasted_start, is_active) "
            "VALUES (?, ?, ?, ?, ?, '2026-01-01', 1)",
            [npl_pack_id, f"npl-{i}", f"Testmedel {i}", atc_code, atc_term],
        )
    con.commit()
    con.close()


def test_kategorier_index_renders(client, ready_db):
    ns.refresh_national_shortages(SAMPLE_SHORTAGES_XML)
    r = client.get("/kategorier")
    assert r.status_code == 200
    assert "Alla läkemedelsgrupper med kända bristsituationer" in r.get_data(as_text=True)


def test_kategori_detail_page_renders(client, ready_db):
    _seed_category_with_n_products(ready_db, 3)
    url = category_url("", "Q99ZZ01", "Testkategori")
    r = client.get(url)
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Testkategori" in body
    assert "Produkter i den här gruppen" in body
    assert "Testmedel 0" in body
    assert "Vanliga frågor" in body


def test_kategori_detail_redirects_wrong_slug(client, ready_db):
    _seed_category_with_n_products(ready_db, 3)
    r = client.get("/kategori/Q99ZZ01-fel-slug")
    assert r.status_code == 301
    assert r.headers["Location"].endswith(category_url("", "Q99ZZ01", "Testkategori"))
