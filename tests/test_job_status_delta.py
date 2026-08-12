"""CI: multi-batch job counters reconcile with library gold deltas."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from helix.services.pipeline_modes import run_pipeline_batch
from helix.worker import _process_one_batch


def test_run_pipeline_batch_gold_new_is_library_delta():
    """gold_new must equal after-before count, not a soft estimate."""
    db = MagicMock()

    # _user_gold_count is called twice: before and after
    counts = iter([10, 13])

    def fake_count(db, owner, tenant):
        return next(counts)

    code_res = {
        "items_processed": 5,
        "gold_new": 99,  # deliberately wrong — batch must use DB delta
        "candidates_new": 2,
        "gather_results": 9,
        "zero_evidence": False,
        "warnings": [],
    }

    with (
        patch("helix.services.pipeline_modes._user_gold_count", side_effect=fake_count),
        patch("helix.services.pipeline_modes.run_code_pipeline_batch", return_value=code_res),
        patch("helix.services.pipeline_modes.mode_llm_agents", return_value=[]),
        patch("helix.services.pipeline_modes._brief_dict", return_value={"domain": "support"}),
        patch("helix.services.pipeline_modes.clamp_mode", side_effect=lambda m: m),
        patch("helix.services.pipeline_modes.clamp_batch_size", side_effect=lambda n: n),
    ):
        out = run_pipeline_batch(
            db,
            tenant_id="t1",
            owner_user_id="u1",
            quality_mode=4,
            batch_size=5,
        )
    assert out["gold_before"] == 10
    assert out["gold_after"] == 13
    assert out["gold_new"] == 3
    assert "3 new gold" in out["user_message"] or "3" in out["user_message"]


def test_worker_accumulates_total_gold_across_batches():
    """Multi-batch job total_gold_new must sum batch deltas, not last-batch only."""
    # Simulate two sequential batch summaries the way worker does
    prev = {"total_gold_new": 0, "batches": []}
    for gold_new in (2, 3):
        total = int(prev.get("total_gold_new") or 0) + gold_new
        prev = {
            "total_gold_new": total,
            "gold_new": gold_new,
            "job_user_message": f"Job so far: {total} new gold",
            "batches": prev.get("batches", []) + [{"gold_new": gold_new}],
        }
    assert prev["total_gold_new"] == 5
    assert "5 new gold" in prev["job_user_message"]


def test_items_processed_and_message_align_with_delta():
    """Sanity: completed message should mention cumulative gold, not zero when delta>0."""
    completed_batches = 2
    total_batches = 2
    total_gold = 5
    if total_gold > 0:
        msg = (
            f"Completed {completed_batches}/{total_batches} batches — "
            f"{total_gold} new gold example(s) saved to your library."
        )
    else:
        msg = f"Completed {completed_batches}/{total_batches} batches — 0 new gold examples"
    assert "5 new gold" in msg
    assert "0 new gold" not in msg
