"""Unit tests for adaptive ontology-routed research."""

from helix.services.research_targets import (
    build_search_queries,
    min_evidence_threshold,
    research_domain_kind,
    score_item_for_kind,
)


def test_support_kind_detection():
    brief = {
        "domain": "food delivery customer support",
        "mission": "Help with refunds",
        "categories": ["refunds"],
    }
    assert research_domain_kind(brief) == "support"


def test_query_variation_across_attempts():
    brief = {
        "domain": "food delivery customer support",
        "mission": "Help customers with late deliveries and refunds",
        "categories": ["refunds", "late delivery"],
    }
    q0 = build_search_queries(brief, category="refunds", attempt=0)
    q1 = build_search_queries(brief, category="refunds", attempt=1)
    q2 = build_search_queries(brief, category="refunds", attempt=2)
    assert q0
    assert q1
    assert set(q0) != set(q1)
    assert any("help" in q.lower() or "faq" in q.lower() or "support" in q.lower() for q in q0 + q1)
    assert min_evidence_threshold(5) >= 3


def test_support_demotes_ads_boosts_help():
    ad = score_item_for_kind(
        kind="support",
        title="Like and share for 50% off",
        snippet="sponsored #ad click here",
        url="https://facebook.com/x/posts/1",
    )
    help_ = score_item_for_kind(
        kind="support",
        title="Refund policy help center",
        snippet="How do I request a refund for cold food",
        url="https://support.example.com/faq/refund",
    )
    assert help_["relevance_score"] > ad["relevance_score"]
    assert ad["ad_like"] is True
    assert help_["help_like"] is True
