"""BYO corpus splits into units and promotes into candidate/evidence shape."""

from helix.services.corpus import extract_training_units, infer_category_from_text
from helix.services.gold_quality import (
    format_rejection_reason,
    quality_reject_reasons,
    synthesize_gold_pair,
)


FAQ = """
Q1: My delivery is late. What should I do?
If your order is more than 30 minutes late, open the app and tap Help on the order.
We can check status and often issue a credit for the wait.
Q2: An item is missing from my bag.
Reply with your order ID and a photo of the receipt if you have one.
Missing items are refunded or re-delivered depending on stock.
Q3: Food arrived damaged or cold.
We refund damaged or cold food within 24 hours when you share order ID and a photo.
"""


def test_faq_splits_into_units():
    units = extract_training_units(title="Support FAQ", content=FAQ, category="general")
    assert len(units) >= 3
    assert any("late" in u["title"].lower() or "late" in u["evidence"].lower() for u in units)


def test_infer_category_from_plan():
    cat = infer_category_from_text(
        FAQ, ["late delivery", "missing items", "refunds", "wrong items"]
    )
    assert cat in {"late delivery", "missing items", "refunds"}


def test_corpus_unit_synthesizes_gold():
    brief = {
        "domain": "food delivery customer support",
        "mission": "Help with late delivery and refunds",
        "categories": ["late delivery", "missing items", "refunds"],
        "agent_instructions": "friendly",
    }
    units = extract_training_units(title="Support FAQ", content=FAQ)
    pair = synthesize_gold_pair(
        brief=brief,
        title=units[0]["title"],
        evidence=units[0]["evidence"],
        topic="late_delivery",
        prefer_llm=False,
    )
    assert pair is not None
    assert pair.get("quality_ok")
    reasons = quality_reject_reasons(
        brief=brief,
        topic="late_delivery",
        evidence=units[0]["evidence"],
        output=pair["output"],
    )
    assert reasons == []


def test_format_rejection_reason_readable():
    s = format_rejection_reason(
        ["support_refuses_or_demands_internal_docs", "verbatim_evidence_echo"]
    )
    assert "internal docs" in s.lower() or "refuses" in s.lower()
    assert "verbatim" in s.lower() or "paste" in s.lower()
