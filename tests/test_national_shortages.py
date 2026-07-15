import national_shortages as ns
from tests.conftest import SAMPLE_SHORTAGES_XML


def test_parse_reads_expected_fields():
    rows = ns._parse(SAMPLE_SHORTAGES_XML)
    assert set(rows) == {"20040113100574", "20040831100191", "20180608100143"}

    estradot = rows["20040113100574"]
    assert estradot["product_name"] == "Estradot 25 mikrogram/24 timmar Depotplåster"
    assert estradot["atc_code"] == "G03CA03"
    assert estradot["atc_term"] == "Estradiol"
    assert estradot["manufacturer"] == "Sandoz A/S"
    assert estradot["is_active"] == 1
    assert estradot["actual_end"] is None

    bondil = rows["20180608100143"]
    assert bondil["is_active"] == 0
    assert bondil["actual_end"] == "2026-05-25"


def test_refresh_backfills_curated_placeholder_to_real_data(ready_db):
    """checker.seed_products() (run by the ready_db fixture) only ever
    inserts a name==npl_pack_id placeholder for curated ids -- the daily
    catalogue sync must resolve it to the real Läkemedelsverket name/
    npl_id/manufacturer/atc_code, exactly like any other catalogue
    product (see checker.py's PRODUCTS docstring)."""
    import db as dbmod

    with dbmod.get_db() as con:
        before = con.execute(
            "SELECT name FROM medications WHERE npl_pack_id=?", ["20040113100574"]
        ).fetchone()
        assert before["name"] == "20040113100574"  # still a placeholder

    stats = ns.refresh_national_shortages(SAMPLE_SHORTAGES_XML)
    assert stats["packages"] == 3
    assert stats["medications_backfilled"] == 3  # 2 new catalog rows + 1 curated placeholder healed

    with dbmod.get_db() as con:
        row = con.execute(
            "SELECT name, npl_id, manufacturer, atc_code, package_description, form "
            "FROM medications WHERE npl_pack_id=?",
            ["20040113100574"],
        ).fetchone()
        assert row["name"] == "Estradot 25 mikrogram/24 timmar Depotplåster"
        assert row["npl_id"] == "20040607005750"
        assert row["manufacturer"] == "Sandoz A/S"
        assert row["atc_code"] == "G03CA03"
        assert row["package_description"] == "Påse, 24 x 1 depotplåster"
        assert row["form"] is None  # never guessed, see _backfill_medications() docstring


def test_refresh_is_idempotent(ready_db):
    ns.refresh_national_shortages(SAMPLE_SHORTAGES_XML)
    stats2 = ns.refresh_national_shortages(SAMPLE_SHORTAGES_XML)
    assert stats2["medications_backfilled"] == 0


def test_refresh_heals_stale_pre_existing_name(ready_db):
    """Regression test: a row seeded by an OLDER version of the app (real
    name/npl_id/manufacturer already set, but never package_description or
    atc_code -- exactly what checker.py's PRODUCTS used to write before it
    became curation-only) must still get healed to the feed's current
    name/atc_code, not stay stuck on its outdated value forever. This is
    the bug found live on beta 2026-07-15 (a stale "mcg"-named row never
    matched the old is_placeholder-or-name-already-matches condition)."""
    import db as dbmod

    with dbmod.get_db() as con:
        con.execute(
            "UPDATE medications SET name=?, strength=?, form=?, npl_id=?, manufacturer=? "
            "WHERE npl_pack_id=?",
            [
                "Estradot 25 mcg depotplåster", "25 mcg/24 h", "depotplåster",
                "20040607005750", "Sandoz A/S", "20040113100574",
            ],
        )
        con.commit()

    ns.refresh_national_shortages(SAMPLE_SHORTAGES_XML)

    with dbmod.get_db() as con:
        row = con.execute(
            "SELECT name, form FROM medications WHERE npl_pack_id=?", ["20040113100574"]
        ).fetchone()
        assert row["name"] == "Estradot 25 mikrogram/24 timmar Depotplåster"
        assert row["form"] is None


def test_resolved_shortage_marked_inactive(ready_db):
    import db as dbmod

    ns.refresh_national_shortages(SAMPLE_SHORTAGES_XML)
    with dbmod.get_db() as con:
        row = con.execute(
            "SELECT is_active, actual_end FROM national_shortages WHERE npl_pack_id=?",
            ["20180608100143"],
        ).fetchone()
        assert row["is_active"] == 0
        assert row["actual_end"] == "2026-05-25"


def test_get_shortage_categories_groups_by_atc(ready_db):
    import db as dbmod

    ns.refresh_national_shortages(SAMPLE_SHORTAGES_XML)
    with dbmod.get_db() as con:
        # Only 1 active product per ATC in the fixture, below any sane
        # min_products threshold -- explicitly pass 1 so this test isn't
        # coupled to national_shortages.DEFAULT_MIN_PRODUCTS' current value.
        categories = ns.get_shortage_categories(con, min_products=1)
    atc_codes = {c["atc_code"] for c in categories}
    assert "G03CA03" in atc_codes
    assert "V04CX11" in atc_codes
    # Bondil's shortage is resolved (is_active=0) -- must not count as an
    # active category.
    assert "G04BE01" not in atc_codes
