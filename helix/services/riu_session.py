"""Riu session store — create/load conversational state.

Public callers should import from helix.services.riu (facade).
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from helix.config import get_settings
from helix.db import models as m


RIU_NAME = "Riu"

DEFAULT_SCHEMA = {
    "type": "object",
    "required": ["input", "output", "difficulty"],
    "properties": {
        "input": {"type": "string", "description": "Model input / user brief"},
        "output": {"type": "string", "description": "Ideal model output"},
        "rationale": {"type": "string", "description": "Why this output is correct"},
        "difficulty": {
            "type": "string",
            "enum": ["canonical", "moderate", "edge-case"],
        },
        "is_negative": {
            "type": "boolean",
            "description": "True if this is a negative/counterfactual example",
        },
    },
}

SYSTEM_PROMPT = """You are Riu, a warm, clear conversational guide inside Helix (gold LLM training-data studio).

Your job:
1) Ask plain-English questions (one or two at a time).
2) Understand the ROLE / TASK the AI will perform.
3) Collect one perfect example, then the required number of edge cases.
4) Save Helix configuration. Never invent scraped web/data.

Tone: friendly, non-technical, short paragraphs. Avoid jargon (say "training examples" not "SFT dataset").

Conversation phases (strict order):
greet → role → discover → example → edge_cases → own_data → materials → model_estimate → confirm → running → offer_synth → done

1) role: What job will this AI do? (e.g. screen CVs, write captions, handle refunds)
2) discover: Understand the product/domain in depth.
3) example: Collect ONE perfect input + ideal output.
4) edge_cases: Ask for the required number of TRICKY/outlier scenarios
   (high-risk roles: 3; medium: 2; low: 1). Do not skip this.
5) own_data / materials: labeled zip then unlabeled materials (or skip).
6) model_estimate: Offer Apache-2.0 / MIT instruct models up to 30B (never Llama).
   Recommend one default from the catalog; let the user pick another. QLoRA only.
   Training compute is always pay-per-run GPU (idle when unused). Never offer an
   always-on GPU machine — those can sit on and keep billing.
   DO NOT invent dollar amounts — the server attaches official per-row rates.
7) confirm → start_pipeline only after confirm (or start 10 if no corpus).
8) offer_synth: ONLY after mining finishes (or after no-source scale).
   Ask if they want variations. Never emit start_synthesis during confirm.
9) After gold exists: download from My data, generate synthetics (stored separately),
   or train with Double Helix (start_double_helix_train only after confirm train).
   Synthetics join training only if they say confirm train with synthetics.
10) No corpus + target > 10: start 10, then review_seed (each gold, each parameter),
    then 10 proof, then confirm scale at ~$2–$3 per gold row. Do not skip the review.

Risk: hiring/credit/medical/legal = high (stricter fairness, more edge cases).
Captions/copy = low. Support/sales/HR = medium.

You MUST reply with ONLY a JSON object (no markdown fences) of this shape:
{
  "reply": "What you say to the user in plain English",
  "phase": "greet|role|discover|example|edge_cases|own_data|materials|model_estimate|confirm|running|offer_synth|done",
  "state_patch": {
    "project_name": "...",
    "domain": "...",
    "mission": "...",
    "research_questions": ["..."],
    "categories": ["..."],
    "sources": ["..."],
    "phase_targets": {"billing": 40},
    "topic_key": "support_reply",
    "format_name": "Support reply",
    "format_description": "...",
    "sample_input": "...",
    "sample_output": "...",
    "sample_rationale": "...",
    "gold_target": 10,
    "variations_per_gold": 4,
    "quality_mode": 2,
    "batch_size": 5,
    "total_batches": 2,
    "run_synthesis": false,
    "role_text": "...",
    "risk_level": "low|medium|high",
    "role_type": "...",
    "edge_cases": [],
    "edge_cases_required": 2,
    "recommended_base_model": "Qwen/Qwen2.5-7B-Instruct",
    "has_own_data": false,
    "own_data_awaiting_upload": false,
    "has_materials": false,
    "materials_awaiting_upload": false,
    "notes": "..."
  },
  "actions": [
    // optional, only when ready to apply config or run:
    // {"type": "save_plan"},
    // {"type": "save_format"},
    // {"type": "save_goals"},
    // {"type": "start_pipeline"},
    // {"type": "start_synthesis"}
  ],
  "progress": 0
}

Rules for actions:
- Emit save_plan only when you have project_name + domain + mission (categories helpful).
- Emit save_format when you have a sample input and sample output (or enough to invent a sensible sample from their domain — mark sample as illustrative).
- Emit save_goals when gold_target / variations known. gold_target is a library
  goal, not a promise this job will produce that many. First job is batch_size
  × total_batches (default 5×2=10).
- Never invent costs. Official gold is ~$0.75–$1/row with sources, ~$2–$3/row without. Synthetics ~$0.04–$0.20/row.
- Emit start_pipeline only after the user confirms AND they either have corpus
  or explicitly accepted the 10-example exploratory job ("start 10").
  Do not emit start_pipeline for a 5000-gold promise with zero attached data.
- Emit start_synthesis only if user wants variations and gold goals are set; usually after pipeline is started or they already have gold.
- progress is 0–100 estimate of setup completeness.
- Merge state_patch with prior state; only include keys you want to update.
- progress must be non-decreasing (never go backward) as setup completes.
- When defining a format, set replace_formats: true and a single topic_key —
  do not list old demo formats.
"""


def _uid(prefix: str = "riu_") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_json(raw: str | None, default: Any) -> Any:
    try:
        return json.loads(raw or "") if raw else default
    except json.JSONDecodeError:
        return default


def _topic_key(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return (s or "examples")[:120]


def session_to_dict(row: m.RiuSession) -> dict[str, Any]:
    state = _load_json(row.state_json, {})
    if not isinstance(state, dict):
        state = {}
    phase = row.phase or "greet"
    show_gold_upload = bool(
        state.get("own_data_awaiting_upload")
        or (phase == "own_data" and state.get("has_own_data"))
    )
    show_materials_upload = bool(
        state.get("materials_awaiting_upload")
        or (phase == "materials" and state.get("has_materials"))
    )
    return {
        "id": row.id,
        "status": row.status,
        "phase": phase,
        "state": state,
        "messages": _load_json(row.messages_json, []),
        "last_job_id": row.last_job_id,
        "last_synth_job_id": row.last_synth_job_id,
        "show_gold_zip_upload": show_gold_upload,
        "show_materials_zip_upload": show_materials_upload,
        "own_data_uploaded": bool(state.get("own_data_uploaded")),
        "own_data_count": int(state.get("own_data_count") or 0),
        "materials_uploaded": bool(state.get("materials_uploaded")),
        "materials_count": int(state.get("materials_count") or 0),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "assistant_name": RIU_NAME,
    }


def get_active_session(
    db: Session, *, user_id: str, tenant_id: str
) -> m.RiuSession | None:
    return (
        db.query(m.RiuSession)
        .filter_by(owner_user_id=user_id, tenant_id=tenant_id, status="active")
        .order_by(m.RiuSession.updated_at.desc())
        .first()
    )


def create_session(db: Session, *, user_id: str, tenant_id: str) -> m.RiuSession:
    # abandon previous active
    for old in (
        db.query(m.RiuSession)
        .filter_by(owner_user_id=user_id, tenant_id=tenant_id, status="active")
        .all()
    ):
        old.status = "abandoned"
        old.updated_at = _now()

    greeting = (
        f"Hi — I'm **{RIU_NAME}**, your Helix guide.\n\n"
        "I'll ask a few plain-English questions, set everything up for you, "
        "then start collecting high-quality training examples.\n\n"
        "First: **what AI or product are you training?** "
        "(e.g. a customer support bot, a sales coach, a medical FAQ assistant)"
    )
    messages = [
        {
            "id": _uid("msg_"),
            "role": "assistant",
            "name": RIU_NAME,
            "content": greeting,
            "phase": "greet",
            "actions": [],
            "ts": _now().isoformat(),
        }
    ]
    row = m.RiuSession(
        id=_uid(),
        tenant_id=tenant_id,
        owner_user_id=user_id,
        status="active",
        phase="greet",
        state_json=json.dumps(
            {
                "project_name": "",
                "domain": "",
                "mission": "",
                "research_questions": [],
                "categories": [],
                "sources": ["docs", "web"],
                "phase_targets": {},
                "topic_key": "",
                "format_name": "",
                "format_description": "",
                "sample_input": "",
                "sample_output": "",
                "sample_rationale": "",
                "gold_target": get_settings().default_gold_target_count,
                "variations_per_gold": get_settings().default_variations_per_gold,
                "quality_mode": 2,
                "batch_size": 5,
                "total_batches": 2,
                "run_synthesis": False,
                "notes": "",
            }
        ),
        messages_json=json.dumps(messages),
        updated_at=_now(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_or_create_session(
    db: Session, *, user_id: str, tenant_id: str
) -> m.RiuSession:
    existing = get_active_session(db, user_id=user_id, tenant_id=tenant_id)
    if existing:
        return existing
    return create_session(db, user_id=user_id, tenant_id=tenant_id)


def _merge_state(base: dict, patch: dict | None) -> dict:
    out = dict(base or {})
    if not patch:
        return out
    for k, v in patch.items():
        if v is None:
            continue
        if k in {"research_questions", "categories", "sources"} and isinstance(v, list):
            out[k] = [str(x).strip() for x in v if str(x).strip()]
        elif k == "phase_targets" and isinstance(v, dict):
            cleaned = {}
            for ck, cv in v.items():
                try:
                    cleaned[str(ck)] = int(cv)
                except (TypeError, ValueError):
                    continue
            if cleaned:
                out[k] = cleaned
        else:
            out[k] = v
    return out


def _extract_json_object(text: str) -> dict | None:
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None
