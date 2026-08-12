"""Held-out domain canaries — must pass before domain-agnostic claims ship."""

from helix.services.domain_ontology import domain_gold_pair
from helix.services.gold_quality import quality_reject_reasons, synthesize_gold_pair
from helix.services.research_targets import build_search_queries, research_domain_kind


CANARIES = [
    {
        "name": "support",
        "brief": {
            "domain": "SaaS billing customer support",
            "mission": "Resolve billing and access tickets",
            "categories": ["billing", "access", "refunds"],
        },
        "kind": "support",
        "evidence": (
            "If your card fails, update payment method in Settings. "
            "Refunds for accidental annual upgrades are available within 14 days. "
            "Contact support with your account email."
        ),
        "title": "Charged twice for annual plan",
        "topic": "billing",
    },
    {
        "name": "sales",
        "brief": {
            "domain": "B2B sales coaching",
            "mission": "Handle price objections on discovery calls",
            "categories": ["objections", "discovery"],
        },
        "kind": "sales",
        "evidence": (
            "When a buyer says it is too expensive, reframe value to outcomes, "
            "ask which metric matters most, and propose a pilot."
        ),
        "title": "Too expensive objection",
        "topic": "objections",
    },
    {
        "name": "hr",
        "brief": {
            "domain": "HR onboarding assistant",
            "mission": "Answer employee handbook questions",
            "categories": ["pto", "benefits", "onboarding"],
        },
        "kind": "hr",
        "evidence": (
            "New hires receive PTO accrual starting month two. "
            "Benefits enrollment closes 30 days after start date."
        ),
        "title": "When do benefits start?",
        "topic": "benefits",
    },
    {
        "name": "legal",
        "brief": {
            "domain": "privacy policy Q&A",
            "mission": "Explain GDPR data subject rights accurately",
            "categories": ["gdpr", "privacy"],
        },
        "kind": "legal",
        "evidence": (
            "Users may request access, rectification, or erasure of personal data. "
            "We respond within 30 days as required by applicable law."
        ),
        "title": "How do I delete my data?",
        "topic": "gdpr",
    },
    {
        "name": "ecommerce",
        "brief": {
            "domain": "online retail help desk",
            "mission": "Help shoppers with returns and shipping",
            "categories": ["returns", "shipping"],
        },
        "kind": "ecommerce",
        "evidence": (
            "Unworn items can be returned within 30 days with receipt. "
            "Standard shipping takes 3–5 business days."
        ),
        "title": "Can I return shoes?",
        "topic": "returns",
    },
]


def test_domain_kind_routing():
    for c in CANARIES:
        assert research_domain_kind(c["brief"]) == c["kind"], c["name"]
        qs = build_search_queries(c["brief"], category=c["brief"]["categories"][0], attempt=0)
        assert qs, c["name"]


def test_domain_synthesis_not_refuse_or_echo():
    for c in CANARIES:
        pair = synthesize_gold_pair(
            brief=c["brief"],
            title=c["title"],
            evidence=c["evidence"],
            topic=c["topic"],
            prefer_llm=False,
        )
        assert pair is not None, c["name"]
        assert pair.get("quality_ok"), c["name"]
        out = pair["output"]
        assert "internal doc" not in out.lower(), c["name"]
        assert "based on the available documentation" not in out.lower(), c["name"]
        # Should not be a pure evidence dump
        assert out.strip() != c["evidence"].strip(), c["name"]
        reasons = quality_reject_reasons(
            brief=c["brief"],
            topic=c["topic"],
            evidence=c["evidence"],
            output=out,
        )
        assert reasons == [], (c["name"], reasons)


def test_domain_gold_pair_shapes():
    for c in CANARIES:
        pair = domain_gold_pair(
            brief=c["brief"],
            title=c["title"],
            evidence=c["evidence"],
            topic=c["topic"],
        )
        assert len(pair["input"]) > 20
        assert len(pair["output"]) > 20
