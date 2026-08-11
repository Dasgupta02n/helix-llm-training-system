"""Domain-derived ontology for gold mining (not locked to influencer marketing)."""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy.orm import Session

from helix.db import models as m
from helix.services.brief import get_active_project, project_to_dict


def _uid(prefix: str = "ont_") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _slug_type(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "", (name or "").title().replace(" ", ""))
    return s or "Entity"


def _is_support_domain(brief: dict[str, Any], topic: str = "") -> bool:
    domain = (brief.get("domain") or "").lower()
    mission = (brief.get("mission") or "").lower()
    cats = " ".join(str(c).lower() for c in (brief.get("categories") or []))
    blob = f"{domain} {mission} {cats} {topic}".lower()
    return any(
        k in blob
        for k in (
            "support",
            "ticket",
            "customer service",
            "helpdesk",
            "refund",
            "billing",
            "shipping",
            "faq",
            "cx ",
            "saas",
            "delivery",
            "order",
            "help center",
        )
    )


def ontology_for_brief(brief: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Return (type_name, kind, description) rows for this research brief."""
    domain = (brief.get("domain") or "").lower()
    mission = (brief.get("mission") or "").lower()
    cats = [str(c).lower() for c in (brief.get("categories") or [])]
    blob = " ".join([domain, mission, " ".join(cats)])

    if _is_support_domain(brief):
        base = [
            ("Customer", "entity", "End user or account contacting support"),
            ("SupportTicket", "entity", "A support case or request"),
            ("IssueType", "entity", "Category of problem (billing, shipping, access, etc.)"),
            ("Product", "entity", "Product or plan the issue is about"),
            ("Policy", "entity", "Policy or rule that governs resolution"),
            ("ResolutionStep", "entity", "Concrete step in the ideal support reply"),
            ("BillingAction", "entity", "Refund, charge, credit, or invoice action"),
            ("mentions", "relationship", "Ticket mentions Product or Policy"),
            ("resolved_by", "relationship", "Issue resolved by ResolutionStep"),
            ("requires", "relationship", "Resolution requires BillingAction or info"),
            ("opportunity_window", "fact", "Allowed refund/return window if stated"),
            ("eligibility", "fact", "Whether customer is eligible for the action"),
            ("channel", "fact", "chat, email, ticket, phone"),
            ("tone", "fact", "Expected reply tone"),
        ]
    elif any(k in blob for k in ("sales", "coach", "crm", "pipeline", "lead")):
        base = [
            ("Buyer", "entity", "Prospect or customer persona"),
            ("Seller", "entity", "Rep or coach role"),
            ("Objection", "entity", "Buyer objection"),
            ("Offer", "entity", "Product or offer being sold"),
            ("Play", "entity", "Recommended sales/coaching move"),
            ("addresses", "relationship", "Play addresses Objection"),
            ("targets", "relationship", "Play targets Buyer persona"),
            ("stage", "fact", "Funnel stage"),
            ("next_step", "fact", "Clear next action"),
        ]
    elif any(
        k in blob
        for k in ("influencer", "creator", "campaign", "instagram", "tiktok", "sponsor")
    ):
        base = [
            ("Brand", "entity", "Brand running a campaign"),
            ("Creator", "entity", "Content creator"),
            ("Campaign", "entity", "Sponsored campaign"),
            ("Platform", "entity", "Social platform"),
            ("Product", "entity", "Product being promoted"),
            ("sponsors", "relationship", "Creator sponsors Brand"),
            ("features", "relationship", "Campaign features Product"),
            ("runs_on", "relationship", "Campaign runs on Platform"),
            ("engagement_rate", "fact", "Engagement metric if stated"),
            ("content_format", "fact", "Reel, short, long-form, etc."),
            ("call_to_action", "fact", "Primary CTA"),
        ]
    else:
        base = [
            ("Entity", "entity", "Primary subject in the domain"),
            ("Concept", "entity", "Domain concept or term"),
            ("Document", "entity", "Source document or page"),
            ("Procedure", "entity", "How-to or process step"),
            ("Constraint", "entity", "Rule, limit, or requirement"),
            ("relates_to", "relationship", "General relationship between entities"),
            ("defined_in", "relationship", "Concept defined in Document"),
            ("requires", "relationship", "Procedure requires Constraint"),
            ("definition", "fact", "Canonical definition"),
            ("example", "fact", "Illustrative example from evidence"),
            ("source_url", "fact", "URL of grounding evidence"),
        ]

    for cat in brief.get("categories") or []:
        tname = _slug_type(str(cat))
        if tname and tname not in {b[0] for b in base}:
            base.append(
                (
                    tname,
                    "entity",
                    f"Category/topic bucket from research plan: {cat}",
                )
            )
    return base


def sync_ontology_from_brief(db: Session, tenant_id: str) -> dict[str, Any]:
    """Replace tenant ontology with types derived from the active research brief."""
    project = get_active_project(db, tenant_id)
    if not project:
        return {"ok": False, "error": "no_brief", "types": 0}
    brief = project_to_dict(project)
    types = ontology_for_brief(brief)

    old = db.query(m.OntologyType).filter_by(tenant_id=tenant_id).all()
    for row in old:
        db.delete(row)
    db.flush()

    for name, kind, desc in types:
        db.add(
            m.OntologyType(
                id=_uid(),
                tenant_id=tenant_id,
                type_name=name,
                kind=kind,
                description=desc,
            )
        )
    db.flush()
    return {
        "ok": True,
        "types": len(types),
        "type_names": [t[0] for t in types],
        "domain": brief.get("domain"),
    }


def _clean_evidence(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    # Drop obvious UI chrome fragments
    t = re.sub(r"(Like|Comment|Share|Follow)\s*$", "", t, flags=re.I)
    return t.strip()


def _customer_question_from_title(title: str, topic: str) -> str:
    t = (title or topic or "my issue").strip()
    # Prefer natural customer phrasing over "about: raw headline"
    if t.lower().startswith("how ") or t.endswith("?"):
        return t if t.endswith("?") else f"{t}?"
    return f"Hi — I need help with this: {t}. What should I do next?"


def _support_reply_thin(*, title: str, topic: str, fragment: str) -> str:
    """Customer-facing reply when public evidence is thin — still helpful."""
    topic_l = (topic or "this").replace("_", " ")
    frag = (fragment or title or topic_l).strip()
    short = frag[:120] + ("…" if len(frag) > 120 else "")
    return (
        f"Hey! Happy to help with {topic_l}.\n\n"
        f"I can see this relates to “{short}”, but I want to make sure I give you the "
        f"right next step for *your* account.\n\n"
        f"Could you share:\n"
        f"1) Your order ID or account email (whichever applies)\n"
        f"2) What you expected to happen vs what you saw\n"
        f"3) A screenshot if there’s an error message\n\n"
        f"Once I have those, I’ll tell you exactly what we can do and the fastest path "
        f"to fix it."
    )


def _paraphrase_support_facts(evidence: str, topic: str) -> list[str]:
    """Turn evidence cues into short paraphrased bullets — never paste scrape text."""
    low = (evidence or "").lower()
    topic_l = (topic or "this").replace("_", " ")
    facts: list[str] = []

    if re.search(r"\brefund\b|\bmoney back\b|\breturn\b", low):
        if re.search(r"30[-\s]?day", low):
            facts.append(
                "When food quality is the issue, refunds are often available within a "
                "limited window (around 30 days)—I’ll confirm exactly what applies to your order."
            )
        else:
            facts.append(
                "Refunds may be available depending on what went wrong and how recently "
                "it happened—I’ll check against your order details."
            )
    if re.search(r"\bdeliver|\bshipping|\btrack(ing)?\b|\blate\b|\bmissing\b", low):
        facts.append(
            "I can look up live tracking and delivery options as soon as I have your order ID."
        )
    if re.search(r"\bcancel", low):
        facts.append(
            "Cancellation is usually possible if the kitchen hasn’t started preparing yet—"
            "I’ll check the status and tell you the fastest path."
        )
    if re.search(r"\bcold\b|\bspoil|\bwarm|\bwrong item|\bmissing item", low):
        facts.append(
            "If something arrived cold, spoiled, wrong, or incomplete, that’s something "
            "we can usually fix—credit, re-delivery, or refund depending on your case."
        )
    if re.search(r"\bpayment|\bcharg|\bbill|\bcoupon|\bpromo", low) and not facts:
        facts.append(
            "I can review the charge or offer on your account once I can pull up the order."
        )
    if not facts:
        facts.append(
            f"I can help with {topic_l}, and I’ll stick to what’s actually available "
            f"for your account rather than generic promo copy."
        )
    return facts[:3]


def _support_reply_from_evidence(*, title: str, topic: str, evidence: str) -> str:
    """Synthesize a support reply — never dump raw scrape/marketing text."""
    text = _clean_evidence(evidence)
    topic_l = (topic or "this").replace("_", " ")
    low_all = text.lower()
    facts = _paraphrase_support_facts(text, topic)

    if "refund" in low_all or "30 day" in low_all or "30-day" in low_all or "return" in low_all:
        next_step = (
            "If this matches your case, reply with your order ID and I’ll start the "
            "refund/check for you right away."
        )
    elif "deliver" in low_all or "shipping" in low_all or "track" in low_all:
        next_step = (
            "Send me your order ID and I’ll pull the latest tracking status and options."
        )
    elif "cancel" in low_all:
        next_step = (
            "Share your order ID and whether the order has already started preparing — "
            "I’ll tell you if we can still cancel and how."
        )
    else:
        next_step = (
            "Reply with your order ID (or account email) and I’ll take the next step for you."
        )

    fact_block = " ".join(facts)
    # Genuine customer-facing reply: paraphrase only, concrete next step, no doc dump
    return (
        f"Hey! Thanks for reaching out about {topic_l}.\n\n"
        f"I’m sorry this happened — here’s how I can help: {fact_block}\n\n"
        f"{next_step}\n\n"
        f"I won’t invent promos or policies that don’t apply to you; once I have your "
        f"details I’ll confirm the exact fix."
    )


def _general_reply_from_evidence(*, title: str, domain: str, mission: str, evidence: str) -> str:
    text = _clean_evidence(evidence)
    # Paraphrase: keep short cue phrases, not multi-sentence dumps
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 25]
    cues: list[str] = []
    skip = ("click here", "follow us", "like and share", "subscribe", "#ad", "sponsored")
    for s in sentences:
        low = s.lower()
        if any(x in low for x in skip):
            continue
        # compress to first clause
        bit = re.split(r"[,;:]", s)[0].strip()
        if 20 <= len(bit) <= 120:
            cues.append(bit)
        if len(cues) >= 2:
            break
    if cues:
        summary = "; ".join(cues)
    else:
        summary = (text[:140] + "…") if len(text) > 140 else (text or title)
    return (
        f"Here’s a clear answer on “{title}” for {domain}:\n\n"
        f"Key points: {summary}.\n\n"
        f"Next step: tell me which part you need to act on "
        f"(related to: {mission[:100]}), and I’ll give a concrete action."
    )


def domain_gold_pair(
    *,
    brief: dict[str, Any],
    title: str,
    evidence: str,
    topic: str,
    url: str = "",
) -> dict[str, Any]:
    """Build a training Q/A pair: helpful domain output, grounded, not refuse/echo scrape."""
    domain = (brief.get("domain") or "this product").strip()
    mission = (brief.get("mission") or "Answer helpfully and accurately").strip()
    instructions = (brief.get("agent_instructions") or "").strip()
    cats = brief.get("categories") or []
    cat_hint = ", ".join(str(c) for c in cats[:6]) if cats else topic
    text = _clean_evidence(evidence or title or "")
    support = _is_support_domain(brief, topic)
    thin = len(text) < 80

    # Sample format from plan if present
    sample_in = ""
    sample_out = ""
    # brief may carry sample via topic schemas externally; keep hooks clean

    customer_q = _customer_question_from_title(title, topic)

    if support:
        if thin:
            output = _support_reply_thin(title=title, topic=topic, fragment=text or title)
            rationale = (
                "Thin public evidence: still help the customer with a friendly reply "
                "and ask for the account details needed for a concrete next step "
                "(not an internal-docs demand)."
            )
            difficulty = "edge-case"
            is_neg = False  # still a valid positive training example for support
        else:
            output = _support_reply_from_evidence(
                title=title, topic=topic, evidence=text
            )
            rationale = (
                "Synthesized a customer-facing support reply from public evidence: "
                "acknowledge, paraphrase useful facts, give a concrete next step. "
                "Does not paste raw scrape/marketing text or invent policies."
            )
            difficulty = "moderate"
            is_neg = False

        tone_note = ""
        if instructions:
            tone_note = f"\nStyle notes from plan: {instructions[:240]}"

        input_text = (
            f"You are a customer support agent for: {domain}.\n"
            f"Topics: {cat_hint}\n"
            f"Goal: {mission[:220]}{tone_note}\n\n"
            f"Customer message:\n{customer_q}\n\n"
            f"Internal notes (may include public web snippets — do not dump them raw; "
            f"write a natural helpful reply; if info is incomplete ask the customer for "
            f"order/account details, never for internal doc links):\n"
            f"{text[:900] or title}\n"
            + (f"Source: {url}\n" if url else "")
        )
    else:
        if thin:
            output = (
                f"I can help with “{title}”. I only have a short note so far "
                f"(“{(text or title)[:100]}”). Tell me what you’re trying to do and any "
                f"constraints, and I’ll give a concrete next step."
            )
            rationale = "Thin evidence: ask clarifying questions and stay helpful."
            difficulty = "edge-case"
            is_neg = False
        else:
            output = _general_reply_from_evidence(
                title=title, domain=domain, mission=mission, evidence=text
            )
            rationale = (
                "Synthesized answer from evidence with a concrete next step; "
                "not a raw echo of the source."
            )
            difficulty = "moderate"
            is_neg = False

        input_text = (
            f"Domain: {domain}\nTopics: {cat_hint}\nMission: {mission[:200]}\n\n"
            f"User: {customer_q}\n\n"
            f"Evidence (paraphrase; do not invent):\n{text[:1200]}\n"
            + (f"URL: {url}\n" if url else "")
        )

    return {
        "input": input_text[:4000],
        "output": output[:2000],
        "rationale": rationale[:1000],
        "difficulty": difficulty,
        "is_negative": is_neg,
    }
