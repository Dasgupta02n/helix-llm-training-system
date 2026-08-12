"""Ontology-routed research targeting + adaptive query variation.

Support briefs prefer help-center / FAQ / Q&A / reviews; influencer briefs
keep social/campaign sources. Queries vary per attempt so retries don't
replay the same cache/dedupe key.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any


def research_domain_kind(brief: dict[str, Any], topic: str = "") -> str:
    """Coarse domain kind for source routing: support|sales|hr|legal|ecommerce|influencer|general."""
    domain = (brief.get("domain") or "").lower()
    mission = (brief.get("mission") or "").lower()
    cats = " ".join(str(c).lower() for c in (brief.get("categories") or []))
    blob = f"{domain} {mission} {cats} {topic}".lower()

    def has(*keys: str) -> bool:
        return any(k in blob for k in keys)

    # More specific verticals first so "shipping" doesn't force support for retail
    if has("e-commerce", "ecommerce", "shopify", "product catalog", "checkout", "retail", "online retail"):
        return "ecommerce"
    if has("sales", "coach", "crm", "pipeline", "lead", "outbound", "objection"):
        return "sales"
    if has("hr", "human resources", "onboarding", "employee handbook", "payroll", "benefits", "pto"):
        return "hr"
    if has("legal", "compliance", "gdpr", "contract", "terms of service", "privacy policy", "data subject"):
        return "legal"
    if has(
        "support",
        "ticket",
        "customer service",
        "helpdesk",
        "help center",
        "help desk",
        "faq",
        "billing",
        "cx ",
        "saas",
    ) or (
        has("refund", "delivery", "order", "shipping")
        and has("support", "customer", "ticket", "help")
    ):
        return "support"
    # Loose support cues without a stronger vertical
    if has("refund", "delivery", "order status", "customer support"):
        return "support"
    if has("influencer", "creator", "campaign", "instagram", "tiktok", "sponsor"):
        return "influencer"
    return "general"


# Preferred site/query operators by domain kind (appended to broaden searches)
_KIND_OPERATORS: dict[str, list[str]] = {
    "support": [
        'site:support.',
        "help center",
        "FAQ",
        '"how do I"',
        "zendesk",
        "site:reddit.com",
        "site:community.",
        "app store review",
        "customer service refund",
        "troubleshooting",
    ],
    "sales": [
        "sales playbook",
        "objection handling",
        "discovery call",
        "site:reddit.com sales",
        "cold email template",
    ],
    "hr": [
        "employee handbook",
        "HR policy",
        "onboarding checklist",
        "site:shrm.org",
        "benefits FAQ",
    ],
    "legal": [
        "terms of service",
        "privacy policy",
        "compliance FAQ",
        "GDPR rights",
        "contract clause",
    ],
    "ecommerce": [
        "product FAQ",
        "return policy",
        "shipping policy",
        "checkout help",
        "size guide",
    ],
    "influencer": [
        "sponsored post",
        "brand collab",
        "creator campaign",
        "instagram partnership",
    ],
    "general": [
        "guide",
        "best practices",
        "how to",
        "documentation",
    ],
}

# Sources preferred when writing work-queue style assignments
_KIND_SOURCES: dict[str, list[str]] = {
    "support": ["web", "blog", "web", "web"],  # web search with operators; blog for docs
    "sales": ["web", "blog"],
    "hr": ["web", "blog"],
    "legal": ["web", "blog"],
    "ecommerce": ["web", "blog"],
    "influencer": ["instagram", "tiktok", "youtube", "web"],
    "general": ["web", "blog"],
}

# Phrases that signal ad/marketing chrome — demote for support mining
_AD_CHROME = re.compile(
    r"(like and share|follow us|#ad\b|sponsored|shop now|click here|"
    r"subscribe now|limited time offer|promo code)",
    re.I,
)
_HELP_SIGNALS = re.compile(
    r"(help center|faq|support|how (do|can|to)|troubleshoot|refund|"
    r"return policy|shipping|order status|customer service|zendesk|"
    r"knowledge base|documentation)",
    re.I,
)


def preferred_sources(kind: str) -> list[str]:
    return list(_KIND_SOURCES.get(kind) or _KIND_SOURCES["general"])


def operators_for_kind(kind: str) -> list[str]:
    return list(_KIND_OPERATORS.get(kind) or _KIND_OPERATORS["general"])


def _mission_snip(brief: dict[str, Any], n: int = 80) -> str:
    mission = re.sub(r"\s+", " ", (brief.get("mission") or "").strip())
    return mission[:n]


def build_search_queries(
    brief: dict[str, Any],
    *,
    category: str,
    source: str = "web",
    attempt: int = 0,
    max_queries: int = 4,
) -> list[str]:
    """
    Build varied search queries for one discovery attempt.

    attempt=0 → primary focused queries
    attempt=1+ → broader operators + alternate phrasings (never identical to attempt 0)
    """
    kind = research_domain_kind(brief, category)
    domain = (brief.get("domain") or "").strip()
    cats = [str(c) for c in (brief.get("categories") or [])]
    mission = _mission_snip(brief)
    ops = operators_for_kind(kind)
    base_terms = [t for t in [category, domain] if t]

    queries: list[str] = []

    # Core: category + domain + kind-specific operator rotation
    op = ops[attempt % len(ops)] if ops else "guide"
    q0 = " ".join(p for p in [category, domain, op] if p).strip()
    if q0:
        queries.append(q0[:240])

    # Mission-grounded
    if mission:
        q1 = f"{category} {domain} {mission[:60]}".strip()
        queries.append(q1[:240])

    # Alternate category from brief
    if cats:
        alt = cats[attempt % len(cats)]
        op2 = ops[(attempt + 1) % len(ops)] if ops else "FAQ"
        queries.append(f"{alt} {domain} {op2}".strip()[:240])

    # Broaden on higher attempts: drop site: if any, add "how to" / public docs
    if attempt >= 1:
        broaden = [
            f"{domain} {category} customer FAQ",
            f"{domain} help center {category}",
            f'"{category}" {domain} site:reddit.com',
            f"{domain} troubleshooting {category}",
        ]
        if kind == "support":
            broaden.extend(
                [
                    f"{domain} refund policy",
                    f"{domain} order delivery problem",
                    f"{category} app store review {domain}",
                ]
            )
        queries.extend(broaden[attempt - 1 : attempt + 2])

    if attempt >= 2:
        # Even wider: remove domain brand lock-in, use category + public Q&A patterns
        queries.append(f"{category} customer support how to fix")
        queries.append(f"{category} FAQ common issues")

    # Deterministic de-dupe while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        qn = re.sub(r"\s+", " ", (q or "").strip().lower())
        if len(qn) < 6 or qn in seen:
            continue
        seen.add(qn)
        out.append(q.strip()[:240])
        if len(out) >= max_queries:
            break

    if not out:
        out = [(category or domain or "training examples")[:240]]

    # Salt query on attempt so cache/dedupe keys differ across retries
    if attempt > 0 and out:
        salt = hashlib.md5(f"{attempt}:{out[0]}".encode()).hexdigest()[:6]
        # Append invisible-to-search variation via trailing synonym that changes hash
        # but keeps intent: use different operator already baked in; add attempt tag
        # only to second query if present for cache bust when engines ignore unknown tokens
        out = [f"{q} {ops[(attempt + i) % len(ops)]}"[:240] for i, q in enumerate(out)]
        # ensure uniqueness from attempt-0 set
        out = [re.sub(r"\s+", " ", q).strip() for q in out]
        _ = salt  # reserved for logging

    return out[:max_queries]


def score_item_for_kind(
    *,
    kind: str,
    title: str,
    snippet: str,
    url: str = "",
    category: str = "",
    query: str = "",
) -> dict[str, Any]:
    """
    Domain-aware relevance. Support demotes ad chrome and boosts help/FAQ signals.
    """
    text = f"{title} {snippet} {url} {query}".lower()
    score = 0.28
    if category and category.lower() in text:
        score += 0.22
    words = set(re.findall(r"[a-z0-9]{3,}", text))
    cwords = set(re.findall(r"[a-z0-9]{3,}", (category or "").lower()))
    if cwords:
        score += 0.2 * (len(words & cwords) / max(len(cwords), 1))
    if query:
        qwords = set(re.findall(r"[a-z0-9]{3,}", query.lower()))
        if qwords:
            score += 0.12 * (len(words & qwords) / max(len(qwords), 1))

    if kind == "support":
        if _HELP_SIGNALS.search(text):
            score += 0.28
        if _AD_CHROME.search(text):
            score -= 0.35
        if any(
            h in (url or "").lower()
            for h in (
                "support.",
                "help.",
                "faq",
                "zendesk",
                "freshdesk",
                "intercom",
                "reddit.com",
                "community.",
            )
        ):
            score += 0.2
        # Pure social marketing landing pages
        if re.search(r"facebook\.com/.*/posts|instagram\.com/p/", url or "", re.I):
            score -= 0.25
    elif kind == "influencer":
        for w in ("sponsored", "partner", "collab", "campaign", "#ad", "review"):
            if w in text:
                score += 0.08
                break
    else:
        if _HELP_SIGNALS.search(text):
            score += 0.1

    score = round(min(max(score, 0.01), 0.99), 3)
    return {
        "relevance_score": score,
        "kind": kind,
        "help_like": bool(_HELP_SIGNALS.search(text)),
        "ad_like": bool(_AD_CHROME.search(text)),
    }


def min_evidence_threshold(batch_size: int = 5) -> int:
    """Minimum on-topic gather hits before we consider the attempt non-thin."""
    return max(3, min(batch_size, 5))
