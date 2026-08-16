"""Riu must use official per-row rates + corpus gate — never invent $47-65 / 3-6 hours."""

from helix.services.riu import (
    apply_official_riu_estimate,
    official_estimate_for_state,
    riu_start_block_reason,
    _heuristic_turn,
    _wants_exploratory,
    _wants_run,
    _user_denied_attached_data,
    exploratory_job_shape,
    pipeline_quality_mode,
)
from helix.services.riu_actions import _apply_count_cues, _should_skip_llm
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
    assert p["cost_per_1000_gold_usd"] == 1000.0
    assert p["mining_target_all_in_usd"] == 5000.0
    assert p["first_job_units"] == 10
    assert abs(p["first_job_unit_cap_usd"] - 30.0) < 1e-9
    assert p["can_start_requested"] is False
    blob = " ".join(p["summary_lines"]).lower()
    assert "0.75" in blob or "per gold" in blob or "per row" in blob
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
    assert "0.75" in out or "per gold" in out.lower() or "per row" in out.lower()
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
    assert "0.75" in reason or "2" in reason or "per gold" in reason.lower()


def test_start_10_is_a_run_confirm():
    assert _wants_exploratory("start 10")
    assert _wants_run("start 10")
    assert _wants_run("start small")
    assert _wants_run("start")
    assert not _wants_run("restart")
    assert not _wants_run("start over")
    assert not _wants_exploratory("start 5000")


def test_heuristic_confirm_start_10_emits_pipeline():
    turn = _heuristic_turn(
        "start 10",
        {
            "project_name": "X",
            "mission": "m",
            "categories": ["a"],
            "sample_input": "q",
            "sample_output": "a",
            "accept_exploratory": True,
        },
        "confirm",
    )
    assert any(a.get("type") == "start_pipeline" for a in turn["actions"])
    assert turn["phase"] == "running"


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


def test_exploratory_job_honors_five_gold():
    assert exploratory_job_shape({"gold_target": 5}) == (5, 1)
    assert exploratory_job_shape({"gold_target": 10}) == (10, 1)
    assert exploratory_job_shape({"gold_target": 5000}) == (10, 1)
    assert exploratory_job_shape({}) == (10, 1)


def test_cheap_small_jobs_use_mode_3():
    assert pipeline_quality_mode({"gold_target": 5, "accept_exploratory": True}) == 3
    assert pipeline_quality_mode({"cheap_test": True, "quality_mode": 2}) == 3
    assert pipeline_quality_mode({"quality_mode": 1, "gold_target": 5000}) == 1
    assert pipeline_quality_mode({"quality_mode": 4}) == 4


def test_count_cues_override_default_5000():
    state: dict = {"gold_target": 5000, "quality_mode": 2}
    _apply_count_cues(
        "I only need 5 gold examples, then 20 synthetics. Cheap test.",
        state,
    )
    assert state["gold_target"] == 5
    assert state["variations_per_gold"] == 4
    assert state["cheap_test"] is True
    assert state["quality_mode"] == 3


def test_start_and_skip_do_not_call_for_llm():
    assert _should_skip_llm("start 10", "confirm")
    assert _should_skip_llm("no corpus — web research only", "own_data")
    assert _should_skip_llm("skip materials", "materials")
    assert not _should_skip_llm("IT helpdesk for a SaaS company", "greet")


def test_running_reply_does_not_append_rate_card():
    out = apply_official_riu_estimate(
        "Mining job queued. Watch Home.",
        phase="running",
        state={"gold_target": 5, "accept_exploratory": True},
    )
    assert "First job" not in out
    assert out.startswith("Mining job queued")


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
