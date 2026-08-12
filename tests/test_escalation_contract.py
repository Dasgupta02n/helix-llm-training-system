from helix.services.escalation_contract import build_escalation_payload


def test_ambiguous_match_contract():
    p = build_escalation_payload(
        "ambiguous_match",
        message="Is creator X the same as Y?",
        payload={"candidate_id": "c1"},
    )
    assert p["expected_answer_type"] == "choice"
    assert len(p["options"]) >= 2
    assert p["question"]
    assert p["contract_version"] == 1
    assert p["candidate_id"] == "c1"


def test_low_confidence_fact_contract():
    p = build_escalation_payload(
        "low_confidence_fact",
        message="Is refund window 30 days?",
        payload={"fact_text": "30-day refund for spoiled items"},
    )
    assert p["expected_answer_type"] == "structured_fact"
    assert p["needs_input"] is True
    assert p["action_label"]


def test_invalid_choice_without_options_raises():
    try:
        build_escalation_payload(
            "generic",
            message="pick one",
            payload={"expected_answer_type": "choice", "options": []},
        )
        assert False, "should have raised"
    except ValueError:
        pass
