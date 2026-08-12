"""Quality gates, clarifying fallback, self-correction critique."""

from helix.services.gold_quality import (
    clarifying_support_fallback,
    critique_for_reasons,
    quality_reject_reasons,
    synthesize_gold_pair,
)


BRIEF = {
    "domain": "food delivery customer support",
    "mission": "Help customers",
    "categories": ["refunds"],
    "agent_instructions": "casual friendly",
}
EV = (
    "Get 50 percent off with code FOOD50. Like and share! "
    "Our refund policy allows returns within 30 days for spoiled items. "
    "Track your order in the app. #ad sponsored content"
)


def test_reject_refuse_and_echo():
    refuse = (
        "I don't have enough verified evidence in the sources provided to answer confidently. "
        "Please share the specific policy page or internal docs section."
    )
    r = quality_reject_reasons(
        brief=BRIEF, topic="refunds", evidence=EV, output=refuse
    )
    assert "support_refuses_or_demands_internal_docs" in r

    dump = "Based on the available documentation: " + EV
    r2 = quality_reject_reasons(brief=BRIEF, topic="refunds", evidence=EV, output=dump)
    assert "documentation_dump_wrapper" in r2 or "verbatim_evidence_echo" in r2


def test_clarifying_fallback_passes_gates():
    out = clarifying_support_fallback(title="Cold pizza", topic="refunds", evidence=EV)
    reasons = quality_reject_reasons(
        brief=BRIEF, topic="refunds", evidence=EV, output=out
    )
    assert reasons == []


def test_critique_mentions_refuse_and_echo():
    c = critique_for_reasons(
        ["support_refuses_or_demands_internal_docs", "verbatim_evidence_echo"],
        last_out="please send internal docs",
    )
    assert "order ID" in c or "internal docs" in c.lower()
    assert "paste" in c.lower() or "echo" in c.lower() or "Paraphrase" in c


def test_template_synthesize_ok():
    pair = synthesize_gold_pair(
        brief=BRIEF,
        title="Refund for cold pizza",
        evidence=EV,
        topic="refunds",
        prefer_llm=False,
    )
    assert pair is not None
    assert pair.get("quality_ok") is True
    reasons = quality_reject_reasons(
        brief=BRIEF, topic="refunds", evidence=EV, output=pair["output"]
    )
    assert reasons == []
