"""Map a research-brief `sources` label to something discovery can actually gather.

Brief sources are free-text (education sites, forums, docs, sales scripts, …).
The old gather path treated anything outside instagram/tiktok/youtube/x/blog as
if it were still a social platform — or ignored it and scored only those five.

Rules:
- Public-web types are attempted via Apify google search + query operators.
- Named social platforms keep site: filters.
- Private / internal types are marked unreachable and must be escalated honestly.
"""

from __future__ import annotations

import re
from typing import Any

# Channels gather_search understands (web search unless a social site filter).
SOCIAL_CHANNELS = {"instagram", "tiktok", "youtube", "x", "facebook"}
WEB_CHANNELS = {"web", "blog", "docs", "forum", "education", "sales_script", "news"}

_UNREACHABLE = (
    (r"\btickets?\b", "Private ticket queues are not on the public web."),
    (r"\bcrm\b|\bsalesforce\b|\bhubspot\b", "CRM records are not publicly searchable."),
    (r"\bslack\b|\bteams\b|\bdiscord\b", "Internal chat is not publicly searchable."),
    (r"\bemail\b|\binbox\b|\boutlook\b", "Mailbox content is not publicly searchable."),
    (r"\bcall recording|\bphone call|\bzoom recording", "Call recordings are not on the public web."),
    (r"\binternal\b|\bintranet\b|\bprivate wiki\b", "Internal docs are not publicly searchable."),
    (r"\bconfluence\b|\bsharepoint\b|\bnotion\b", "Private wikis are not publicly searchable."),
    (r"\bour (docs|wiki|handbook|drive)\b", "Workspace-private files are not publicly searchable."),
)

_SOCIAL = {
    "instagram": ("instagram", ["site:instagram.com"]),
    "tiktok": ("tiktok", ["site:tiktok.com"]),
    "youtube": ("youtube", ["site:youtube.com"]),
    "twitter": ("x", ["site:x.com OR site:twitter.com"]),
    "x.com": ("x", ["site:x.com OR site:twitter.com"]),
    "facebook": ("facebook", ["site:facebook.com"]),
    "linkedin": ("web", ["site:linkedin.com"]),
}


def _norm(label: str) -> str:
    return re.sub(r"\s+", " ", (label or "").strip().lower())


def adapt_source(label: str) -> dict[str, Any]:
    """Return {label, channel, operators, reachable, reason}."""
    raw = (label or "").strip() or "web"
    key = _norm(raw)

    for pat, reason in _UNREACHABLE:
        if re.search(pat, key, re.I):
            return {
                "label": raw,
                "channel": "unreachable",
                "operators": [],
                "reachable": False,
                "reason": reason,
            }

    for needle, (channel, ops) in _SOCIAL.items():
        if needle in key:
            return {
                "label": raw,
                "channel": channel,
                "operators": list(ops),
                "reachable": True,
                "reason": None,
            }

    if re.search(r"\bforum|\breddit|\bcommunity|\bquora|\bstack.?overflow", key):
        return {
            "label": raw,
            "channel": "forum",
            "operators": ["site:reddit.com", "site:community.", "forum discussion"],
            "reachable": True,
            "reason": None,
        }
    if re.search(r"\bdocs?\b|\bdocumentation|\bhelp center|\bknowledge base|\bmanual", key):
        return {
            "label": raw,
            "channel": "docs",
            "operators": ["documentation", "help center", "site:support.", "knowledge base"],
            "reachable": True,
            "reason": None,
        }
    if re.search(
        r"\beducat|\bexplained|\bcourse|\bcurriculum|\bconsumer .{0,20}educat|\bguide\b|\bhow to\b",
        key,
    ):
        return {
            "label": raw,
            "channel": "education",
            "operators": [
                '"consumer education"',
                "site:.edu",
                "explained",
                "beginner guide",
            ],
            "reachable": True,
            "reason": None,
        }
    if re.search(r"\bscripts?\b|\bplaybook|\bobjection|\bpitch deck|\bsales call", key):
        return {
            "label": raw,
            "channel": "sales_script",
            "operators": ["sales script", "playbook", "objection handling", "discovery call"],
            "reachable": True,
            "reason": None,
        }
    if re.search(r"\bnews\b|\barticle|\bpress\b|\bblog\b", key):
        return {
            "label": raw,
            "channel": "blog",
            "operators": ["article", "guide"],
            "reachable": True,
            "reason": None,
        }
    if key in {"web", "www", "google", "search", "internet"}:
        return {
            "label": raw,
            "channel": "web",
            "operators": ["guide", "how to"],
            "reachable": True,
            "reason": None,
        }

    # Unknown public-looking phrase (e.g. "consumer credit card education"):
    # attempt as web search terms, do not collapse to Instagram.
    extra = [raw] if raw.lower() not in {"web", "blog"} else []
    return {
        "label": raw,
        "channel": "web",
        "operators": extra + ["guide", "how to"],
        "reachable": True,
        "reason": None,
    }


def adapt_sources(labels: list[str] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in labels or []:
        spec = adapt_source(str(raw))
        key = _norm(spec["label"])
        if key in seen:
            continue
        seen.add(key)
        out.append(spec)
    return out


def sources_for_gather(
    *,
    brief_sources: list[str] | None,
    assignment_source: str | None = None,
    domain_kind: str = "",
    fallback: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Prefer the plan's named sources. Never silently replace them with
    instagram/tiktok/youtube just because those exist on SourceReliability.
    """
    named = adapt_sources(brief_sources)
    if named:
        return named

    extras: list[str] = []
    if assignment_source:
        extras.append(assignment_source)
    for s in fallback or []:
        extras.append(s)
    if not extras:
        if domain_kind == "influencer":
            extras = ["youtube", "web"]
        else:
            extras = ["web", "blog"]
    return adapt_sources(extras)
