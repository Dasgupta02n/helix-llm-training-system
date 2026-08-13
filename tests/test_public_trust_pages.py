"""Public trust / marketing routes must return 200 (Genspark diligence checklist)."""

from fastapi.testclient import TestClient

from helix.api.main import app


client = TestClient(app)


PUBLIC_HTML = [
    "/",
    "/privacy",
    "/terms",
    "/pricing",
    "/docs",
    "/about",
    "/contact",
    "/security",
]


def test_public_html_pages_ok():
    for path in PUBLIC_HTML:
        r = client.get(path)
        assert r.status_code == 200, path
        assert "text/html" in r.headers.get("content-type", "")
        assert len(r.text) > 200, path


def test_privacy_is_honest_beta():
    r = client.get("/privacy")
    assert r.status_code == 200
    body = r.text.lower()
    assert "privacy" in body
    assert "beta" in body or "early" in body
    assert "dasgupta.02n@gmail.com" in r.text


def test_terms_and_pricing_exist():
    t = client.get("/terms")
    assert t.status_code == 200
    assert "terms" in t.text.lower()
    p = client.get("/pricing")
    assert p.status_code == 200
    assert "beta" in p.text.lower()
    assert "free" in p.text.lower() or "contact" in p.text.lower()


def test_docs_not_404_and_not_only_openapi():
    r = client.get("/docs")
    assert r.status_code == 200
    assert "Plan" in r.text or "plan" in r.text
    assert "corpus" in r.text.lower() or "gold" in r.text.lower()


def test_about_identity():
    r = client.get("/about")
    assert r.status_code == 200
    assert "Sabyasachi" in r.text or "dasgupta" in r.text.lower()


def test_robots_and_sitemap():
    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert "Sitemap:" in robots.text
    assert "Allow:" in robots.text
    sm = client.get("/sitemap.xml")
    assert sm.status_code == 200
    assert "application/xml" in sm.headers.get("content-type", "")
    assert "https://c7xai.in/privacy" in sm.text
    assert "https://c7xai.in/docs" in sm.text


def test_homepage_has_trust_links_not_dead_docs():
    r = client.get("/")
    assert r.status_code == 200
    assert 'href="/docs"' in r.text
    assert 'href="/privacy"' in r.text or "Privacy" in r.text
    # Positioning wedge language
    low = r.text.lower()
    assert "dataset" in low or "training data" in low or "gold" in low
    assert "chat box" in low or "not a chatbot" in low or "not another chat" in low
    for path in ("/security", "/about", "/contact", "/pricing", "/terms"):
        assert f'href="{path}"' in r.text, path


def test_homepage_introduces_riu_and_seo_geo():
    r = client.get("/")
    assert r.status_code == 200
    low = r.text.lower()
    assert 'id="riu"' in r.text
    assert "meet riu" in low or "riu is" in low
    assert 'id="seo-geo"' in r.text
    assert "generative" in low or "geo" in low
    assert "langsmith" in low or "labeling" in low or "rag" in low
    assert "application/ld+json" in r.text


def test_site_nav_on_inner_pages():
    r = client.get("/pricing")
    assert r.status_code == 200
    assert "Open studio" in r.text
    assert "$35" in r.text or "35" in r.text
    assert "Double Helix" in r.text or "double helix" in r.text.lower()


def test_docs_cover_riu_and_materials():
    r = client.get("/docs")
    body = r.text.lower()
    assert "riu" in body
    assert "material" in body or "rulebook" in body or "script" in body
    assert "export" in body


def test_app_login_has_legal_links():
    r = client.get("/app")
    assert r.status_code == 200
    assert 'href="/privacy"' in r.text
    assert 'href="/terms"' in r.text
