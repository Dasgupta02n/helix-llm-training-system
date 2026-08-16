"""Riu — plain-English conversational helper that configures Helix and starts jobs.

Implementation is split for maintainability:

    riu_session.py    session CRUD, merge/parse helpers, SYSTEM_PROMPT
    riu_estimate.py   official per-row estimate + no-corpus start gate
    riu_actions.py    heuristic/LLM turns, save/start actions, handle_user_message
    riu_seed_review.py  10+10 no-resource review (already separate)

Import from this module so existing ``from helix.services.riu import X`` stays valid.
"""

from helix.services.riu_actions import (
    _apply_save_format,
    _apply_save_goals,
    _apply_save_plan,
    _apply_start_pipeline,
    _apply_start_synthesis,
    _heuristic_turn,
    _llm_turn,
    _ready_for_pipeline,
    _refuses_run,
    _user_denied_attached_data,
    _wants_exploratory,
    _wants_mailbox_list,
    _wants_run,
    _wants_send_mail,
    exploratory_job_shape,
    pipeline_quality_mode,
    execute_actions,
    handle_user_message,
)
from helix.services.riu_estimate import (
    _looks_like_cost_quote,
    apply_official_riu_estimate,
    official_estimate_for_state,
    riu_start_block_reason,
)
from helix.services.riu_session import (
    DEFAULT_SCHEMA,
    RIU_NAME,
    SYSTEM_PROMPT,
    _extract_json_object,
    _load_json,
    _merge_state,
    _now,
    _topic_key,
    _uid,
    create_session,
    get_active_session,
    get_or_create_session,
    session_to_dict,
)

__all__ = [
    "DEFAULT_SCHEMA",
    "RIU_NAME",
    "SYSTEM_PROMPT",
    "_apply_save_format",
    "_apply_save_goals",
    "_apply_save_plan",
    "_apply_start_pipeline",
    "_apply_start_synthesis",
    "_extract_json_object",
    "_heuristic_turn",
    "_llm_turn",
    "_load_json",
    "_looks_like_cost_quote",
    "_merge_state",
    "_now",
    "_ready_for_pipeline",
    "_refuses_run",
    "_topic_key",
    "_uid",
    "_user_denied_attached_data",
    "_wants_exploratory",
    "_wants_mailbox_list",
    "_wants_run",
    "_wants_send_mail",
    "exploratory_job_shape",
    "pipeline_quality_mode",
    "apply_official_riu_estimate",
    "create_session",
    "execute_actions",
    "get_active_session",
    "get_or_create_session",
    "handle_user_message",
    "official_estimate_for_state",
    "riu_start_block_reason",
    "session_to_dict",
]
