"""Role-type + risk-level guardrails for Riu setup and gold gates.

Branches on what the model will *do* (hire, lend, advise a person) — not on
LoRA vs QLoRA. High-risk roles require more edge cases and stricter gates.
"""

from __future__ import annotations

import re
from typing import Any

# Recommended Double Helix v1 base (P5). Named here so Riu can recommend now.
RECOMMENDED_BASE_MODEL = "meta-llama/Llama-3.1-8B-Instruct"
RECOMMENDED_TRAINING = "QLoRA"

_HIGH = (
    (r"\b(cv|resume|curriculum vitae)\b.{0,40}\b(screen|rank|score|filter|shortlist)", "hiring"),
    (r"\b(screen|rank|score|filter|shortlist).{0,40}\b(cv|resume|applicant)", "hiring"),
    (r"\b(hir(e|ing)|recruiter|applicant tracking|shortlist candidate)", "hiring"),
    (r"\b(credit|loan|underwrit|lending|mortgage|credit.?card approval)\b", "credit"),
    (r"\b(medical|diagnos|prescription|clinical advice|patient)\b", "medical"),
    (r"\b(legal advice|attorney|litigation|criminal sentenc)", "legal"),
    (r"\b(insurance claim|claims adjust)", "insurance"),
    (r"\b(child protect|social work|welfare eligibility)", "welfare"),
    (r"\b(biometric|face match|identity verif).{0,20}\b(decision|approve|deny)", "identity"),
    (r"\b(fired|terminate employment|disciplinary)\b", "employment"),
)

_MEDIUM = (
    (r"\b(customer support|helpdesk|refund|billing dispute)\b", "support"),
    (r"\b(hr |human resources|employee handbook|pto |payroll)\b", "hr"),
    (r"\b(sales|objection|closer|outbound)\b", "sales"),
    (r"\b(teacher|tutor|grading students|exam)\b", "education"),
    (r"\b(financial education|credit education|money coach)\b", "finance_edu"),
)

_LOW = (
    (r"\b(caption|tagline|slogan|social post)\b", "caption"),
    (r"\b(product description|seo blurb|marketing copy)\b", "copy"),
    (r"\b(faq|help article|knowledge base)\b", "faq"),
)


def classify_role(text: str, *, domain: str = "", mission: str = "") -> dict[str, Any]:
    blob = f"{text} {domain} {mission}".lower()

    def hit(pairs: tuple) -> str | None:
        for pat, kind in pairs:
            if re.search(pat, blob, re.I):
                return kind
        return None

    role_type = hit(_HIGH)
    if role_type:
        risk = "high"
    else:
        role_type = hit(_MEDIUM)
        if role_type:
            risk = "medium"
        else:
            role_type = hit(_LOW) or "general"
            risk = "low"

    if risk == "high":
        edges, quality = 3, 1
    elif risk == "medium":
        edges, quality = 2, 2
    else:
        edges, quality = 1, 3

    return {
        "role_type": role_type,
        "risk_level": risk,
        "edge_cases_required": edges,
        "quality_mode": quality,
        "recommended_base_model": RECOMMENDED_BASE_MODEL,
        "recommended_training": RECOMMENDED_TRAINING,
        "strict_fairness": risk == "high",
        "summary": (
            f"{risk} risk ({role_type}): ask for {edges} edge case(s); "
            f"quality mode {quality}; train later with {RECOMMENDED_TRAINING} "
            f"on {RECOMMENDED_BASE_MODEL}."
        ),
    }


def role_relevance_reject_reasons(
    *,
    role_text: str,
    input_text: str,
    output: str,
    risk_level: str = "medium",
) -> list[str]:
    """Off-role or high-risk fairness failures for a single gold pair."""
    reasons: list[str] = []
    role = (role_text or "").lower()
    blob = f"{input_text} {output}".lower()
    if not role.strip():
        return reasons

    role_tokens = {w for w in re.findall(r"[a-z]{4,}", role) if w not in _STOP}
    text_tokens = set(re.findall(r"[a-z]{4,}", blob))
    if role_tokens and text_tokens:
        overlap = len(role_tokens & text_tokens) / max(len(role_tokens), 1)
        if overlap < 0.08 and not any(t in blob for t in list(role_tokens)[:6]):
            reasons.append("off_role_drift")

    # High-risk: decisions about people must not stereotype protected classes
    if risk_level == "high":
        if re.search(
            r"\b(because (they|he|she|the candidate) is )\b.{0,20}"
            r"\b(woman|man|black|white|asian|hispanic|muslim|christian|disabled|old|young)\b",
            blob,
        ):
            reasons.append("protected_class_stereotype")
        if re.search(r"\b(automatically (reject|deny|fire|hire))\b", blob):
            reasons.append("automated_adverse_decision_without_review")
    return reasons


_STOP = {
    "this",
    "that",
    "with",
    "from",
    "your",
    "about",
    "train",
    "training",
    "model",
    "should",
    "would",
    "could",
    "their",
    "them",
    "have",
    "been",
    "will",
    "into",
    "for",
    "and",
    "the",
}
