import checker


def test_products_is_curation_only():
    """PRODUCTS must carry npl_pack_id only -- no display data (name,
    strength, form, manufacturer, ...). That data lives exclusively in the
    medications table, backfilled by national_shortages.py, so it can never
    drift out of sync between two independently-maintained sources (see
    checker.py's module-level PRODUCTS docstring)."""
    assert len(checker.PRODUCTS) == 10
    for p in checker.PRODUCTS:
        assert set(p.keys()) == {"npl_pack_id"}
        assert p["npl_pack_id"].isdigit()


def test_seed_products_only_creates_placeholders(ready_db):
    import db as dbmod

    with dbmod.get_db() as con:
        for p in checker.PRODUCTS:
            row = con.execute(
                "SELECT name FROM medications WHERE npl_pack_id=?", [p["npl_pack_id"]]
            ).fetchone()
            assert row is not None
            assert row["name"] == p["npl_pack_id"]  # placeholder: name == npl_pack_id


def test_seed_products_never_overwrites_existing_row(ready_db):
    """seed_products() must be safe to call again (e.g. every app startup)
    without clobbering data a later backfill already filled in."""
    import db as dbmod

    npl_pack_id = checker.PRODUCTS[0]["npl_pack_id"]
    with dbmod.get_db() as con:
        con.execute("UPDATE medications SET name=? WHERE npl_pack_id=?", ["Riktigt namn", npl_pack_id])
        con.commit()

    checker.seed_products()

    with dbmod.get_db() as con:
        row = con.execute("SELECT name FROM medications WHERE npl_pack_id=?", [npl_pack_id]).fetchone()
        assert row["name"] == "Riktigt namn"


def test_staleness_tier_thresholds():
    from datetime import datetime, timedelta

    now = checker.now_local()
    fresh = (now - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    one_hour = (now - timedelta(hours=1, minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    three_hours = (now - timedelta(hours=3, minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    one_day = (now - timedelta(days=1, minutes=5)).strftime("%Y-%m-%d %H:%M:%S")

    assert checker.staleness_tier(fresh) is None
    assert checker.staleness_tier(one_hour) == "1h"
    assert checker.staleness_tier(three_hours) == "3h"
    assert checker.staleness_tier(one_day) == "1d"
    assert checker.staleness_tier(None) is None
