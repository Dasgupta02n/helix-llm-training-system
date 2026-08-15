"""helix.services.riu stays a facade after the session/estimate/actions split."""

import helix.services.riu as riu
from helix.services import riu_actions, riu_estimate, riu_session


def test_facade_reexports_public_and_test_helpers():
    assert riu.handle_user_message is riu_actions.handle_user_message
    assert riu._heuristic_turn is riu_actions._heuristic_turn
    assert riu._wants_run is riu_actions._wants_run
    assert riu.official_estimate_for_state is riu_estimate.official_estimate_for_state
    assert riu.riu_start_block_reason is riu_estimate.riu_start_block_reason
    assert riu.create_session is riu_session.create_session
    assert riu._load_json is riu_session._load_json
    assert riu._now is riu_session._now
    assert riu._uid is riu_session._uid
    assert riu.RIU_NAME == "Riu"


def test_start_gate_still_blocks_large_no_corpus():
    reason = riu.riu_start_block_reason({"gold_target": 1000, "corpus_docs": 0})
    assert reason
    assert "start 10" in reason.lower()
