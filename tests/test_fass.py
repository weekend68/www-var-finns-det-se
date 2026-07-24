import fass


class FakeHTTPError(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP Error {code}")
        self.code = code


def test_check_stock_detects_blocked_product(monkeypatch):
    """Every call 400s, including the single-GLN probe -- e.g. a
    narcotic-classified medication Fass permanently refuses to look up
    (confirmed 2026-07-24 for Metadon 2care4/Abcur). Must be reported as
    blocked, not retried batch by batch forever."""
    calls = []

    def fake_post(path, body):
        calls.append(list(body))
        raise FakeHTTPError(400)

    monkeypatch.setattr(fass, "_proxy_post", fake_post)

    gln_codes = [str(i) for i in range(120)]  # spans 3 batches of 50
    pharmacies, failed_glns, blocked = fass.check_stock("20170823100318", gln_codes, {})

    assert blocked is True
    assert pharmacies == []
    assert failed_glns == len(gln_codes)
    # Remaining batches must be skipped once blocked is confirmed on the
    # first one -- not retried in sub-batches of 10 for every batch.
    assert len(calls) < 10


def test_check_stock_not_blocked_on_ordinary_400(monkeypatch):
    """A 400 on the full batch that clears on a smaller probe/sub-batch is
    just a few unknown GLNs -- not a blocked product. Must fall back to the
    existing sub-batch-of-10 handling instead of giving up early."""
    bad_gln = "999"

    def fake_post(path, body):
        if bad_gln in body:
            raise FakeHTTPError(400)
        return [{"glnCode": g, "stockInformation": "IN_STOCK", "exchangeableProductInStock": False} for g in body]

    monkeypatch.setattr(fass, "_proxy_post", fake_post)

    gln_codes = [str(i) for i in range(49)] + [bad_gln]  # one batch of 50, one bad GLN
    pharmacy_map = {g: {"name": g} for g in gln_codes}
    pharmacies, failed_glns, blocked = fass.check_stock("20001018100021", gln_codes, pharmacy_map)

    assert blocked is False
    # bad_gln falls in a sub-batch of 10 -- Fass 400s the whole sub-batch,
    # not just the one bad GLN within it, so all 10 count as failed.
    assert failed_glns == 10
    assert len(pharmacies) == 40


def test_check_stock_success(monkeypatch):
    def fake_post(path, body):
        return [{"glnCode": g, "stockInformation": "IN_STOCK", "exchangeableProductInStock": False} for g in body]

    monkeypatch.setattr(fass, "_proxy_post", fake_post)

    gln_codes = ["1", "2", "3"]
    pharmacy_map = {g: {"name": f"Apotek {g}"} for g in gln_codes}
    pharmacies, failed_glns, blocked = fass.check_stock("20001018100021", gln_codes, pharmacy_map)

    assert blocked is False
    assert failed_glns == 0
    assert len(pharmacies) == 3
