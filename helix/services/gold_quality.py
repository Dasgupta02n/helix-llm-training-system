"""Gold answer synthesis + hard quality gates + self-correction loop."""

from __future__ import annotations

import re
from typing import Any

from helix.services.domain_ontology import (
    _is_support_domain,
    clarifying_domain_fallback,
    domain_gold_pair,
)


# Patterns that must never ship as verified support gold
_REFUSE_INTERNAL_DOCS = re.compile(
    r"(policy page|internal doc|docs section|ticket field|"
    r"sources provided to answer confidently|"
    r"don.?t have enough verified evidence|"
    r"share the specific policy|"
    r"provide (a |the )?(link|url) to (our |the )?(internal|policy))",
    re.I,
)
_DOC_DUMP_WRAPPER = re.compile(
    r"based on the available documentation\s*:",
    re.I,
)
_RAW_ECHO_MARKERS = re.compile(
    r"(like and share|follow us|subscribe now|click here to|"
    r"#ad\b|sponsored content)",
    re.I,
)

# Max self-correction attempts (generate → critique → regenerate)
MAX_SYNTH_ATTEMPTS = 3


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _overlap_ratio(a: str, b: str) -> float:
    """Fraction of evidence content that appears as a long contiguous dump in output."""
    ea, eb = _normalize(a), _normalize(b)
    if len(ea) < 40 or len(eb) < 40:
        return 0.0
    window = 60
    hits = 0
    checks = 0
    for i in range(0, max(1, len(ea) - window), window // 2):
        chunk = ea[i : i + window]
        checks += 1
        if chunk in eb:
            hits += 1
    if checks == 0:
        return 0.0
    return hits / checks


def _has_long_verbatim_chunk(evidence: str, output: str, min_len: int = 55) -> bool:
    ea, eb = _normalize(evidence), _normalize(output)
    if len(ea) < min_len or len(eb) < min_len:
        return False
    step = max(15, min_len // 3)
    for i in range(0, len(ea) - min_len + 1, step):
        if ea[i : i + min_len] in eb:
            return True
    return False


def quality_reject_reasons(
    *,
    brief: dict[str, Any],
    topic: str,
    evidence: str,
    output: str,
    input_text: str = "",
) -> list[str]:
    """Return list of rejection reasons; empty means OK."""
    reasons: list[str] = []
    out = output or ""
    ev = evidence or ""
    if len(ev.strip()) < 40 and input_text:
        ev = input_text
    support = _is_support_domain(brief, topic)

    if len(out.strip()) < 40:
        reasons.append("output_too_short")

    if support and _REFUSE_INTERNAL_DOCS.search(out):
        reasons.append("support_refuses_or_demands_internal_docs")

    if _DOC_DUMP_WRAPPER.search(out):
        reasons.append("documentation_dump_wrapper")

    if support and re.search(
        r"(cannot answer|can'?t answer|unable to (help|answer)|"
        r"insufficient (evidence|information) to answer|"
        r"need (more|additional) (internal )?sources)",
        out,
        re.I,
    ):
        reasons.append("support_refuses_outright")

    if len(ev) >= 80 and _overlap_ratio(ev, out) >= 0.35:
        reasons.append("verbatim_evidence_echo")

    if _has_long_verbatim_chunk(ev, out, min_len=55):
        reasons.append("long_verbatim_chunk")

    if len(ev) >= 60 and _normalize(ev)[:200] in _normalize(out) and len(ev) > 120:
        reasons.append("evidence_substring_dump")

    # Concrete next step for any interactive agent (support, HR, sales, …)
    next_step_cues = (
        "order id",
        "order number",
        "account email",
        "reply with",
        "send me",
        "share your",
        "i'll",
        "i will",
        "next step",
        "right away",
        "look up",
        "check for you",
        "help you",
        "i can help",
        "once i have",
        "could you",
        "can you tell me",
        "what you expected",
        "tell me which",
        "tell me what",
        "concrete action",
        "concrete next step",
        "hr portal",
        "manager approval",
        "start date",
    )
    ol = out.lower()
    if not any(c in ol for c in next_step_cues):
        reasons.append("missing_concrete_customer_next_step")

    instr = str(brief.get("agent_instructions") or "")
    if "RISK:high" in instr.replace(" ", "") or "RISK: high" in instr:
        role_text = brief.get("domain") or brief.get("mission") or ""
        if "ROLE:" in instr:
            role_text = instr.split("ROLE:", 1)[1].split("\n", 1)[0].strip() or role_text
        from helix.services.role_risk import role_relevance_reject_reasons

        reasons.extend(
            role_relevance_reject_reasons(
                role_text=role_text,
                input_text=input_text or "",
                output=out,
                risk_level="high",
            )
        )

    if support:
        if re.search(r"provide (a |the )?(url|link) to (our |the )?internal", ol):
            reasons.append("asks_customer_for_internal_resources")
        if re.search(r"\b(code|promo)\s*[:=]?\s*[A-Z0-9]{4,}\b", out) and re.search(
            r"(like and share|click here|#ad|sponsored)", ev, re.I
        ):
            reasons.append("promo_code_echo_from_marketing")

    if _RAW_ECHO_MARKERS.search(out) and support:
        reasons.append("marketing_chrome_in_support_reply")

    return reasons


def critique_for_reasons(reasons: list[str], last_out: str = "") -> str:
    """Human-readable critique the regenerator must incorporate (not discarded)."""
    tips: list[str] = []
    for r in reasons:
        if r in {
            "support_refuses_or_demands_internal_docs",
            "asks_customer_for_internal_resources",
            "support_refuses_outright",
        }:
            tips.append(
                "Do NOT refuse or ask the customer for internal docs/policy pages. "
                "Stay in support voice and ask for order ID or account email instead."
            )
        elif r in {
            "verbatim_evidence_echo",
            "long_verbatim_chunk",
            "evidence_substring_dump",
            "documentation_dump_wrapper",
        }:
            tips.append(
                "Do NOT paste or echo raw scraped text. Paraphrase only the useful facts "
                "in natural conversational language."
            )
        elif r == "missing_concrete_customer_next_step":
            tips.append(
                "End with a concrete next step: ask for order ID / account email / "
                "what they expected vs what they saw."
            )
        elif r == "marketing_chrome_in_support_reply":
            tips.append("Strip marketing chrome (like-and-share, #ad, click-here).")
        elif r == "promo_code_echo_from_marketing":
            tips.append("Do not regurgitate promo codes from ad copy.")
        elif r == "output_too_short":
            tips.append("Write a fuller reply (at least a few sentences).")
        else:
            tips.append(f"Fix: {r}.")
    # de-dupe preserve order
    seen: set[str] = set()
    uniq: list[str] = []
    for t in tips:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    blob = " ".join(uniq)
    if last_out:
        blob += (
            "\nPrevious rejected draft (do not repeat its mistakes):\n"
            f"{last_out[:500]}"
        )
    return blob


def clarifying_support_fallback(*, title: str, topic: str, evidence: str = "") -> str:
    """Always-valid customer-facing clarifying reply — never refuses, never dumps scrape."""
    topic_l = (topic or "this").replace("_", " ")
    issue = (title or topic_l).strip()
    if len(issue) > 100:
        issue = issue[:97] + "…"
    cue = ""
    low = (evidence or "").lower()
    if "refund" in low or "return" in low:
        cue = "If this is about a refund or return, I can check eligibility once I have your order."
    elif "deliver" in low or "late" in low or "track" in low:
        cue = "If this is about delivery or tracking, I can pull the latest status with your order ID."
    elif "cancel" in low:
        cue = "If you need to cancel, I’ll check whether we can still stop preparation."
    else:
        cue = "I want to make sure I give you the right fix for your account."
    return (
        f"Hey! Happy to help with {topic_l}.\n\n"
        f"I can see this relates to “{issue}”. {cue}\n\n"
        f"Could you share:\n"
        f"1) Your order ID or account email\n"
        f"2) What you expected vs what you saw\n"
        f"3) Any error message if you have one\n\n"
        f"Once I have those, I’ll take the next step for you right away."
    )


def build_customer_input(
    *,
    brief: dict[str, Any],
    title: str,
    topic: str,
    evidence: str,
    url: str = "",
) -> str:
    """Training input: customer message + internal evidence notes for the agent."""
    pair = domain_gold_pair(
        brief=brief, title=title, evidence=evidence, topic=topic, url=url
    )
    return pair["input"]


_SYNTH_SYSTEM = """You write gold training outputs for Helix (LLM training data studio).

You produce ONLY the assistant/support agent reply text (no JSON, no markdown fences).

Rules:
1. Ground claims in the evidence. Do not invent policies, prices, promo codes, or SLAs.
2. For customer SUPPORT domains:
   - Write as a friendly support agent speaking TO the customer.
   - Acknowledge the issue, help with what you can, give a concrete next step
     (usually ask for order ID / account email / what they expected vs saw).
   - NEVER tell the customer to send you internal policy pages, ticket fields, or docs.
   - NEVER paste raw scraped web/marketing text. Paraphrase useful facts in natural language.
   - NEVER use the phrase "Based on the available documentation:" followed by a dump.
   - NEVER refuse to help. If evidence is thin, ask a friendly clarifying question in-voice.
3. If evidence is thin: still be helpful—ask clarifying questions a real agent would ask.
4. Match the plan tone if provided (e.g. casual, friendly).
5. Keep reply under ~180 words.
"""


def synthesize_gold_with_llm(
    *,
    brief: dict[str, Any],
    title: str,
    evidence: str,
    topic: str,
    url: str = "",
    tenant: Any | None = None,
    max_attempts: int = MAX_SYNTH_ATTEMPTS,
) -> dict[str, Any] | None:
    """
    LLM synthesis with self-correction loop:
    generate → quality gates → critique → regenerate incorporating critique → re-check.
    """
    try:
        from helix.llm.client import get_llm_client_for_tenant

        client = get_llm_client_for_tenant(tenant)
    except Exception:  # noqa: BLE001
        return None

    domain = brief.get("domain") or ""
    mission = brief.get("mission") or ""
    instructions = brief.get("agent_instructions") or ""
    support = _is_support_domain(brief, topic)
    input_text = build_customer_input(
        brief=brief, title=title, evidence=evidence, topic=topic, url=url
    )

    user_msg = (
        f"Domain: {domain}\n"
        f"Mission: {mission}\n"
        f"Topic: {topic}\n"
        f"Plan style notes: {instructions or '(none)'}\n"
        f"Support domain: {support}\n"
        f"Source title: {title}\n"
        f"Source URL: {url or '(none)'}\n"
        f"Evidence (paraphrase only; do not invent beyond this):\n{evidence[:1500]}\n\n"
        f"Write the ideal assistant OUTPUT only for this training input:\n"
        f"-----\n{input_text[:2000]}\n-----\n"
    )

    last_out = ""
    last_reasons: list[str] = []
    critiques: list[str] = []
    attempts = max(1, min(int(max_attempts or MAX_SYNTH_ATTEMPTS), 5))

    for attempt in range(attempts):
        try:
            system = _SYNTH_SYSTEM
            if attempt and last_reasons:
                critique = critique_for_reasons(last_reasons, last_out)
                critiques.append(critique)
                system += (
                    f"\n\nSELF-CORRECTION (attempt {attempt + 1}/{attempts}):\n"
                    f"Your previous draft was REJECTED for: {'; '.join(last_reasons)}.\n"
                    f"Critique you MUST incorporate:\n{critique}\n"
                    "Rewrite a better reply that fixes every point above."
                )
            resp = client.chat(
                system=system,
                messages=[{"role": "user", "content": user_msg}],
                tools=None,
                tool_choice=None,
            )
            try:
                from helix.llm.client import cost_from_usage
                from helix.services.cost_tracking import record_openrouter_spend

                usage = getattr(resp, "usage", None)
                if usage and tenant is not None:
                    amt, _src = cost_from_usage(
                        usage, model=getattr(client, "model", None)
                    )
                    if amt > 0:
                        record_openrouter_spend(tenant, amt)
            except Exception:  # noqa: BLE001
                pass
            last_out = (resp.choices[0].message.content or "").strip()
            if last_out.startswith("```"):
                last_out = re.sub(r"^```(?:\w+)?\s*", "", last_out)
                last_out = re.sub(r"\s*```$", "", last_out)
            last_reasons = quality_reject_reasons(
                brief=brief, topic=topic, evidence=evidence, output=last_out
            )
            if not last_reasons:
                return {
                    "input": input_text[:4000],
                    "output": last_out[:2000],
                    "rationale": (
                        "LLM-synthesized reply grounded in gathered evidence; "
                        f"passed quality gates after {attempt + 1} attempt(s)."
                    ),
                    "difficulty": "moderate" if len(evidence) > 80 else "edge-case",
                    "is_negative": False,
                    "verification_status": "verified",
                    "synth": "llm",
                    "quality_ok": True,
                    "synth_attempts": attempt + 1,
                    "critiques": critiques,
                }
        except Exception:  # noqa: BLE001
            return None

    # Exhausted retries — never leave a refuse/echo; use clarifying fallback for support
    if support:
        fallback = clarifying_support_fallback(
            title=title, topic=topic, evidence=evidence
        )
        reasons = quality_reject_reasons(
            brief=brief, topic=topic, evidence=evidence, output=fallback
        )
        if not reasons:
            return {
                "input": input_text[:4000],
                "output": fallback[:2000],
                "rationale": (
                    "Self-correction exhausted; used clarifying support fallback "
                    "(friendly questions, never refuse-for-docs or scrape echo)."
                ),
                "difficulty": "edge-case",
                "is_negative": False,
                "verification_status": "verified",
                "synth": "llm_fallback_clarify",
                "quality_ok": True,
                "synth_attempts": attempts,
                "critiques": critiques,
                "prior_reject_reasons": last_reasons,
            }

    return {
        "input": input_text[:4000],
        "output": last_out[:2000] if last_out else "",
        "rationale": "Failed quality gates after self-correction loop.",
        "difficulty": "edge-case",
        "is_negative": False,
        "verification_status": "rejected",
        "synth": "llm",
        "quality_ok": False,
        "reject_reasons": last_reasons,
        "synth_attempts": attempts,
        "critiques": critiques,
    }


def synthesize_gold_pair(
    *,
    brief: dict[str, Any],
    title: str,
    evidence: str,
    topic: str,
    url: str = "",
    tenant: Any | None = None,
    prefer_llm: bool = True,
) -> dict[str, Any] | None:
    """
    Produce a gold training pair that passes quality gates.
    Always attaches reject_reasons when quality_ok is False so job logs can surface them.
    """
    last_reasons: list[str] = []
    last_out = ""

    if prefer_llm:
        llm_pair = synthesize_gold_with_llm(
            brief=brief,
            title=title,
            evidence=evidence,
            topic=topic,
            url=url,
            tenant=tenant,
        )
        if llm_pair and llm_pair.get("quality_ok"):
            return llm_pair
        if llm_pair:
            last_reasons = list(llm_pair.get("reject_reasons") or [])
            last_out = llm_pair.get("output") or ""

    # Template fallback — still must pass gates
    pair = domain_gold_pair(
        brief=brief, title=title, evidence=evidence, topic=topic, url=url
    )
    reasons = quality_reject_reasons(
        brief=brief,
        topic=topic,
        evidence=evidence,
        output=pair.get("output") or "",
    )
    if not reasons:
        pair["verification_status"] = "verified"
        pair["synth"] = "template"
        pair["quality_ok"] = True
        pair["reject_reasons"] = []
        return pair

    last_reasons = reasons
    last_out = pair.get("output") or ""

    # Domain-appropriate clarifying fallback (must pass gates)
    if _is_support_domain(brief, topic):
        out = clarifying_support_fallback(
            title=title, topic=topic, evidence=evidence
        )
        synth_tag = "template_clarify_support"
    else:
        out = clarifying_domain_fallback(
            title=title,
            topic=topic,
            domain=(brief.get("domain") or "this domain"),
            mission=(brief.get("mission") or "Answer helpfully"),
        )
        synth_tag = "template_clarify_domain"

    reasons2 = quality_reject_reasons(
        brief=brief, topic=topic, evidence=evidence, output=out
    )
    if not reasons2:
        return {
            "input": pair["input"][:4000],
            "output": out[:2000],
            "rationale": (
                f"Template failed gates ({'; '.join(reasons)}); "
                f"used {synth_tag} fallback."
            ),
            "difficulty": "edge-case",
            "is_negative": False,
            "verification_status": "verified",
            "synth": synth_tag,
            "quality_ok": True,
            "reject_reasons": [],
            "prior_template_reject_reasons": reasons,
        }

    return {
        "input": pair.get("input", "")[:4000],
        "output": (last_out or out)[:2000],
        "rationale": "All synthesis paths failed quality gates.",
        "difficulty": "edge-case",
        "is_negative": False,
        "verification_status": "rejected",
        "synth": "failed",
        "quality_ok": False,
        "reject_reasons": reasons2 or last_reasons or ["synth_failed"],
        "template_reject_reasons": reasons,
        "fallback_reject_reasons": reasons2,
    }


def format_rejection_reason(reasons: list[str]) -> str:
    """Human-readable single-string reason for API/UI."""
    if not reasons:
        return ""
    labels = {
        "support_refuses_or_demands_internal_docs": (
            "Support reply refuses to help or demands internal docs from the customer"
        ),
        "support_refuses_outright": "Support reply refuses to answer",
        "documentation_dump_wrapper": "Output dumps evidence under a documentation wrapper",
        "verbatim_evidence_echo": "Output is mostly a verbatim paste of evidence",
        "long_verbatim_chunk": "Output contains a long contiguous copy of source text",
        "evidence_substring_dump": "Output embeds a large evidence substring",
        "missing_concrete_customer_next_step": (
            "Support reply lacks a concrete next step (order ID / account email / etc.)"
        ),
        "asks_customer_for_internal_resources": (
            "Asks the customer for internal policy/doc links"
        ),
        "promo_code_echo_from_marketing": "Regurgitates promo codes from marketing scrapes",
        "marketing_chrome_in_support_reply": "Contains marketing chrome (like-and-share, #ad)",
        "output_too_short": "Output is too short to be useful training gold",
        "cross_domain_contamination": (
            "Gold content belongs to a different product domain than its topic/plan "
            "(e.g. food-delivery FAQ labeled as HR/PTO) — cross-plan corpus contamination"
        ),
        "topic_content_mismatch": (
            "Gold sub-topic label does not match the content "
            "(e.g. PTO text tagged as remote-work eligibility)"
        ),
    }
    parts = [labels.get(r, r.replace("_", " ")) for r in reasons]
    return "; ".join(parts)


# Domain families for cross-plan / mislabel detection
_DOMAIN_FAMILY_CUES: dict[str, tuple[str, ...]] = {
    "food_delivery": (
        "order id",
        "order number",
        "arrived late",
        "late delivery",
        "missing items",
        "missing from my bag",
        "food arrived",
        "restaurant",
        "driver",
        "delivery is more than",
        "re-delivered",
        "cold pizza",
        "spoiled",
        "tracking status",
        "kitchen",
    ),
    "hr": (
        "pto",
        "paid time off",
        "accrue",
        "accrual",
        "carry over",
        "hr portal",
        "employee handbook",
        "remote work",
        "hybrid work",
        "manager approval",
        "full-time employees",
        "second full month",
        "benefits enrollment",
        "new hire",
    ),
    "sales": (
        "objection",
        "prospect",
        "pipeline",
        "discovery call",
        "too expensive",
        "quota",
        "deal stage",
    ),
    "legal": (
        "gdpr",
        "data subject",
        "privacy policy",
        "terms of service",
        "personal data",
        "erasure",
    ),
    "influencer": (
        "campaign brief",
        "creator",
        "sponsored",
        "influencer",
        "instagram",
        "tiktok",
        "brand collab",
    ),
}

_TOPIC_FAMILY_CUES: dict[str, tuple[str, ...]] = {
    "food_delivery": (
        "late_delivery",
        "missing_item",
        "refund",
        "driver",
        "delivery",
        "order",
        "food",
        "support_reply",
    ),
    "hr": (
        "pto",
        "leave",
        "accrual",
        "carryover",
        "remote",
        "hybrid",
        "handbook",
        "hr_policy",
        "benefit",
        "onboard",
        "payroll",
    ),
    "sales": ("objection", "sales", "pipeline", "prospect", "quota"),
    "legal": ("gdpr", "privacy", "legal", "compliance", "contract"),
    "influencer": (
        "campaign",
        "creator",
        "influencer",
        "budget_allocation",
        "beauty",
        "fitness",
        "gaming",
    ),
}


def _family_scores_from_text(text: str) -> dict[str, int]:
    low = (text or "").lower()
    scores: dict[str, int] = {}
    for fam, cues in _DOMAIN_FAMILY_CUES.items():
        scores[fam] = sum(1 for c in cues if c in low)
    return scores


def _family_from_topic(topic: str) -> str | None:
    tl = (topic or "").lower().replace(" ", "_")
    best: str | None = None
    best_n = 0
    for fam, cues in _TOPIC_FAMILY_CUES.items():
        n = sum(1 for c in cues if c in tl)
        if n > best_n:
            best_n = n
            best = fam
    return best if best_n > 0 else None


def _dominant_content_family(text: str) -> tuple[str | None, int, dict[str, int]]:
    scores = _family_scores_from_text(text)
    if not scores or max(scores.values()) <= 0:
        return None, 0, scores
    fam = max(scores, key=lambda k: scores[k])
    return fam, scores[fam], scores


def cross_domain_reject_reasons(
    *,
    topic: str,
    input_text: str,
    output_text: str,
    metadata_json: str | None = None,
) -> list[str]:
    """
    Detect gold poisoned by another product domain (cross-plan corpus contamination)
    or clear topic/content mismatch within a domain.
    """
    import json

    reasons: list[str] = []
    blob = f"{input_text or ''}\n{output_text or ''}"
    content_fam, content_score, scores = _dominant_content_family(blob)
    topic_fam = _family_from_topic(topic or "")

    meta_domain = ""
    try:
        meta = json.loads(metadata_json or "{}")
        if isinstance(meta, dict):
            meta_domain = str(meta.get("domain") or "")
    except Exception:  # noqa: BLE001
        meta = {}

    meta_fam, meta_score, _ = _dominant_content_family(meta_domain)

    # Strong content family vs conflicting topic family
    if content_fam and content_score >= 2 and topic_fam and content_fam != topic_fam:
        reasons.append("cross_domain_contamination")

    # Metadata domain (plan domain at write time) conflicts with content
    if (
        content_fam
        and content_score >= 2
        and meta_fam
        and meta_score >= 1
        and content_fam != meta_fam
    ):
        if "cross_domain_contamination" not in reasons:
            reasons.append("cross_domain_contamination")

    # Explicit food content under HR topic labels (common contamination pattern)
    if topic_fam == "hr" and scores.get("food_delivery", 0) >= 2:
        if "cross_domain_contamination" not in reasons:
            reasons.append("cross_domain_contamination")

    # Same domain family but wrong sub-topic: PTO content under remote topic
    tl = (topic or "").lower()
    low = blob.lower()
    if any(x in tl for x in ("remote", "hybrid")) and any(
        x in low for x in ("pto", "accrue", "carry over", "paid time")
    ) and not any(x in low for x in ("remote", "hybrid", "wfh", "work from home")):
        reasons.append("topic_content_mismatch")
    if any(x in tl for x in ("pto", "accrual", "carryover", "leave")) and any(
        x in low for x in ("remote work", "hybrid", "work from home")
    ) and not any(x in low for x in ("pto", "accrue", "vacation", "carry over", "leave")):
        reasons.append("topic_content_mismatch")

    return reasons


def backfill_reject_cross_domain_gold(
    db: Any,
    *,
    owner_user_id: str | None = None,
    tenant_id: str | None = None,
    limit: int = 10000,
    skip_seeds: bool = True,
) -> dict[str, Any]:
    """
    One-shot cleanup: reject gold rows whose content domain conflicts with
    their topic/plan (cross-plan corpus contamination).
    Idempotent via metadata cross_domain_backfill_v1.
    """
    import json

    from helix.db import models as m
    from helix.services.library import _is_seed_kind

    q = db.query(m.GoldExample).filter_by(is_archived=False)
    if owner_user_id:
        q = q.filter_by(owner_user_id=owner_user_id)
    if tenant_id:
        q = q.filter_by(tenant_id=tenant_id)
    rows = q.order_by(m.GoldExample.created_at.asc()).limit(limit).all()

    scanned = 0
    newly_rejected = 0
    already_rejected = 0
    skipped_seed = 0
    skipped_clean = 0
    details: list[dict[str, Any]] = []
    tenant_slug_cache: dict[str, str | None] = {}

    for g in rows:
        scanned += 1
        tid = g.tenant_id
        if tid not in tenant_slug_cache:
            trow = db.query(m.Tenant).filter_by(id=tid).first()
            tenant_slug_cache[tid] = trow.slug if trow else None
        try:
            meta = json.loads(g.metadata_json or "{}")
            if not isinstance(meta, dict):
                meta = {}
        except Exception:  # noqa: BLE001
            meta = {}

        if skip_seeds and _is_seed_kind(
            g.source_kind,
            g.topic,
            input_text=g.input_text,
            metadata_json=g.metadata_json,
            source_ref=g.source_ref,
            created_at=g.created_at,
            tenant_slug=tenant_slug_cache[tid],
        ):
            skipped_seed += 1
            continue

        reasons = cross_domain_reject_reasons(
            topic=g.topic or "",
            input_text=g.input_text or "",
            output_text=g.output_text or "",
            metadata_json=g.metadata_json,
        )
        if not reasons:
            skipped_clean += 1
            # Still mark scanned so we don't thrash
            if not meta.get("cross_domain_backfill_v1"):
                meta["cross_domain_backfill_v1"] = "clean"
                g.metadata_json = json.dumps(meta)
            continue

        reason_str = format_rejection_reason(reasons)
        content_fam, content_score, scores = _dominant_content_family(
            f"{g.input_text or ''}\n{g.output_text or ''}"
        )
        topic_fam = _family_from_topic(g.topic or "")

        if (g.verification_status or "").lower() == "rejected":
            # Ensure reasons persisted even if already rejected
            meta["rejection_reasons"] = list(
                dict.fromkeys(
                    list(meta.get("rejection_reasons") or []) + reasons
                )
            )
            meta["rejection_reason"] = format_rejection_reason(
                meta["rejection_reasons"]
            )
            meta["cross_domain_backfill_v1"] = "rejected"
            meta["cross_domain_content_family"] = content_fam
            meta["cross_domain_topic_family"] = topic_fam
            meta["cross_domain_scores"] = scores
            g.metadata_json = json.dumps(meta)
            already_rejected += 1
            details.append(
                {
                    "id": g.id,
                    "topic": g.topic,
                    "action": "already_rejected_annotated",
                    "rejection_reasons": reasons,
                    "content_family": content_fam,
                    "topic_family": topic_fam,
                }
            )
            continue

        g.verification_status = "rejected"
        meta["rejection_reasons"] = reasons
        meta["rejection_reason"] = reason_str
        meta["cross_domain_backfill_v1"] = "rejected"
        meta["cross_domain_content_family"] = content_fam
        meta["cross_domain_topic_family"] = topic_fam
        meta["cross_domain_scores"] = scores
        g.metadata_json = json.dumps(meta)
        note = f"[cross-domain backfill rejected: {reason_str}]"
        if g.rationale:
            if "cross-domain backfill" not in (g.rationale or ""):
                g.rationale = f"{g.rationale}\n{note}"[:1000]
        else:
            g.rationale = note[:1000]
        newly_rejected += 1
        details.append(
            {
                "id": g.id,
                "topic": g.topic,
                "action": "newly_rejected",
                "rejection_reasons": reasons,
                "rejection_reason": reason_str,
                "content_family": content_fam,
                "topic_family": topic_fam,
                "scores": scores,
            }
        )

    db.commit()
    return {
        "scanned": scanned,
        "newly_rejected": newly_rejected,
        "already_rejected_annotated": already_rejected,
        "skipped_seed": skipped_seed,
        "skipped_clean": skipped_clean,
        "items": details[:100],
    }


def rejection_fields_from_meta(metadata_json: str | None, rationale: str | None = None) -> dict:
    """Extract structured rejection fields for gold API payloads."""
    import json

    reasons: list[str] = []
    try:
        meta = json.loads(metadata_json or "{}")
        if not isinstance(meta, dict):
            meta = {}
    except Exception:  # noqa: BLE001
        meta = {}
    raw = meta.get("rejection_reasons") or meta.get("quality_backfill_reject") or []
    if isinstance(raw, list):
        reasons = [str(x) for x in raw]
    elif isinstance(raw, str) and raw:
        reasons = [raw]
    reason = meta.get("rejection_reason") or format_rejection_reason(reasons)
    if not reason and rationale and "quality-backfill rejected:" in (rationale or ""):
        # legacy parse from rationale note
        try:
            reason = rationale.split("quality-backfill rejected:", 1)[1].strip()
            if not reasons:
                reasons = [r.strip() for r in reason.split(";") if r.strip()]
        except Exception:  # noqa: BLE001
            pass
    return {
        "rejection_reasons": reasons,
        "rejection_reason": reason or None,
    }


def backfill_quality_on_gold_rows(
    db: Any,
    *,
    owner_user_id: str | None = None,
    tenant_id: str | None = None,
    limit: int = 5000,
    skip_seeds: bool = True,
    restore_seeds: bool = True,
) -> dict[str, Any]:
    """
    Re-run hard quality gates on existing gold rows.
    Bad patterns (refuse/echo) are demoted from verified → rejected.

    Seed/demo rows are skipped by default (and optionally restored if a prior
    over-eager backfill rejected them under the wrong domain brief).
    """
    import json

    from helix.db import models as m
    from helix.services.brief import get_active_project, project_to_dict
    from helix.services.library import _is_seed_kind

    q = db.query(m.GoldExample).filter_by(is_archived=False)
    if owner_user_id:
        q = q.filter_by(owner_user_id=owner_user_id)
    if tenant_id:
        q = q.filter_by(tenant_id=tenant_id)
    rows = q.order_by(m.GoldExample.created_at.asc()).limit(limit).all()

    brief_cache: dict[str, dict] = {}
    tenant_slug_cache: dict[str, str | None] = {}
    scanned = 0
    rejected = 0
    already_bad = 0
    skipped_seed = 0
    restored_seed = 0
    details: list[dict[str, Any]] = []

    for g in rows:
        scanned += 1
        tid = g.tenant_id
        if tid not in tenant_slug_cache:
            trow = db.query(m.Tenant).filter_by(id=tid).first()
            tenant_slug_cache[tid] = trow.slug if trow else None
        slug = tenant_slug_cache[tid]
        is_seed = _is_seed_kind(
            g.source_kind,
            g.topic,
            input_text=g.input_text,
            metadata_json=g.metadata_json,
            source_ref=g.source_ref,
            created_at=g.created_at,
            tenant_slug=slug,
        )

        try:
            meta = json.loads(g.metadata_json or "{}")
            if not isinstance(meta, dict):
                meta = {}
        except Exception:  # noqa: BLE001
            meta = {}

        # Seed rows: never auto-reject under an unrelated support brief
        if skip_seeds and is_seed:
            skipped_seed += 1
            if restore_seeds and (g.verification_status or "").lower() == "rejected":
                # Only restore if rejection was from quality backfill
                if meta.get("quality_backfill") or meta.get("quality_backfill_reject"):
                    g.verification_status = "verified"
                    meta.pop("quality_backfill_reject", None)
                    meta.pop("rejection_reasons", None)
                    meta.pop("rejection_reason", None)
                    meta["seed_restored_from_backfill"] = True
                    g.metadata_json = json.dumps(meta)
                    if g.rationale and "quality-backfill rejected:" in g.rationale:
                        g.rationale = re.sub(
                            r"\n?\[quality-backfill rejected:[^\]]*\]",
                            "",
                            g.rationale,
                        ).strip() or g.rationale
                    restored_seed += 1
                    details.append(
                        {
                            "id": g.id,
                            "topic": g.topic,
                            "action": "restored_seed",
                            "is_seed": True,
                        }
                    )
            continue

        if tid not in brief_cache:
            brief: dict = {}
            try:
                proj = get_active_project(db, tid)
                if proj:
                    brief = project_to_dict(proj)
            except Exception:  # noqa: BLE001
                brief = {}
            brief_cache[tid] = brief
        brief = brief_cache[tid]
        reasons = quality_reject_reasons(
            brief=brief,
            topic=g.topic or "",
            evidence=g.input_text or "",
            output=g.output_text or "",
            input_text=g.input_text or "",
        )
        if not reasons:
            # Clear stale rejection metadata if now clean
            if meta.get("rejection_reasons") or meta.get("quality_backfill_reject"):
                meta.pop("rejection_reasons", None)
                meta.pop("rejection_reason", None)
                meta.pop("quality_backfill_reject", None)
                g.metadata_json = json.dumps(meta)
            continue
        reason_str = format_rejection_reason(reasons)
        if (g.verification_status or "").lower() == "rejected":
            # Ensure reasons are persisted even for already-rejected rows
            meta["rejection_reasons"] = reasons
            meta["rejection_reason"] = reason_str
            meta["quality_backfill_reject"] = reasons
            meta["quality_backfill"] = True
            g.metadata_json = json.dumps(meta)
            already_bad += 1
            details.append(
                {
                    "id": g.id,
                    "topic": g.topic,
                    "action": "already_rejected",
                    "rejection_reasons": reasons,
                    "rejection_reason": reason_str,
                }
            )
            continue
        g.verification_status = "rejected"
        meta["rejection_reasons"] = reasons
        meta["rejection_reason"] = reason_str
        meta["quality_backfill_reject"] = reasons
        meta["quality_backfill"] = True
        g.metadata_json = json.dumps(meta)
        note = f"[quality-backfill rejected: {reason_str}]"
        if g.rationale:
            if "quality-backfill" not in (g.rationale or ""):
                g.rationale = f"{g.rationale}\n{note}"[:1000]
        else:
            g.rationale = note[:1000]
        rejected += 1
        details.append(
            {
                "id": g.id,
                "topic": g.topic,
                "action": "newly_rejected",
                "rejection_reasons": reasons,
                "rejection_reason": reason_str,
                "is_seed": is_seed,
            }
        )
    db.commit()
    return {
        "scanned": scanned,
        "newly_rejected": rejected,
        "already_rejected": already_bad,
        "skipped_seed": skipped_seed,
        "restored_seed": restored_seed,
        "skip_seeds": skip_seeds,
        "items": details[:100],
    }


def reverify_gold_for_role(
    db: Any,
    *,
    tenant_id: str,
    owner_user_id: str,
    role_text: str,
    risk_level: str = "medium",
    limit: int = 200,
) -> dict[str, Any]:
    """Post-generation: reject verified gold that drifted off the stated role."""
    import json

    from helix.db import models as m
    from helix.services.role_risk import role_relevance_reject_reasons

    rows = (
        db.query(m.GoldExample)
        .filter_by(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            is_archived=False,
            verification_status="verified",
        )
        .order_by(m.GoldExample.created_at.desc())
        .limit(limit)
        .all()
    )
    scanned = 0
    rejected = 0
    items: list[dict[str, Any]] = []
    for g in rows:
        scanned += 1
        reasons = role_relevance_reject_reasons(
            role_text=role_text,
            input_text=g.input_text or "",
            output=g.output_text or "",
            risk_level=risk_level,
        )
        if not reasons:
            continue
        try:
            meta = json.loads(g.metadata_json or "{}")
        except Exception:  # noqa: BLE001
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        if meta.get("role_reverify_v1"):
            continue
        g.verification_status = "rejected"
        meta["rejection_reasons"] = reasons
        meta["rejection_reason"] = "; ".join(reasons)
        meta["role_reverify_v1"] = "rejected"
        g.metadata_json = json.dumps(meta)
        note = f"[role-reverify rejected: {', '.join(reasons)}]"
        g.rationale = f"{(g.rationale or '').strip()}\n{note}".strip()[:1000]
        rejected += 1
        items.append({"id": g.id, "reasons": reasons})
    db.commit()
    return {"scanned": scanned, "newly_rejected": rejected, "items": items[:50]}
