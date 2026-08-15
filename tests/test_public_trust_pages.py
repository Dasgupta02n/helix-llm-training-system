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
    "/account",
    "/trust",
    "/status",
    "/gold-training-data",
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
    assert "https://c7xai.in/account" in sm.text
    assert "https://c7xai.in/trust" in sm.text
    assert "https://c7xai.in/gold-training-data" in sm.text


def test_crawler_files_exist():
    robots = client.get("/robots.txt")
    assert "llms.txt" in robots.text.lower() or "LLMs-Txt" in robots.text
    llms = client.get("/llms.txt")
    assert llms.status_code == 200
    assert "c7x" in llms.text.lower()
    assert "training-data" in llms.text.lower() or "training data" in llms.text.lower()
    sec = client.get("/.well-known/security.txt")
    assert sec.status_code == 200
    assert "Contact:" in sec.text
    assert "dasgupta.02n@gmail.com" in sec.text
    humans = client.get("/humans.txt")
    assert humans.status_code == 200
    assert "Sabyasachi" in humans.text


def test_public_pages_have_social_and_canonical():
    r = client.get("/trust")
    assert r.status_code == 200
    assert 'property="og:title"' in r.text
    assert 'rel="canonical"' in r.text
    assert 'application/ld+json' in r.text
    acc = client.get("/account")
    assert acc.status_code == 200
    low = acc.text.lower()
    assert "profile" in low
    assert "create account" in low or "sign in" in low
    assert "store" in low or "library" in low


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
    assert "meet riu" in low or "riu is" in low or "riu, the setup" in low
    assert 'id="seo-geo"' in r.text
    assert "generative" in low or "geo" in low
    assert "langsmith" in low or "labeling" in low or "rag" in low
    assert "application/ld+json" in r.text


def test_gold_training_data_page_is_citable():
    r = client.get("/gold-training-data")
    assert r.status_code == 200
    low = r.text.lower()
    assert "gold training data" in low
    assert "riu" in low
    assert 'rel="canonical"' in r.text
    assert "FAQPage" in r.text
    assert "HowTo" in r.text
    assert "labeling factory" in low
    assert "hosted chat" in low
    md = client.get("/gold-training-data.md")
    assert md.status_code == 200
    assert "canonical: https://c7xai.in/gold-training-data" in md.text.lower()
    redir = client.get("/why-c7x", follow_redirects=False)
    assert redir.status_code in {301, 307, 308}
    banned = (
        "langsmith",
        "scale ai",
        "surge ai",
        "labelbox",
        "unsloth",
        "predibase",
        "openpipe",
        "argilla",
        "distilabel",
    )
    for name in banned:
        assert name not in low, name
        assert name not in md.text.lower(), name


def test_site_nav_on_inner_pages():
    r = client.get("/pricing")
    assert r.status_code == 200
    assert "Open studio" in r.text
    assert "0.75" in r.text or "per gold" in r.text.lower() or "per row" in r.text.lower()
    assert "C7X-IO" in r.text or "C7X-IO" in r.text.lower()


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


_VENDOR_NAMES = (
    "apify",
    "runpod",
    "openrouter",
    "hugging face",
    "huggingface",
    "hostinger",
    "resend",
)


def test_public_and_app_html_hide_vendor_names():
    from helix.services.pipeline_modes import MODE_META
    from helix.services.runpod_train import compute_policy

    for path in PUBLIC_HTML + ["/app"]:
        r = client.get(path)
        assert r.status_code == 200, path
        low = r.text.lower()
        for name in _VENDOR_NAMES:
            assert name not in low, f"{path} still mentions {name}"
    for mode in MODE_META.values():
        blob = " ".join(str(mode.get(k) or "") for k in ("description", "label", "short", "cost")).lower()
        for name in _VENDOR_NAMES:
            assert name not in blob, f"MODE_META still mentions {name}"
    policy = compute_policy()
    blob = f"{policy.get('label')} {policy.get('note')}".lower()
    for name in _VENDOR_NAMES:
        assert name not in blob, f"compute_policy still mentions {name}"
