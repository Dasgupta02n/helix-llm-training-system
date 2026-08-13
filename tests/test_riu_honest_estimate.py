"""Riu must use job-system $35/1k + corpus gate — never invent $47-65 / 3-6 hours."""

from helix.services.riu import (
    apply_official_riu_estimate,
    official_estimate_for_state,
    riu_start_block_reason,
    _wants_exploratory,
    _user_denied_attached_data,
)
from helix.services.user_material_upload import estimate_setup_pricing


def test_official_rate_is_35_per_1000_not_invented_band():
    p = official_estimate_for_state(
        {
            "gold_target": 5000,
            "batch_size": 5,
            "total_batches": 2,
            "corpus_docs": 0,
            "corpus_units": 0,
            "attached_support": 0,
            "own_data_count": 0,
            "materials_count": 0,
        }
    )
    assert p["cost_per_1000_gold_usd"] == 35.0
    assert p["mining_target_all_in_usd"] == 175.0
    assert p["first_job_units"] == 10
    assert abs(p["first_job_unit_cap_usd"] - 0.35) < 1e-9
    assert p["can_start_requested"] is False
    blob = " ".join(p["summary_lines"]).lower()
    assert "$35" in blob or "35 per" in blob
    assert "175" in blob
    assert "47" not in blob and "65" not in blob


def test_apply_official_replaces_invented_quote():
    fake = (
        "Cost & time estimate for 5000 gold examples: approximately $47-65 "
        "in total credits, finishes in 3-6 hours... just type start and "
        "I'll launch everything."
    )
    state = {
        "gold_target": 5000,
        "batch_size": 5,
        "total_batches": 2,
        "corpus_docs": 0,
        "corpus_units": 0,
        "attached_support": 0,
        "project_name": "Credit Card Sales Pro",
    }
    out = apply_official_riu_estimate(fake, phase="confirm", state=state)
    low = out.lower()
    assert "$47" not in out and "$65" not in out
    assert "approximately $47" not in out
    assert "finishes in 3-6 hours" not in low
    assert "35" in out
    assert "175" in out
    assert "start 10" in low or "exploratory" in low
    assert "just type start and" not in low


def test_start_5000_without_corpus_is_blocked():
    reason = riu_start_block_reason(
        {
            "gold_target": 5000,
            "batch_size": 5,
            "total_batches": 2,
            "corpus_docs": 0,
            "attached_support": 0,
        }
    )
    assert reason
    assert "5000" in reason or "5,000" in reason
    assert "start 10" in reason.lower()
    assert "35" in reason


def test_start_10_exploratory_is_allowed():
    assert _wants_exploratory("start 10")
    reason = riu_start_block_reason(
        {
            "gold_target": 5000,
            "batch_size": 5,
            "total_batches": 2,
            "corpus_docs": 0,
            "accept_exploratory": True,
        }
    )
    assert reason is None


def test_user_denied_corpus_cues():
    assert _user_denied_attached_data("I have zero corpus, zero documents, web research only")
    assert _user_denied_attached_data("no labeled data")
    assert not _user_denied_attached_data("I have a handbook zip")


def test_estimate_setup_flags_cannot_start_5000_empty():
    p = estimate_setup_pricing(
        {
            "gold_target": 5000,
            "corpus_docs": 0,
            "attached_support": 0,
            "own_data_count": 0,
            "materials_count": 0,
        }
    )
    assert p["can_start_requested"] is False
    assert p["requested_exceeds_corpus"] is True
