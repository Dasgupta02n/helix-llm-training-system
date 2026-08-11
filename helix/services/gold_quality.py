"""Gold answer synthesis + hard quality gates (reject echo/refuse patterns)."""

from __future__ import annotations

import json
import re
from typing import Any

from helix.services.domain_ontology import _is_support_domain, domain_gold_pair


# Patterns that must never ship as verified support gold
_REFUSE_INTERNAL_DOCS = re.compile(
    r"(policy page|internal doc|docs section|ticket field|"
    r"sources provided to answer confidently|"
    r"don.?t have enough verified evidence|"
    r"share the specific policy)",
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


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _overlap_ratio(a: str, b: str) -> float:
    """Fraction of evidence content that appears as a long contiguous dump in output."""
    ea, eb = _normalize(a), _normalize(b)
    if len(ea) < 40 or len(eb) < 40:
        return 0.0
    # sliding window of ~60 chars from evidence (catch partial dumps)
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
    """True if any long contiguous evidence span is pasted into the output."""
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
    # Prefer explicit evidence; fall back to input notes for review agents
    if len(ev.strip()) < 40 and input_text:
        # strip common training-input prefixes so we compare against notes body
        ev = input_text
    support = _is_support_domain(brief, topic)

    if len(out.strip()) < 40:
        reasons.append("output_too_short")

    if support and _REFUSE_INTERNAL_DOCS.search(out):
        reasons.append("support_refuses_or_demands_internal_docs")

    if _DOC_DUMP_WRAPPER.search(out):
        reasons.append("documentation_dump_wrapper")

    # Refuse-to-answer patterns even outside support keyword brief
    if re.search(
        r"(cannot answer|can'?t answer|unable to (help|answer)|"
        r"insufficient (evidence|information) to answer|"
        r"need (more|additional) (internal )?sources)",
        out,
        re.I,
    ) and support:
        reasons.append("support_refuses_outright")

    # Large verbatim paste of evidence
    if len(ev) >= 80 and _overlap_ratio(ev, out) >= 0.35:
        reasons.append("verbatim_evidence_echo")

    if _has_long_verbatim_chunk(ev, out, min_len=55):
        reasons.append("long_verbatim_chunk")

    # Output mostly equals evidence
    if len(ev) >= 60 and _normalize(ev)[:200] in _normalize(out):
        if len(ev) > 120:
            reasons.append("evidence_substring_dump")

    if support:
        # Must offer a concrete next step for the customer
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
        )
        ol = out.lower()
        if not any(c in ol for c in next_step_cues):
            reasons.append("missing_concrete_customer_next_step")
        # Should not address customer as if they have internal tools
        if re.search(r"provide (a |the )?(url|link) to (our |the )?internal", ol):
            reasons.append("asks_customer_for_internal_resources")
        # Promo-code regurgitation from marketing scrapes is not a support reply
        if re.search(r"\b(code|promo)\s*[:=]?\s*[A-Z0-9]{4,}\b", out) and re.search(
            r"(like and share|click here|#ad|sponsored)", ev, re.I
        ):
            reasons.append("promo_code_echo_from_marketing")

    if _RAW_ECHO_MARKERS.search(out) and support:
        reasons.append("marketing_chrome_in_support_reply")

    return reasons


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
    # domain_gold_pair already builds a good input; reuse it
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
) -> dict[str, Any] | None:
    """LLM synthesis of a gold pair. Returns None if LLM unavailable or fails quality."""
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
    for attempt in range(2):
        try:
            resp = client.chat(
                system=_SYNTH_SYSTEM
                + (
                    "\nPrevious attempt was REJECTED: "
                    + ("; ".join(quality_reject_reasons(
                        brief=brief, topic=topic, evidence=evidence, output=last_out
                    )) if last_out else "")
                    + "\nRewrite fixing those issues."
                    if attempt and last_out
                    else ""
                ),
                messages=[{"role": "user", "content": user_msg}],
                tools=None,
                tool_choice=None,
            )
            last_out = (resp.choices[0].message.content or "").strip()
            # strip accidental quotes/fences
            if last_out.startswith("```"):
                last_out = re.sub(r"^```(?:\w+)?\s*", "", last_out)
                last_out = re.sub(r"\s*```$", "", last_out)
            reasons = quality_reject_reasons(
                brief=brief, topic=topic, evidence=evidence, output=last_out
            )
            if not reasons:
                return {
                    "input": input_text[:4000],
                    "output": last_out[:2000],
                    "rationale": (
                        "LLM-synthesized reply grounded in gathered evidence; "
                        "passed hard quality gates (no refuse-to-customer-for-docs, "
                        "no raw scrape echo, concrete next step for support)."
                    ),
                    "difficulty": "moderate" if len(evidence) > 80 else "edge-case",
                    "is_negative": False,
                    "verification_status": "verified",
                    "synth": "llm",
                    "quality_ok": True,
                }
        except Exception:  # noqa: BLE001
            return None
    return {
        "input": input_text[:4000],
        "output": last_out[:2000] if last_out else "",
        "rationale": "Failed quality gates after retry.",
        "difficulty": "edge-case",
        "is_negative": False,
        "verification_status": "rejected",
        "synth": "llm",
        "quality_ok": False,
        "reject_reasons": quality_reject_reasons(
            brief=brief, topic=topic, evidence=evidence, output=last_out
        ),
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
    Returns None if nothing acceptable can be produced.
    """
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
    if reasons:
        return None
    pair["verification_status"] = "verified"
    pair["synth"] = "template"
    pair["quality_ok"] = True
    return pair
