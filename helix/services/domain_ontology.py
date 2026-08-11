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


def ontology_for_brief(brief: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Return (type_name, kind, description) rows for this research brief."""
    domain = (brief.get("domain") or "").lower()
    mission = (brief.get("mission") or "").lower()
    cats = [str(c).lower() for c in (brief.get("categories") or [])]
    blob = " ".join([domain, mission, " ".join(cats)])

    # Support / CX / helpdesk
    if any(
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
        )
    ):
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
            ("severity_window", "fact", "Allowed refund/return window if stated"),
            ("eligibility", "fact", "Whether customer is eligible for the action"),
            ("channel", "fact", "chat, email, ticket, phone"),
            ("tone", "fact", "Expected reply tone"),
        ]
    # Sales / coaching
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
    # Influencer / marketing (legacy default only when clearly marketing)
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
        # Generic knowledge / Q&A domain
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

    # Always add category-specific entity types from plan categories
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

    # Remove old ontology rows so agents cannot keep using Brand/Creator for support domains
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


def domain_gold_pair(
    *,
    brief: dict[str, Any],
    title: str,
    evidence: str,
    topic: str,
    url: str = "",
) -> dict[str, Any]:
    """Build a training Q/A pair grounded only in evidence (domain-agnostic)."""
    domain = (brief.get("domain") or "this product").strip()
    mission = (brief.get("mission") or "Answer helpfully and accurately").strip()
    cats = brief.get("categories") or []
    cat_hint = ", ".join(str(c) for c in cats[:6]) if cats else topic
    text = (evidence or title or "").strip()
    thin = len(text) < 60

    if thin:
        return {
            "input": (
                f"Domain: {domain}\nTopic: {topic}\n"
                f"User question: Based on available public material about “{title}”, "
                f"what should a good assistant say?\n"
                f"Evidence (may be incomplete):\n{text[:400] or '(none)'}"
            ),
            "output": (
                "I don’t have enough verified evidence in the sources provided to answer "
                "confidently. Please share the specific policy page, ticket field, or docs "
                "section so I can answer without guessing."
            ),
            "rationale": (
                "Faithfulness: evidence too thin; refusal avoids inventing domain facts."
            ),
            "difficulty": "edge-case",
            "is_negative": True,
        }

    # Support-shaped default question when domain looks like support
    blob = f"{domain} {mission} {cat_hint}".lower()
    if any(k in blob for k in ("support", "refund", "billing", "ticket", "shipping", "delivery")):
        user_q = (
            f"A customer asks about: {title}. "
            f"Using only the evidence below, write the ideal support reply "
            f"(acknowledge the issue, state what you can do, ask for missing details if needed)."
        )
        output = (
            f"Thanks for reaching out about this. Based on the available documentation:\n\n"
            f"{text[:700]}\n\n"
            f"If anything in your account differs (order ID, plan, or promo fields), share those "
            f"details and I’ll confirm the exact next step—without inventing policies."
        )
    else:
        user_q = (
            f"Using only the evidence below about “{title}” in domain “{domain}”, "
            f"write a high-quality answer that helps achieve: {mission[:180]}"
        )
        output = (
            f"Based on the available evidence:\n\n{text[:700]}\n\n"
            f"I limited this answer to what the sources state; I did not invent details."
        )

    if url:
        user_q += f"\nSource URL: {url}"

    return {
        "input": (
            f"Domain: {domain}\nTopics: {cat_hint}\nMission: {mission[:200]}\n\n"
            f"{user_q}\n\nEvidence:\n{text[:1200]}"
        ),
        "output": output[:2000],
        "rationale": (
            f"Grounded only in gathered evidence for “{title}”. "
            f"Does not invent facts beyond the source text. Topic “{topic}”."
        ),
        "difficulty": "moderate",
        "is_negative": False,
    }
