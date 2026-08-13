"""End-to-end heuristic walkthrough of the P2 Riu-only setup."""

from helix.services.riu import _heuristic_turn, riu_start_block_reason


def test_full_riu_setup_hits_edges_and_corpus_gate():
    state: dict = {}
    t = _heuristic_turn("Screen CVs and rank applicants", state, "greet")
    state.update(t["state_patch"])
    assert t["phase"] == "discover"
    assert state["risk_level"] == "high"
    assert state["edge_cases_required"] == 3

    t = _heuristic_turn("Rank applicants fairly for an ATS", state, t["phase"])
    state.update(t["state_patch"])
    t = _heuristic_turn("skills, experience, culture", state, t["phase"])
    state.update(t["state_patch"])
    assert t["phase"] == "example"

    t = _heuristic_turn(
        "Score this CV for a backend role\nAsk a human reviewer; do not auto-reject.",
        state,
        t["phase"],
    )
    state.update(t["state_patch"])
    assert t["phase"] == "edge_cases"
    assert "3" in t["reply"]

    for msg in (
        "Applicant with a 5-year gap",
        "Two candidates with identical skills, different ages",
        "Missing education section entirely",
    ):
        t = _heuristic_turn(msg, state, "edge_cases")
        state.update(t["state_patch"])
    assert t["phase"] == "own_data"
    assert len(state["edge_cases"]) >= 3

    t = _heuristic_turn("no", state, "own_data")
    state.update(t["state_patch"])
    t = _heuristic_turn("skip", state, t["phase"])
    state.update(t["state_patch"])
    assert t["phase"] in {"model_estimate", "confirm"}
    assert "llama" in t["reply"].lower() or "qlora" in t["reply"].lower() or "35" in t["reply"]

    state["gold_target"] = 5000
    state["corpus_docs"] = 0
    assert riu_start_block_reason(state)
    state["accept_exploratory"] = True
    assert riu_start_block_reason(state) is None
