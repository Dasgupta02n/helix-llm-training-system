"""Accurate cost tracking and hard spend-cap for mining / synthesis jobs.

Gold with attached sources: about $0.75–$1.00 per row.
Gold with no sources (after 10+10 review): about $2–$3 per row.
Synthetics: about $0.04–$0.20 per row.
Spend caps use the high end of each band.
"""

from __future__ import annotations

from typing import Any, Literal

# ── Product cost targets (USD per row) ───────────────────────────────
GOLD_WITH_RESOURCE_USD_MIN = 0.75
GOLD_WITH_RESOURCE_USD_MAX = 1.00
GOLD_NO_RESOURCE_USD_MIN = 2.00
GOLD_NO_RESOURCE_USD_MAX = 3.00
SYNTH_USD_MIN = 0.04
SYNTH_USD_MAX = 0.20

# High-end rates used as spend-cap scale (also exposed as /1k for older callers).
GOLD_COST_CAP_USD_PER_1000 = GOLD_WITH_RESOURCE_USD_MAX * 1000.0  # 1000
GOLD_COST_NO_CORPUS_USD_PER_1000 = GOLD_NO_RESOURCE_USD_MAX * 1000.0  # 3000
SYNTH_COST_CAP_USD_PER_1000 = SYNTH_USD_MAX * 1000.0  # 200

DOUBLE_HELIX_TRAINING_COST_MIN_USD = 15.0
DOUBLE_HELIX_TRAINING_COST_MAX_USD = 50.0

# User usage = (sum of billed service spend) × this factor.
USER_COST_MARKUP = 2.0

# Fallback token pricing (USD per 1M tokens) when provider omits usage.cost.
# Prefer actual billed cost from OpenRouter / Apify whenever present.
# Grok-class midpoints via OpenRouter (pay-as-you-go list, updated when models change).
MODEL_PRICING_PER_M: dict[str, tuple[float, float]] = {
    # model substring -> (prompt_per_m, completion_per_m)
    "grok-4.5": (1.2, 6.0),
    "grok-4": (1.2, 6.0),
    "grok-3": (1.0, 5.0),
    "grok-2": (2.0, 10.0),
    "default": (1.5, 6.0),
}


Kind = Literal["gold", "synthetic"]


def gold_rate_per_row(*, no_corpus: bool = False, kind: Kind = "gold") -> float:
    """High-end per-row rate used for spend caps."""
    if kind == "synthetic":
        return SYNTH_USD_MAX
    return GOLD_NO_RESOURCE_USD_MAX if no_corpus else GOLD_WITH_RESOURCE_USD_MAX


def gold_rate_band(*, no_corpus: bool = False, kind: Kind = "gold") -> tuple[float, float]:
    if kind == "synthetic":
        return SYNTH_USD_MIN, SYNTH_USD_MAX
    if no_corpus:
        return GOLD_NO_RESOURCE_USD_MIN, GOLD_NO_RESOURCE_USD_MAX
    return GOLD_WITH_RESOURCE_USD_MIN, GOLD_WITH_RESOURCE_USD_MAX


def format_row_rate(*, no_corpus: bool = False, kind: Kind = "gold") -> str:
    lo, hi = gold_rate_band(no_corpus=no_corpus, kind=kind)
    if kind == "synthetic":
        return f"~${lo:.2f}–${hi:.2f} per synthetic row"
    label = "gold row (no source material)" if no_corpus else "gold row (with your sources)"
    return f"~${lo:.2f}–${hi:.2f} per {label}"


def estimate_units_usd(
    n: int, *, no_corpus: bool = False, kind: Kind = "gold"
) -> tuple[float, float]:
    lo, hi = gold_rate_band(no_corpus=no_corpus, kind=kind)
    units = max(0, int(n or 0))
    return round(units * lo, 2), round(units * hi, 2)


def gold_rate_per_1000(*, no_corpus: bool = False, kind: Kind = "gold") -> float:
    return gold_rate_per_row(no_corpus=no_corpus, kind=kind) * 1000.0


def user_charge_usd(provider_usd: float) -> float:
    """What the usage counter shows: 2 × incurred service spend."""
    return round(max(0.0, float(provider_usd or 0.0)) * USER_COST_MARKUP, 6)


def provider_spend_total(
    *,
    model_usd: float = 0.0,
    gather_usd: float = 0.0,
    compute_usd: float = 0.0,
    other_usd: float = 0.0,
) -> float:
    return round(
        max(0.0, float(model_usd or 0.0))
        + max(0.0, float(gather_usd or 0.0))
        + max(0.0, float(compute_usd or 0.0))
        + max(0.0, float(other_usd or 0.0)),
        6,
    )


def usage_from_provider_parts(
    *,
    model_usd: float = 0.0,
    gather_usd: float = 0.0,
    compute_usd: float = 0.0,
    other_usd: float = 0.0,
) -> dict[str, float]:
    provider = provider_spend_total(
        model_usd=model_usd,
        gather_usd=gather_usd,
        compute_usd=compute_usd,
        other_usd=other_usd,
    )
    user = user_charge_usd(provider)
    return {
        "provider_usd": provider,
        "user_charge_usd": user,
        "markup": USER_COST_MARKUP,
        "model_usd": round(max(0.0, float(model_usd or 0.0)), 6),
        "gather_usd": round(max(0.0, float(gather_usd or 0.0)), 6),
        "compute_usd": round(max(0.0, float(compute_usd or 0.0)), 6),
        "other_usd": round(max(0.0, float(other_usd or 0.0)), 6),
    }


def gold_spend_cap_usd(
    target_gold: int,
    *,
    usd_per_1000: float | None = None,
    no_corpus: bool = False,
    kind: Kind = "gold",
) -> float:
    """Hard cap for a job aiming at `target_gold` units (high end of the band)."""
    n = max(0, int(target_gold or 0))
    if n <= 0:
        return 0.0
    if usd_per_1000 is not None:
        rate = float(usd_per_1000) / 1000.0
    else:
        rate = gold_rate_per_row(no_corpus=no_corpus, kind=kind)
    return round(n * rate, 6)


def _as_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _pricing_for_model(model: str | None) -> tuple[float, float]:
    m = (model or "").lower()
    for key, pair in MODEL_PRICING_PER_M.items():
        if key != "default" and key in m:
            return pair
    return MODEL_PRICING_PER_M["default"]


def estimate_token_cost_usd(
    prompt_tokens: int,
    completion_tokens: int,
    *,
    model: str | None = None,
) -> float:
    """Token-based estimate only — used when provider does not return actual cost."""
    p_rate, c_rate = _pricing_for_model(model)
    return (
        max(0, int(prompt_tokens or 0)) * p_rate
        + max(0, int(completion_tokens or 0)) * c_rate
    ) / 1_000_000.0


def openrouter_cost_from_usage(
    usage: Any,
    *,
    model: str | None = None,
) -> tuple[float, str]:
    """
    Extract billed OpenRouter cost from a chat completion usage object.

    Returns (usd, source) where source is:
      - "provider" when usage.cost (or equivalent) is present
      - "estimate" when falling back to token rates
    """
    if usage is None:
        return 0.0, "none"

    # OpenRouter returns actual account charge on usage.cost (USD).
    for attr in ("cost", "total_cost", "total_cost_usd"):
        raw = getattr(usage, attr, None)
        if raw is None and isinstance(usage, dict):
            raw = usage.get(attr)
        f = _as_float(raw)
        if f is not None and f >= 0:
            return float(f), "provider"

    # Nested cost_details (prefer total over upstream-only if present)
    details = getattr(usage, "cost_details", None)
    if details is None and isinstance(usage, dict):
        details = usage.get("cost_details")
    if details is not None:
        for attr in ("total_cost", "cost", "upstream_inference_cost"):
            raw = getattr(details, attr, None)
            if raw is None and isinstance(details, dict):
                raw = details.get(attr)
            f = _as_float(raw)
            # upstream_inference_cost can be in different units for BYOK — skip if huge vs tokens
            if f is not None and f >= 0 and attr != "upstream_inference_cost":
                return float(f), "provider"

    prompt = getattr(usage, "prompt_tokens", None)
    if prompt is None and isinstance(usage, dict):
        prompt = usage.get("prompt_tokens")
    completion = getattr(usage, "completion_tokens", None)
    if completion is None and isinstance(usage, dict):
        completion = usage.get("completion_tokens")
    est = estimate_token_cost_usd(
        int(prompt or 0), int(completion or 0), model=model
    )
    return est, "estimate"


def apify_cost_from_run(run: dict[str, Any] | None) -> tuple[float, str]:
    """
    Extract billed Apify cost from an actor run object.

    Prefers usageTotalUsd (official billed total).
    """
    if not run or not isinstance(run, dict):
        return 0.0, "none"
    for key in ("usageTotalUsd", "usage_total_usd"):
        f = _as_float(run.get(key))
        if f is not None and f >= 0:
            return float(f), "provider"
    usage_usd = run.get("usageUsd") or run.get("usage_usd")
    if isinstance(usage_usd, dict):
        total = 0.0
        any_val = False
        for v in usage_usd.values():
            fv = _as_float(v)
            if fv is not None:
                total += fv
                any_val = True
        if any_val:
            return total, "provider"
    # stats nested
    stats = run.get("stats") if isinstance(run.get("stats"), dict) else {}
    f = _as_float(stats.get("usageTotalUsd") if stats else None)
    if f is not None and f >= 0:
        return float(f), "provider"
    return 0.0, "none"


def should_pause_for_spend_cap(
    *,
    cost_usd: float,
    gold_new: int,
    target_gold: int,
    completed_batches: int,
    total_batches: int,
    spend_cap_usd: float | None = None,
    no_corpus: bool = False,
    kind: Kind = "gold",
) -> tuple[bool, str]:
    """
    Hard auto-pause when spend already hits cap, or trajectory would exceed it.

    Trajectory uses cost-per-gold when gold has been produced; otherwise
    cost-per-batch projection across remaining batches.
    """
    cost = max(0.0, float(cost_usd or 0.0))
    target = max(0, int(target_gold or 0))
    cap = (
        float(spend_cap_usd)
        if spend_cap_usd is not None
        else (gold_spend_cap_usd(target, no_corpus=no_corpus, kind=kind) if target > 0 else 0.0)
    )
    if cap <= 0:
        return False, ""
    rate_note = format_row_rate(no_corpus=no_corpus, kind=kind)

    if cost >= cap:
        return (
            True,
            (
                f"Hard spend cap reached: ${cost:.4f} spent "
                f"(cap ${cap:.4f} for {target} target units, {rate_note})."
            ),
        )

    gold = max(0, int(gold_new or 0))
    if gold >= 1:
        per_gold = cost / gold
        projected = per_gold * target
        if projected > cap:
            return (
                True,
                (
                    f"Spend trajectory pause: ${per_gold:.4f}/row × {target} target "
                    f"= ${projected:.4f} would exceed cap ${cap:.4f} "
                    f"({rate_note})."
                ),
            )

    done = max(0, int(completed_batches or 0))
    total = max(1, int(total_batches or 1))
    if done >= 1 and total > done:
        projected = (cost / done) * total
        if projected > cap:
            return (
                True,
                (
                    f"Spend trajectory pause: ${cost / done:.4f}/batch × {total} batches "
                    f"= ${projected:.4f} would exceed cap ${cap:.4f} "
                    f"for {target} target gold."
                ),
            )

    return False, ""


def _add_provider(tenant: Any, field: str, amount_usd: float) -> float:
    amt = max(0.0, float(amount_usd or 0.0))
    if amt <= 0 or tenant is None:
        return 0.0
    if hasattr(tenant, field):
        setattr(tenant, field, float(getattr(tenant, field) or 0.0) + amt)
    return amt


def record_openrouter_spend(tenant: Any, amount_usd: float) -> None:
    """Record billed model spend (provider dollars). Usage counter is 2×."""
    amt = _add_provider(tenant, "openrouter_spent_usd", amount_usd)
    if amt and tenant is not None:
        tenant.spent_usd = float(tenant.spent_usd or 0.0) + user_charge_usd(amt)


def record_apify_spend(tenant: Any, amount_usd: float) -> None:
    """Record billed gather spend (provider dollars). Usage counter is 2×."""
    amt = _add_provider(tenant, "apify_spent_usd", amount_usd)
    if amt and tenant is not None:
        tenant.spent_usd = float(tenant.spent_usd or 0.0) + user_charge_usd(amt)


def record_compute_spend(tenant: Any, amount_usd: float) -> None:
    """Record billed training-compute spend (provider dollars). Usage counter is 2×."""
    amt = _add_provider(tenant, "compute_spent_usd", amount_usd)
    if amt and tenant is not None:
        tenant.spent_usd = float(tenant.spent_usd or 0.0) + user_charge_usd(amt)


def record_other_spend(tenant: Any, amount_usd: float) -> None:
    """Record any other billed service spend (storage, host, etc.)."""
    amt = _add_provider(tenant, "other_spent_usd", amount_usd)
    if amt and tenant is not None:
        tenant.spent_usd = float(tenant.spent_usd or 0.0) + user_charge_usd(amt)


def tenant_over_budget(tenant: Any) -> bool:
    if tenant is None:
        return False
    b = tenant_cost_breakdown(tenant)
    return b["spent_usd"] >= float(b["monthly_usd"] or 0.0) > 0


def tenant_cost_breakdown(tenant: Any) -> dict[str, float]:
    if tenant is None:
        return {
            "openrouter_usd": 0.0,
            "apify_usd": 0.0,
            "compute_usd": 0.0,
            "other_usd": 0.0,
            "provider_usd": 0.0,
            "spent_usd": 0.0,
            "user_charge_usd": 0.0,
            "markup": USER_COST_MARKUP,
            "monthly_usd": 0.0,
        }
    or_u = float(getattr(tenant, "openrouter_spent_usd", 0.0) or 0.0)
    ap_u = float(getattr(tenant, "apify_spent_usd", 0.0) or 0.0)
    cp_u = float(getattr(tenant, "compute_spent_usd", 0.0) or 0.0)
    ot_u = float(getattr(tenant, "other_spent_usd", 0.0) or 0.0)
    stored = float(getattr(tenant, "spent_usd", 0.0) or 0.0)
    # Backfill: older tenants only had spent_usd from model estimates
    if or_u <= 0 and ap_u <= 0 and cp_u <= 0 and ot_u <= 0 and stored > 0:
        or_u = stored
    usage = usage_from_provider_parts(
        model_usd=or_u, gather_usd=ap_u, compute_usd=cp_u, other_usd=ot_u
    )
    return {
        "openrouter_usd": usage["model_usd"],
        "apify_usd": usage["gather_usd"],
        "compute_usd": usage["compute_usd"],
        "other_usd": usage["other_usd"],
        "provider_usd": usage["provider_usd"],
        "spent_usd": usage["user_charge_usd"],
        "user_charge_usd": usage["user_charge_usd"],
        "markup": USER_COST_MARKUP,
        "monthly_usd": float(getattr(tenant, "monthly_budget_usd", 0.0) or 0.0),
    }
