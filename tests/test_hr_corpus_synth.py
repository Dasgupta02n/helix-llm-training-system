"""HR / non-support corpus must synthesize gold (not synth_failed on verbatim gates)."""

from helix.services.corpus import extract_training_units, write_corpus_units_as_gold
from helix.services.domain_ontology import domain_gold_pair
from helix.services.gold_quality import quality_reject_reasons, synthesize_gold_pair


HR_FAQ = """
Q1: When does PTO accrual start?
PTO accrues starting the first day of the second full month of employment.
Full-time employees accrue 1.25 days per month (15 days per year).

Q2: Can unused PTO carry over?
Unused PTO carries over up to 5 days into the next calendar year.

Q3: How do I request remote work?
Remote work is allowed up to 2 days per week with manager approval.
Submit a request in the HR portal at least 2 weeks in advance.
"""

BRIEF = {
    "domain": "HR policy assistant for employee handbook Q&A",
    "mission": "Answer employee handbook questions about PTO, benefits, and onboarding",
    "categories": ["pto", "benefits", "onboarding", "remote work"],
    "agent_instructions": "clear professional",
}


def test_hr_template_does_not_fail_verbatim_gates():
    units = extract_training_units(title="HR FAQ", content=HR_FAQ, category="pto")
    assert len(units) >= 2
    for u in units:
        pair = domain_gold_pair(
            brief=BRIEF, title=u["title"], evidence=u["evidence"], topic="pto"
        )
        reasons = quality_reject_reasons(
            brief=BRIEF,
            topic="pto",
            evidence=u["evidence"],
            output=pair["output"],
        )
        # Template alone may still fail on edge cases; full synth must not
        sp = synthesize_gold_pair(
            brief=BRIEF,
            title=u["title"],
            evidence=u["evidence"],
            topic="pto",
            prefer_llm=False,
        )
        assert sp is not None, u["title"]
        assert sp.get("quality_ok") is True, (u["title"], sp.get("reject_reasons"), reasons)
        assert sp.get("reject_reasons") in (None, [])
        # Must not be a raw dump
        assert "based on the available documentation" not in sp["output"].lower()


def test_hr_synth_surfaces_reject_reasons_when_forced_fail(monkeypatch):
    """When everything fails, reject_reasons must be specific — not bare synth_failed."""
    from helix.services import gold_quality as gq

    def _always_bad(**kwargs):
        return {
            "input": "x",
            "output": "short",
            "quality_ok": False,
            "reject_reasons": ["output_too_short", "long_verbatim_chunk"],
        }

    monkeypatch.setattr(gq, "domain_gold_pair", lambda **k: {"input": "in", "output": "no"})
    monkeypatch.setattr(
        gq,
        "clarifying_domain_fallback",
        lambda **k: "x",  # too short → fail
    )
    monkeypatch.setattr(
        gq,
        "clarifying_support_fallback",
        lambda **k: "x",
    )
    # quality_reject_reasons will mark short output
    sp = gq.synthesize_gold_pair(
        brief=BRIEF, title="t", evidence=HR_FAQ * 2, topic="pto", prefer_llm=False
    )
    assert sp is not None
    assert sp.get("quality_ok") is False
    assert sp.get("reject_reasons")
    assert "synth_failed" not in (sp.get("reject_reasons") or []) or len(
        sp.get("reject_reasons") or []
    ) > 1
