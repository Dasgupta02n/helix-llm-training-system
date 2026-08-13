"""Brief sources must map to real gather channels, not silent social fallback."""

from helix.services.source_adapter import adapt_source, adapt_sources, sources_for_gather
from helix.services.research_targets import build_search_queries


def test_education_sites_are_web_not_instagram():
    spec = adapt_source("consumer credit card education")
    assert spec["reachable"] is True
    assert spec["channel"] not in {"instagram", "tiktok", "youtube", "x"}
    assert spec["channel"] in {"education", "web"}


def test_forums_and_docs_and_scripts():
    assert adapt_source("forums")["channel"] == "forum"
    assert adapt_source("help center docs")["channel"] == "docs"
    assert adapt_source("sales scripts")["channel"] == "sales_script"
    assert adapt_source("sales scripts")["reachable"] is True


def test_tickets_are_unreachable():
    spec = adapt_source("support tickets")
    assert spec["reachable"] is False
    assert "ticket" in (spec["reason"] or "").lower() or "not" in (spec["reason"] or "").lower()


def test_plan_sources_win_over_assignment_instagram():
    specs = sources_for_gather(
        brief_sources=["consumer credit card education", "forums"],
        assignment_source="instagram",
        domain_kind="sales",
    )
    labels = [s["label"] for s in specs]
    assert "consumer credit card education" in labels
    assert "forums" in labels
    assert all(s["channel"] != "instagram" for s in specs if s["reachable"])


def test_queries_include_named_source_type():
    brief = {
        "domain": "Credit Card Sales Pro",
        "mission": "Train reps on consumer credit education",
        "categories": ["APR", "rewards"],
        "sources": ["consumer credit card education"],
    }
    qs = build_search_queries(
        brief,
        category="APR",
        source="web",
        extra_operators=['"consumer education"', "site:.edu"],
        source_label="consumer credit card education",
    )
    blob = " ".join(qs).lower()
    assert "education" in blob
    assert "instagram" not in blob
    assert "tiktok" not in blob


def test_adapt_sources_dedupes():
    out = adapt_sources(["web", "Web", "docs"])
    assert len(out) == 2
