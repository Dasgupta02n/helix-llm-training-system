"""Accurate multi-provider cost tracking and hard spend-cap for mining jobs.

Hard product target: ~$35 all-in (OpenRouter + Apify) per 1,000 gold examples.
Double Helix training ($15–50/job) is a future phase — cost fields here are
structured so training spend can be added without reworking the meter.
"""

from __future__ import annotations

from typing import Any

# ── Product cost targets ─────────────────────────────────────────────
GOLD_COST_CAP_USD_PER_1000 = 35.0
# Future Double Helix training band (documentation only for now)
DOUBLE_HELIX_TRAINING_COST_MIN_USD = 15.0
DOUBLE_HELIX_TRAINING_COST_MAX_USD = 50.0

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


def gold_spend_cap_usd(target_gold: int) -> float:
    """Hard cap for a job aiming at `target_gold` verified examples."""
    n = max(0, int(target_gold or 0))
    if n <= 0:
        return 0.0
    return round((n / 1000.0) * GOLD_COST_CAP_USD_PER_1000, 6)


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
) -> tuple[bool, str]:
    """
    Hard auto-pause when spend already hits cap, or trajectory would exceed it.

    Trajectory uses cost-per-gold when gold has been produced; otherwise
    cost-per-batch projection across remaining batches.
    """
    cost = max(0.0, float(cost_usd or 0.0))
    target = max(0, int(target_gold or 0))
    cap = gold_spend_cap_usd(target) if target > 0 else 0.0
    if cap <= 0:
        return False, ""

    if cost >= cap:
        return (
            True,
            (
                f"Hard spend cap reached: ${cost:.4f} spent "
                f"(cap ${cap:.4f} for {target} target gold @ "
                f"${GOLD_COST_CAP_USD_PER_1000:.0f}/1k)."
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
                    f"Spend trajectory pause: ${per_gold:.4f}/gold × {target} target "
                    f"= ${projected:.4f} would exceed cap ${cap:.4f} "
                    f"(${GOLD_COST_CAP_USD_PER_1000:.0f}/1k gold)."
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


def record_openrouter_spend(tenant: Any, amount_usd: float) -> None:
    """Attribute OpenRouter spend on tenant (total + split counters)."""
    amt = max(0.0, float(amount_usd or 0.0))
    if amt <= 0 or tenant is None:
        return
    tenant.spent_usd = float(tenant.spent_usd or 0.0) + amt
    if hasattr(tenant, "openrouter_spent_usd"):
        tenant.openrouter_spent_usd = float(tenant.openrouter_spent_usd or 0.0) + amt


def record_apify_spend(tenant: Any, amount_usd: float) -> None:
    """Attribute Apify spend on tenant (total + split counters)."""
    amt = max(0.0, float(amount_usd or 0.0))
    if amt <= 0 or tenant is None:
        return
    tenant.spent_usd = float(tenant.spent_usd or 0.0) + amt
    if hasattr(tenant, "apify_spent_usd"):
        tenant.apify_spent_usd = float(tenant.apify_spent_usd or 0.0) + amt


def tenant_cost_breakdown(tenant: Any) -> dict[str, float]:
    if tenant is None:
        return {
            "openrouter_usd": 0.0,
            "apify_usd": 0.0,
            "spent_usd": 0.0,
            "monthly_usd": 0.0,
        }
    or_u = float(getattr(tenant, "openrouter_spent_usd", 0.0) or 0.0)
    ap_u = float(getattr(tenant, "apify_spent_usd", 0.0) or 0.0)
    total = float(tenant.spent_usd or 0.0)
    # Backfill: older tenants only had spent_usd from OpenRouter estimates
    if or_u <= 0 and ap_u <= 0 and total > 0:
        or_u = total
    return {
        "openrouter_usd": round(or_u, 6),
        "apify_usd": round(ap_u, 6),
        "spent_usd": round(total, 6),
        "monthly_usd": float(tenant.monthly_budget_usd or 0.0),
    }
