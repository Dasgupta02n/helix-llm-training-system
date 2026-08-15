"""Spend-cap math uses the high end of the per-row bands."""

from __future__ import annotations

from types import SimpleNamespace

from helix.services.cost_tracking import (
    GOLD_COST_CAP_USD_PER_1000,
    apify_cost_from_run,
    estimate_token_cost_usd,
    gold_spend_cap_usd,
    openrouter_cost_from_usage,
    should_pause_for_spend_cap,
)


def test_gold_spend_cap_scales_to_1000():
    assert gold_spend_cap_usd(1000) == GOLD_COST_CAP_USD_PER_1000
    assert abs(gold_spend_cap_usd(10) - 10.0) < 1e-9
    assert abs(gold_spend_cap_usd(5) - 5.0) < 1e-9
    assert gold_spend_cap_usd(0) == 0.0
    assert gold_spend_cap_usd(10, no_corpus=True) == 30.0
    assert gold_spend_cap_usd(10, kind="synthetic") == 2.0


def test_openrouter_prefers_provider_cost_over_estimate():
    usage = SimpleNamespace(
        prompt_tokens=10_000,
        completion_tokens=2_000,
        cost=0.0123,
    )
    amt, src = openrouter_cost_from_usage(usage, model="x-ai/grok-4.5")
    assert src == "provider"
    assert abs(amt - 0.0123) < 1e-12


def test_openrouter_estimate_lower_than_old_2_6_formula():
    # Old formula: (prompt*2 + completion*6)/1e6
    prompt, completion = 100_000, 20_000
    old = (prompt * 2.0 + completion * 6.0) / 1_000_000
    new = estimate_token_cost_usd(prompt, completion, model="x-ai/grok-4.5")
    assert new < old
    # New should still be positive and order-of-magnitude similar
    assert new > 0
    assert new < old * 0.95


def test_openrouter_dict_usage():
    amt, src = openrouter_cost_from_usage(
        {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.001},
        model="x-ai/grok-4.5",
    )
    assert src == "provider"
    assert abs(amt - 0.001) < 1e-12


def test_apify_usage_total_usd():
    amt, src = apify_cost_from_run(
        {
            "id": "run1",
            "usageTotalUsd": 0.042,
            "usageUsd": {"ACTOR_COMPUTE_UNITS": 0.01},
        }
    )
    assert src == "provider"
    assert abs(amt - 0.042) < 1e-12


def test_apify_sums_usage_usd_when_total_missing():
    amt, src = apify_cost_from_run(
        {"usageUsd": {"ACTOR_COMPUTE_UNITS": 0.01, "DATASET_READS": 0.005}}
    )
    assert src == "provider"
    assert abs(amt - 0.015) < 1e-12


def test_spend_cap_hard_stop_when_over_cap():
    pause, msg = should_pause_for_spend_cap(
        cost_usd=11.0,
        gold_new=2,
        target_gold=10,  # cap $10
        completed_batches=1,
        total_batches=1,
    )
    assert pause
    assert "cap" in msg.lower()


def test_spend_cap_trajectory_per_gold():
    # $3 for 2 gold → $1.50/gold → 10 gold would be $15 > $10 cap
    pause, msg = should_pause_for_spend_cap(
        cost_usd=3.0,
        gold_new=2,
        target_gold=10,
        completed_batches=1,
        total_batches=2,
    )
    assert pause
    assert "trajectory" in msg.lower()


def test_spend_cap_allows_efficient_run():
    # $0.02 for 5 gold → $0.004/gold × 10 = $0.04 < $10 cap
    pause, msg = should_pause_for_spend_cap(
        cost_usd=0.02,
        gold_new=5,
        target_gold=10,
        completed_batches=1,
        total_batches=1,
    )
    assert not pause
    assert msg == ""


def test_spend_cap_batch_trajectory_without_gold():
    # 1 batch spent $8, 2 batches planned, target 10 → cap $10
    # projected $16 > $10
    pause, msg = should_pause_for_spend_cap(
        cost_usd=8.0,
        gold_new=0,
        target_gold=10,
        completed_batches=1,
        total_batches=2,
    )
    assert pause
    assert "batch" in msg.lower() or "trajectory" in msg.lower()
