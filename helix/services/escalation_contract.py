"""First-class typed escalation contracts validated at write time."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator


AnswerType = Literal[
    "free_text",
    "choice",
    "confirm",
    "acknowledge",
    "structured_fact",
    "none",
]


class EscalationContract(BaseModel):
    """Validated shape stored inside Escalation.payload_json (merged with extras)."""

    kind: str = Field(..., min_length=1, max_length=80)
    question: str = Field(..., min_length=1, max_length=2000)
    expected_answer_type: AnswerType = "free_text"
    options: list[str] = Field(default_factory=list)
    # Optional UX helpers
    action_label: str | None = None
    needs_input: bool = True
    context: str | None = None
    # Free-form extras allowed but not required
    fact_text: str | None = None
    candidate_id: str | None = None
    confidence: float | None = None

    @field_validator("options")
    @classmethod
    def options_for_choice(cls, v: list[str], info):  # type: ignore[no-untyped-def]
        return [str(x) for x in (v or []) if str(x).strip()]

    def model_post_init(self, __context: Any) -> None:
        if self.expected_answer_type == "choice" and len(self.options) < 2:
            raise ValueError("choice escalations require at least 2 options")
        if self.expected_answer_type in {"acknowledge", "confirm", "none"}:
            self.needs_input = self.expected_answer_type == "confirm"
        if not self.action_label:
            self.action_label = {
                "free_text": "Save answer",
                "choice": "Choose option",
                "confirm": "Confirm",
                "acknowledge": "Acknowledge",
                "structured_fact": "Save fact",
                "none": "OK",
            }.get(self.expected_answer_type, "Respond")


# Kind → default contract fields (question template may be overridden by caller)
KIND_DEFAULTS: dict[str, dict[str, Any]] = {
    "ambiguous_match": {
        "question": "Is this the same entity/campaign as the candidate match?",
        "expected_answer_type": "choice",
        "options": ["Same entity — merge", "Different — keep separate", "Not sure"],
        "action_label": "Save decision",
    },
    "low_confidence_fact": {
        "question": "Confirm or correct this low-confidence fact extraction.",
        "expected_answer_type": "structured_fact",
        "options": [],
        "action_label": "Save fact",
        "needs_input": True,
    },
    "low_extraction": {
        "question": "Evidence extraction was thin. Provide a better fact or acknowledge and skip.",
        "expected_answer_type": "free_text",
        "options": [],
        "action_label": "Save note",
    },
    "scope_violation": {
        "question": "This item may be out of scope for the active research brief. Allow or reject?",
        "expected_answer_type": "choice",
        "options": ["Allow once", "Reject", "Update plan scope"],
        "action_label": "Decide",
    },
    "zero_evidence": {
        "question": "No verifiable sources found. Broaden the plan, supply a corpus doc, or acknowledge.",
        "expected_answer_type": "acknowledge",
        "options": [],
        "action_label": "Acknowledge",
        "needs_input": False,
    },
    "undersized_split": {
        "question": "A train/test split is undersized. Acknowledge or adjust targets.",
        "expected_answer_type": "acknowledge",
        "options": [],
        "action_label": "Acknowledge",
        "needs_input": False,
    },
    "budget": {
        "question": "Workspace budget pressure detected. Acknowledge or raise budget.",
        "expected_answer_type": "acknowledge",
        "options": [],
        "action_label": "Acknowledge",
        "needs_input": False,
    },
    "generic": {
        "question": "Human input needed.",
        "expected_answer_type": "free_text",
        "options": [],
        "action_label": "Respond",
    },
}


def build_escalation_payload(
    kind: str,
    *,
    message: str = "",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Merge kind defaults + caller payload into a validated EscalationContract dict.
    Raises ValueError if invalid.
    """
    base = dict(KIND_DEFAULTS.get(kind) or KIND_DEFAULTS["generic"])
    extra = dict(payload or {})
    # Prefer explicit question from payload or message
    question = (
        extra.pop("question", None)
        or message
        or base.get("question")
        or "Human input needed."
    )
    data = {
        **base,
        **extra,
        "kind": kind or "generic",
        "question": str(question)[:2000],
    }
    # Legacy keys mapped into contract
    if "needs_input" in extra:
        data["needs_input"] = bool(extra["needs_input"])
    if "action_label" in extra:
        data["action_label"] = extra["action_label"]
    if "options" in extra and extra["options"] is not None:
        data["options"] = list(extra["options"])
    if "expected_answer_type" in extra:
        data["expected_answer_type"] = extra["expected_answer_type"]
    # Keep original message for UI
    data["message"] = message or data["question"]
    try:
        contract = EscalationContract.model_validate(data)
    except ValidationError as e:
        raise ValueError(f"Invalid escalation contract: {e}") from e
    out = contract.model_dump()
    # Preserve any unknown extra keys (fact ids, etc.)
    for k, v in extra.items():
        if k not in out:
            out[k] = v
    out["message"] = message or out["question"]
    out["contract_version"] = 1
    return out
