import pytest


@pytest.mark.parametrize("path", ["/", "/om", "/privacy", "/kategorier", "/healthz", "/sitemap.xml", "/robots.txt"])
def test_main_pages_return_200(client, path):
    r = client.get(path)
    assert r.status_code == 200


def test_robots_txt_disallows_internal_pages(client):
    body = client.get("/robots.txt").get_data(as_text=True)
    for path in ["/subscribe", "/manage/", "/confirm/", "/unsubscribe/", "/extend/", "/api/", "/admin", "/log"]:
        assert f"Disallow: {path}" in body


def test_sitemap_is_valid_xml(client):
    import xml.etree.ElementTree as ET

    body = client.get("/sitemap.xml").get_data(as_text=True)
    root = ET.fromstring(body)  # raises if malformed
    assert len(root) >= 1


def test_healthz_reports_status(client):
    data = client.get("/healthz").get_json()
    assert "status" in data
    assert "polls_done" in data
