"""No-resource 10-review → 10-proof → scale, and synth-in-train flags."""

from helix.services.cost_tracking import (
    GOLD_COST_NO_CORPUS_USD_PER_1000,
    GOLD_NO_RESOURCE_USD_MAX,
    gold_spend_cap_usd,
)
from helix.services.riu import riu_start_block_reason
from helix.services.riu_seed_review import (
    REVIEW_PARAMS,
    apply_review_reply,
    review_state,
    scale_batch_plan,
    wants_no_resource_scale,
)


def test_no_resource_wants_scale():
    assert wants_no_resource_scale(
        {"gold_target": 1000, "corpus_docs": 0, "attached_support": 0}
    )
    assert not wants_no_resource_scale(
        {"gold_target": 10, "corpus_docs": 0, "attached_support": 0}
    )
    assert not wants_no_resource_scale(
        {"gold_target": 1000, "corpus_docs": 2, "attached_support": 40}
    )


def test_scale_allowed_after_review():
    assert riu_start_block_reason(
        {"gold_target": 1000, "corpus_docs": 0, "seed_scale_ready": True}
    ) is None
    assert riu_start_block_reason(
        {"gold_target": 1000, "corpus_docs": 0}
    )


def test_no_corpus_rate_is_higher():
    assert GOLD_COST_NO_CORPUS_USD_PER_1000 == GOLD_NO_RESOURCE_USD_MAX * 1000
    assert gold_spend_cap_usd(1000, no_corpus=True) == 3000.0
    assert gold_spend_cap_usd(1000, no_corpus=False) == 1000.0


def test_scale_plan_fits_job_caps():
    bsize, batches = scale_batch_plan({"gold_target": 1000})
    assert bsize * batches >= 980
    assert batches <= 100
    bsize5, batches5 = scale_batch_plan({"gold_target": 5000})
    assert bsize5 * batches5 >= 4900
    assert batches5 <= 100


def test_review_walks_params_then_finishes(monkeypatch):
    class FakeGold:
        def __init__(self, i):
            self.id = f"g{i}"
            self.input_text = f"Q{i}"
            self.output_text = f"A{i}"

    golds = [FakeGold(i) for i in range(2)]

    class FakeQuery:
        def filter_by(self, **k):
            return self

        def first(self):
            return golds[0]

    class FakeDb:
        def query(self, *_a, **_k):
            return FakeQuery()

    state = {
        "gold_target": 1000,
        "seed_review": {
            "gold_ids": ["g0", "g1"],
            "index": 0,
            "param_index": 0,
            "notes": {},
        },
    }
    db = FakeDb()
    # approve first gold entirely
    turn = apply_review_reply(db, state=state, text="this gold is good")
    assert turn["phase"] == "review_seed"
    assert review_state(state)["index"] == 1
    # walk remaining gold param-by-param
    for _ in REVIEW_PARAMS:
        turn = apply_review_reply(db, state=state, text="ok keep this")
    assert turn["phase"] == "proof_wait"
    assert any(a.get("type") == "start_proof_batch" for a in turn["actions"])
    assert "2" in turn["reply"] or "per gold" in turn["reply"].lower()
