"""Riu official cost estimate + large-job start gate.

Public callers should import from helix.services.riu (facade).
"""

from __future__ import annotations

import re
from typing import Any

def _looks_like_cost_quote(reply: str) -> bool:
    r = reply or ""
    if re.search(r"\$\s*\d", r):
        return True
    low = r.lower()
    return any(
        w in low
        for w in (
            "credit",
            "estimate",
            "per 1,000",
            "per 1000",
            "hours",
            "gold examples",
        )
    )


def riu_start_block_reason(state: dict[str, Any]) -> str | None:
    """Same gate as jobs, but also treat library gold_target as the requested volume."""
    from helix.services.corpus import LARGE_PIPELINE_UNITS

    if state.get("accept_exploratory"):
        return None
    if state.get("seed_scale_ready"):
        return None
    try:
        gold_target = int(state.get("gold_target") or 0)
    except (TypeError, ValueError):
        gold_target = 0
    try:
        units = int(state.get("batch_size") or 5) * int(state.get("total_batches") or 2)
    except (TypeError, ValueError):
        units = 10
    corpus_docs = int(state.get("corpus_docs") or 0)
    intended = max(gold_target, units)
    if intended > LARGE_PIPELINE_UNITS and corpus_docs <= 0:
        return (
            f"I will not launch **{intended:,}** gold with no attached corpus. "
            "Large jobs (more than 10 units) need source material under My data. "
            "Web-research-only starts with **10** gold, then we review them "
            "one-by-one, generate **10 more** as proof, and only then scale. "
            "Type **start 10**. No-source scale is **~$2–$3 per gold row** "
            f"(so {intended:,} ≈ **${intended * 2:,.0f}–${intended * 3:,.0f}**). "
            "With your own docs the rate is **~$0.75–$1 per gold row**."
        )
    return None


def official_estimate_for_state(state: dict[str, Any]) -> dict[str, Any]:
    from helix.services.user_material_upload import estimate_setup_pricing

    return estimate_setup_pricing(state)


def apply_official_riu_estimate(
    reply: str,
    *,
    phase: str,
    state: dict[str, Any],
) -> str:
    """Replace invented $ / hour quotes with the job-system estimate."""
    from helix.services.user_material_upload import format_official_estimate

    pricing = official_estimate_for_state(state)
    state["pricing_estimate"] = pricing
    block = format_official_estimate(
        pricing, project=str(state.get("project_name") or state.get("domain") or "")
    )
    ph = (phase or "").lower()
    if ph in {"pricing", "confirm", "model_estimate"} or _looks_like_cost_quote(reply):
        lead = (reply or "").strip()
        # Drop leftover invented dollar/hour sentences from the model.
        cleaned: list[str] = []
        for para in re.split(r"\n{2,}", lead):
            if re.search(r"\$\s*\d", para) or re.search(
                r"\b(\d+\s*[-–]\s*\d+|\d+)\s*hours?\b", para, re.I
            ):
                continue
            if "just type start" in para.lower() and not pricing.get(
                "can_start_requested"
            ):
                continue
            cleaned.append(para.strip())
        intro = "\n\n".join(p for p in cleaned if p).strip()
        if intro:
            return f"{intro}\n\n{block}"
        return block
    return reply
