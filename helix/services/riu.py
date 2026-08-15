"""Riu — plain-English conversational helper that configures Helix and starts jobs."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from helix.config import get_settings
from helix.db import models as m
from helix.services.batch_jobs import create_batch_job, job_to_dict
from helix.services.brief import (
    get_active_project,
    project_to_dict,
    schema_to_dict,
    sync_workspace_from_brief,
)
from helix.services.library import update_scope

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
   DO NOT invent dollar amounts — the server attaches the official $35/1k estimate.
7) confirm → start_pipeline only after confirm (or start 10 if no corpus).
8) offer_synth: ONLY after mining finishes. Ask if they want variations.
   Never emit start_synthesis during confirm. User must opt in later.
9) After gold exists, two options: download data from My data, OR train with
   Double Helix (emit start_double_helix_train only after they say confirm train).

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
- Never invent costs. Official rate is $35 / 1,000 gold.
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


def _wants_run(text: str) -> bool:
    """True only for explicit run commands — not 'restart' / 'start over'."""
    t = (text or "").strip().lower()
    if t in {"start", "go", "yes", "y", "run", "launch", "begin"}:
        return True
    # word-boundary phrases (avoid matching "restart")
    patterns = (
        r"\brun\s+it\b",
        r"\bgo\s+ahead\b",
        r"\byes,?\s+run\b",
        r"\byes,?\s+start\b",
        r"\bbegin\s+collecting\b",
        r"\bstart\s+(collecting|mining|now|please)\b",
        r"\bdo\s+it\b",
        r"\blaunch\s+(it|now|job)?\b",
    )
    return any(re.search(p, t) for p in patterns)


def _refuses_run(text: str) -> bool:
    t = (text or "").strip().lower()
    return any(
        p in t
        for p in (
            "not yet",
            "wait",
            "hold on",
            "don't run",
            "do not run",
            "don't start",
            "do not start",
            "not now",
            "later",
        )
    )


def _user_denied_attached_data(text: str) -> bool:
    t = (text or "").lower()
    cues = (
        "no corpus",
        "zero corpus",
        "0 corpus",
        "no documents",
        "zero documents",
        "no labeled",
        "zero labeled",
        "no data",
        "don't have data",
        "do not have data",
        "web research only",
        "web only",
        "pure web",
        "nothing to upload",
        "no files",
        "no source material",
    )
    return any(c in t for c in cues)


def _wants_exploratory(text: str) -> bool:
    t = (text or "").strip().lower()
    if t in {"start 10", "start ten", "exploratory", "start small", "start exploratory"}:
        return True
    return bool(re.search(r"\bstart\s+(10|ten|small|exploratory)\b", t))


def _looks_like_cost_quote(reply: str) -> bool:
    r = reply or ""
    if re.search(r"\$\s*\d", r):
        return True
    low = r.lower()
    return any(
        w in low
        for w in (
            "credit",
            "estimate",
            "per 1,000",
            "per 1000",
            "hours",
            "gold examples",
        )
    )


def riu_start_block_reason(state: dict[str, Any]) -> str | None:
    """Same gate as jobs, but also treat library gold_target as the requested volume."""
    from helix.services.corpus import LARGE_PIPELINE_UNITS

    if state.get("accept_exploratory"):
        return None
    try:
        gold_target = int(state.get("gold_target") or 0)
    except (TypeError, ValueError):
        gold_target = 0
    try:
        units = int(state.get("batch_size") or 5) * int(state.get("total_batches") or 2)
    except (TypeError, ValueError):
        units = 10
    corpus_docs = int(state.get("corpus_docs") or 0)
    intended = max(gold_target, units)
    if intended > LARGE_PIPELINE_UNITS and corpus_docs <= 0:
        return (
            f"I will not launch **{intended:,}** gold with no attached corpus. "
            "Large jobs (more than 10 units) need source material under My data. "
            "Web-research-only can run an exploratory **10**-example job — type "
            "**start 10**. Official rate is **$35 / 1,000** gold "
            f"(so {intended:,} ≈ **${intended / 1000.0 * 35:.0f}**, not a lower guess)."
        )
    return None


def official_estimate_for_state(state: dict[str, Any]) -> dict[str, Any]:
    from helix.services.user_material_upload import estimate_setup_pricing

    return estimate_setup_pricing(state)


def apply_official_riu_estimate(
    reply: str,
    *,
    phase: str,
    state: dict[str, Any],
) -> str:
    """Replace invented $ / hour quotes with the job-system estimate."""
    from helix.services.user_material_upload import format_official_estimate

    pricing = official_estimate_for_state(state)
    state["pricing_estimate"] = pricing
    block = format_official_estimate(
        pricing, project=str(state.get("project_name") or state.get("domain") or "")
    )
    ph = (phase or "").lower()
    if ph in {"pricing", "confirm", "model_estimate"} or _looks_like_cost_quote(reply):
        lead = (reply or "").strip()
        # Drop leftover invented dollar/hour sentences from the model.
        cleaned: list[str] = []
        for para in re.split(r"\n{2,}", lead):
            if re.search(r"\$\s*\d", para) or re.search(
                r"\b(\d+\s*[-–]\s*\d+|\d+)\s*hours?\b", para, re.I
            ):
                continue
            if "just type start" in para.lower() and not pricing.get(
                "can_start_requested"
            ):
                continue
            cleaned.append(para.strip())
        intro = "\n\n".join(p for p in cleaned if p).strip()
        if intro:
            return f"{intro}\n\n{block}"
        return block
    return reply


def _heuristic_turn(user_text: str, state: dict, phase: str) -> dict[str, Any]:
    """Deterministic fallback when LLM is unavailable."""
    t = (user_text or "").strip()
    lower = t.lower()
    actions: list[dict] = []
    patch: dict[str, Any] = {}
    reply = ""
    next_phase = phase
    progress = 10

    wants_run = _wants_run(t)
    refuse = _refuses_run(t)
    phase = {
        "plan": "discover",
        "formats": "example",
        "goals": "edge_cases",
        "pricing": "model_estimate",
    }.get(phase, phase)

    if phase in {"greet", "role"} and not state.get("role_text"):
        from helix.services.role_risk import classify_role

        role_text = t or "general assistant"
        risk = classify_role(role_text)
        patch["role_text"] = role_text
        patch["role_type"] = risk["role_type"]
        patch["risk_level"] = risk["risk_level"]
        patch["edge_cases_required"] = risk["edge_cases_required"]
        patch["quality_mode"] = risk["quality_mode"]
        patch["recommended_base_model"] = risk["recommended_base_model"]
        patch["recommended_model_name"] = risk.get("recommended_model_name") or ""
        patch["project_name"] = role_text[:120]
        patch["domain"] = role_text
        reply = (
            f"Role noted: **{role_text[:160]}** — {risk['summary']}\n\n"
            "In one or two sentences: **what should this AI get better at**, "
            "and what’s the product or domain?"
        )
        next_phase = "discover"
        progress = 18
    elif phase in {"greet", "discover"} and not state.get("mission"):
        patch["mission"] = (
            f"Collect high-quality training examples so the AI can: {t}"
            if t
            else "Collect high-quality training examples"
        )
        if not state.get("role_text"):
            from helix.services.role_risk import classify_role

            risk = classify_role(t or state.get("domain") or "")
            patch["role_text"] = t or state.get("domain") or "general"
            patch.update(
                {
                    "role_type": risk["role_type"],
                    "risk_level": risk["risk_level"],
                    "edge_cases_required": risk["edge_cases_required"],
                    "quality_mode": risk["quality_mode"],
                    "recommended_base_model": risk["recommended_base_model"],
                    "recommended_model_name": risk.get("recommended_model_name") or "",
                }
            )
        reply = (
            "Which **topics** should we cover? Comma-separated "
            "(e.g. billing, shipping, returns)."
        )
        next_phase = "discover"
        progress = 32
    elif phase == "discover" and not state.get("categories"):
        cats = [c.strip() for c in re.split(r"[,;\n]+", t) if c.strip()]
        patch["categories"] = cats or ["general"]
        patch["phase_targets"] = {c: 40 for c in patch["categories"][:8]}
        patch["sources"] = state.get("sources") or ["docs", "web"]
        reply = (
            f"Topics: **{', '.join(patch['categories'])}**.\n\n"
            "Give me **one perfect example**:\n"
            "1) the input / user message\n"
            "2) the ideal output / answer"
        )
        next_phase = "example"
        progress = 45
        actions.append({"type": "save_plan"})
    elif phase in {"example", "formats"}:
        # parse sample Q/A
        parts = re.split(r"\n\s*\n|\n(?=answer|output|a:|ideal)", t, maxsplit=1, flags=re.I)
        if len(parts) >= 2:
            patch["sample_input"] = parts[0].strip()
            patch["sample_output"] = parts[1].strip()
        else:
            lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
            if len(lines) >= 2:
                patch["sample_input"] = lines[0]
                patch["sample_output"] = "\n".join(lines[1:])
            else:
                patch["sample_input"] = t or "How can you help me?"
                patch["sample_output"] = (
                    "I'd be happy to help. Could you share a bit more detail?"
                )
        domain = state.get("domain") or state.get("project_name") or "your AI"
        patch["format_name"] = state.get("format_name") or "Primary example format"
        patch["topic_key"] = state.get("topic_key") or _topic_key(
            patch["format_name"] or "examples"
        )
        patch["format_description"] = f"Ideal Q&A style examples for {domain}"
        patch["sample_rationale"] = "Clear, helpful, and matches the product tone."
        need = int(state.get("edge_cases_required") or 2)
        reply = (
            "That’s the clean example — I’ll use it as the quality bar.\n\n"
            f"This role is **{state.get('risk_level') or 'medium'}** risk, so I need "
            f"**{need} tricky / edge-case scenario(s)** (outliers, not the happy path).\n"
            "Send the first one: a hard input and what the AI should do."
        )
        next_phase = "edge_cases"
        progress = 58
        actions.append({"type": "save_format"})
        actions.append({"type": "save_plan"})
    elif phase == "edge_cases":
        edges = list(state.get("edge_cases") or [])
        if t and lower not in {"skip", "done", "next"}:
            edges.append(t[:2000])
        patch["edge_cases"] = edges
        need = int(state.get("edge_cases_required") or 2)
        if len(edges) < need and lower not in {"skip"}:
            reply = (
                f"Logged edge case **{len(edges)}/{need}**.\n\n"
                "Give me another tricky one — bias, missing info, conflict, or abuse."
            )
            next_phase = "edge_cases"
            progress = 58 + min(12, 4 * len(edges))
        else:
            reply = (
                f"I have **{len(edges)}** edge case(s). "
                "Do you already have **labeled** Q&A (zip) to save in gold format?\n\n"
                "Reply **yes** (upload) or **no** / **skip**."
            )
            next_phase = "own_data"
            progress = 78
            actions.append({"type": "save_goals"})
            actions.append({"type": "save_plan"})
    elif phase == "goals":
        # parse goals + quality
        gm = re.search(r"gold\s*[:=]?\s*(\d+)", lower)
        vm = re.search(r"var(?:iation)?s?\s*[:=]?\s*(\d+)", lower)
        if gm:
            patch["gold_target"] = max(1, min(int(gm.group(1)), 1_000_000))
        if vm:
            patch["variations_per_gold"] = max(1, min(int(vm.group(1)), 20))
        if any(w in lower for w in ("best", "highest", "mode 1", "all agents")):
            patch["quality_mode"] = 1
        elif any(w in lower for w in ("balanced", "mode 3", "medium")):
            patch["quality_mode"] = 3
        elif any(w in lower for w in ("cheap", "lowest", "lean", "mode 4")):
            patch["quality_mode"] = 4
        elif any(w in lower for w in ("high", "mode 2", "default", "ok", "yes", "fine")):
            patch["quality_mode"] = 2
        if "synth" in lower or "variation" in lower:
            patch["run_synthesis"] = "no" not in lower and "skip" not in lower
        try:
            q = int(patch.get("quality_mode", state.get("quality_mode", 2)))
            g = int(patch.get("gold_target", state.get("gold_target", 5000)))
            v = int(patch.get("variations_per_gold", state.get("variations_per_gold", 4)))
            batches = int(state.get("total_batches") or 2)
            bsize = int(state.get("batch_size") or 5)
        except (TypeError, ValueError):
            q, g, v, batches, bsize = 2, 5000, 4, 2, 5
        # Prefer newly patched project fields when summarizing
        pname = patch.get("project_name") or state.get("project_name") or "My project"
        mission = patch.get("mission") or state.get("mission") or "Collect great training examples"
        cats = patch.get("categories") or state.get("categories") or ["general"]
        reply = (
            "Goals noted.\n\n"
            f"• Gold target: **{g:,}** · variations/gold: **{v}** · quality mode **{q}**\n\n"
            "Do you already have **your own labeled data** (Q&A, tickets, chats) "
            "you want saved in the **same gold format** Helix uses — so you can "
            "download it later and use it with **Double Helix** training?\n\n"
            "Reply **yes** (I'll show a zip upload) or **no** / **skip** to continue."
        )
        next_phase = "own_data"
        progress = 82
        actions.append({"type": "save_goals"})
        actions.append({"type": "save_plan"})
    elif phase == "own_data":
        yes = any(
            w in lower
            for w in (
                "yes",
                "yeah",
                "yep",
                "y",
                "i have",
                "i do",
                "upload",
                "zip",
                "my data",
                "own data",
                "have data",
            )
        ) and not any(w in lower for w in ("no", "skip", "later", "not now", "don't"))
        no = any(
            w in lower
            for w in ("no", "skip", "later", "not now", "don't have", "nope", "nah")
        ) or lower.strip() in {"n", "no thanks"}
        # "done" / "continue" after they uploaded
        if lower.strip() in {"done", "continue", "next", "ok", "okay"} and state.get(
            "own_data_uploaded"
        ):
            no = True
            yes = False
        if yes and not no:
            patch["has_own_data"] = True
            patch["own_data_awaiting_upload"] = True
            reply = (
                "Perfect. Use the **Upload my gold zip** control below (or under "
                "**My data**).\n\n"
                "Zip should include `.jsonl`, `.json`, or `.csv` files with pairs like "
                "`input`+`output` (also accepts question/answer or prompt/completion).\n\n"
                "I'll save them as **gold-format** rows (downloadable anytime, ready "
                "for Double Helix later).\n\n"
                "After uploading, reply **done** or **continue**. "
                "Or say **skip** to move on without uploading."
            )
            next_phase = "own_data"
            progress = 85
        else:
            patch["has_own_data"] = bool(state.get("own_data_uploaded"))
            patch["own_data_awaiting_upload"] = False
            reply = (
                "Next: do you have **other materials** that aren’t already labeled "
                "Q&A — but you’d still want the model trained on?\n\n"
                "Examples: a **tele-sales script**, a **game referee rulebook**, "
                "**formulas/lesson notes** for a teacher, SOPs, product manuals…\n\n"
                "Reply **yes** (zip upload — I’ll convert them into trainable "
                "gold-format pairs) or **no** / **skip**."
            )
            next_phase = "materials"
            progress = 88
    elif phase == "materials":
        yes = any(
            w in lower
            for w in (
                "yes",
                "yeah",
                "yep",
                "y",
                "i have",
                "i do",
                "upload",
                "zip",
                "script",
                "rulebook",
                "formula",
                "manual",
                "sop",
                "materials",
            )
        ) and not any(
            w in lower for w in ("no", "skip", "later", "not now", "don't", "nope")
        )
        no = any(
            w in lower
            for w in ("no", "skip", "later", "not now", "don't have", "nope", "nah")
        ) or lower.strip() in {"n", "no thanks"}
        if lower.strip() in {"done", "continue", "next", "ok", "okay"} and state.get(
            "materials_uploaded"
        ):
            no = True
            yes = False
        if yes and not no:
            patch["has_materials"] = True
            patch["materials_awaiting_upload"] = True
            reply = (
                "Great. Use **Upload materials zip** below (or **My data**).\n\n"
                "Zip freeform docs: `.txt`, `.md`, `.html`, `.csv`, `.json` — "
                "scripts, rulebooks, notes, formulas, SOPs.\n\n"
                "I’ll convert them into the **best-fit trainable gold format** "
                "so they work alongside mined gold and your labeled uploads. "
                "You can download them anytime from My data.\n\n"
                "After uploading, reply **done** or **continue**. "
                "Or **skip** to go to pricing."
            )
            next_phase = "materials"
            progress = 90
        else:
            patch["has_materials"] = bool(state.get("materials_uploaded"))
            patch["materials_awaiting_upload"] = False
            # Build pricing + summary → confirm
            from helix.services.user_material_upload import estimate_setup_pricing

            merged = {**state, **patch}
            pricing = estimate_setup_pricing(merged)
            patch["pricing_estimate"] = pricing
            pname = state.get("project_name") or "My project"
            mission = state.get("mission") or "Collect great training examples"
            cats = state.get("categories") or ["general"]
            own_n = int(state.get("own_data_count") or 0)
            mat_n = int(state.get("materials_count") or 0)
            data_bits = []
            if own_n:
                data_bits.append(f"{own_n} labeled gold")
            if mat_n:
                data_bits.append(f"{mat_n} material rows")
            data_line = (
                f"• Your data: **{', '.join(data_bits)}**\n" if data_bits else ""
            )
            from helix.services.user_material_upload import format_official_estimate
            from helix.services.base_models import format_model_menu, recommend_model

            rec = recommend_model(
                role_type=str(state.get("role_type") or ""),
                risk_level=str(state.get("risk_level") or "medium"),
            )
            patch["recommended_base_model"] = rec["id"]
            patch["recommended_model_name"] = rec["name"]
            reply = (
                "Here’s the official setup summary — numbers come from the same "
                "$35/1k + corpus rules as mining jobs, not a guess.\n\n"
                f"• Project: **{pname}**\n"
                f"• Goal: {mission}\n"
                f"• Topics: {', '.join(cats)}\n"
                f"{data_line}"
                f"{format_official_estimate(pricing)}\n\n"
                f"Default later-train model: **{rec['name']}** ({rec['license']}, "
                f"{rec['params_b']}B, QLoRA).\n\n"
                f"{format_model_menu()}\n"
            )
            next_phase = "model_estimate"
            progress = 90
            actions.append({"type": "save_goals"})
            actions.append({"type": "save_plan"})
    elif phase in {"pricing", "model_estimate"}:
        from helix.services.user_material_upload import (
            estimate_setup_pricing,
            format_official_estimate,
        )
        from helix.services.base_models import (
            format_model_menu,
            recommend_model,
            resolve_user_model_choice,
        )

        pricing = estimate_setup_pricing(state)
        patch["pricing_estimate"] = pricing
        rec = recommend_model(
            role_type=str(state.get("role_type") or ""),
            risk_level=str(state.get("risk_level") or "medium"),
        )
        picked = resolve_user_model_choice(t)
        keep = lower in {"ok", "okay", "default", "yes", "y", "keep", "that one"}
        if picked or keep or wants_run:
            chosen = picked or rec
            patch["recommended_base_model"] = chosen["id"]
            patch["recommended_model_name"] = chosen["name"]
            reply = (
                f"Locked **{chosen['name']}** (`{chosen['id']}`) · "
                f"{chosen['license']} · {chosen['params_b']}B · QLoRA.\n\n"
                f"{format_official_estimate(pricing)}\n"
            )
            next_phase = "confirm"
            progress = 94
            actions.append({"type": "save_goals"})
        else:
            patch["recommended_base_model"] = state.get("recommended_base_model") or rec["id"]
            reply = (
                f"Default: **{rec['name']}** ({rec['license']}). "
                "Pick another Apache/MIT model ≤30B or say **ok**.\n\n"
                f"{format_model_menu()}\n\n"
                f"{format_official_estimate(pricing)}\n"
            )
            next_phase = "model_estimate"
            progress = 92
    elif phase == "running":
        if wants_run:
            reply = (
                "A job is already in motion (or was just queued). "
                "Check **AI helpers → Your jobs** for live progress, "
                "or say **restart** to set up a new collection."
            )
        else:
            reply = (
                "I'm still here. Your mining job keeps running even if you leave. "
                "Open **Home** for job status, or say **restart** "
                "to configure a new project with me."
            )
        next_phase = "running"
        progress = 100
    elif phase == "confirm":
        if refuse:
            reply = "No problem — I won't start yet. Tell me what to change, or say **start** when ready."
            next_phase = "confirm"
            progress = 90
        elif wants_run:
            actions = [
                {"type": "save_plan"},
                {"type": "save_format"},
                {"type": "save_goals"},
                {"type": "start_pipeline"},
            ]
            reply = (
                "Starting now. I'm saving your plan and quality bar, "
                "then queueing a mining job that keeps running even if you leave.\n\n"
                "Watch progress on **Home**. I will **not** start variations yet — "
                "I'll ask after gold is ready."
            )
            next_phase = "running"
            progress = 100
        else:
            # Allow mid-confirm goal tweaks
            gm = re.search(r"gold\s*[:=]?\s*(\d+)", lower)
            if gm:
                patch["gold_target"] = max(1, min(int(gm.group(1)), 1_000_000))
            if any(w in lower for w in ("cheap", "lowest", "lean", "mode 4")):
                patch["quality_mode"] = 4
            elif any(w in lower for w in ("best", "highest", "mode 1")):
                patch["quality_mode"] = 1
            elif any(w in lower for w in ("balanced", "mode 3")):
                patch["quality_mode"] = 3
            reply = (
                "When you're ready, say **start**. "
                "Or change goals/quality (e.g. `gold 2000, quality cheap`)."
            )
            next_phase = "confirm"
            progress = 90
            if patch:
                actions.append({"type": "save_goals"})
    elif phase == "offer_synth":
        from helix.services.cost_tracking import GOLD_COST_CAP_USD_PER_1000

        try:
            g = int(state.get("gold_target") or 10)
            v = int(state.get("variations_per_gold") or 4)
        except (TypeError, ValueError):
            g, v = 10, 4
        extra = max(1, g) * max(1, v)
        extra_usd = round((extra / 1000.0) * GOLD_COST_CAP_USD_PER_1000, 2)
        yes = bool(re.search(r"\b(yes|yeah|yep|variations|synth)\b", lower))
        no = bool(re.search(r"\b(no|skip|later|not now)\b", lower))
        wants_train = (
            "confirm train" in lower
            or "train with double helix" in lower
            or ("double helix" in lower and "confirm" in lower)
        )
        asks_train = ("train" in lower or "double helix" in lower) and not wants_train
        if wants_train:
            actions.append({"type": "start_double_helix_train"})
            reply = (
                "Starting Double Helix training on the gold already in your account "
                "(no re-upload). GPU is pay per run, about **$15–50**. "
                "When it finishes, download the trained zip from **My data**."
            )
            next_phase = "done"
            progress = 100
        elif asks_train:
            reply = (
                "Two options:\n\n"
                "1. **Download** your gold from **My data** and train anywhere.\n"
                "2. **Train with Double Helix** — Helix fetches gold from this account, "
                "runs QLoRA on pay-per-run GPU (~$15–50), then gives you a zip "
                "(adapter + tokenizer + the gold used).\n\n"
                "Say **confirm train** to start option 2, or open **My data**."
            )
            next_phase = "offer_synth"
            progress = 96
        elif yes and not no:
            actions.append({"type": "start_synthesis"})
            reply = (
                f"Starting variations: about **{extra:,}** extra rows, "
                f"budgeted at **${extra_usd:.2f}** on the $35/1k meter "
                f"({v} per gold). This is a separate job."
            )
            next_phase = "done"
            progress = 100
        else:
            reply = (
                "Gold mining finished. Want **variations** of that gold?\n\n"
                f"That would add about **{extra:,}** rows "
                f"(**{v}** per gold) and is billed on the same "
                f"**${GOLD_COST_CAP_USD_PER_1000:.0f}/1k** meter ≈ **${extra_usd:.2f}**.\n\n"
                "Reply **yes** to start synthesis, or **no** / **skip** to stop here."
            )
            next_phase = "offer_synth"
            progress = 96
            if no:
                next_phase = "done"
                reply = (
                    "All set — no variations. From **My data** you can "
                    "**download your gold** or **train with Double Helix** "
                    "(Helix uses the gold already in your account)."
                )
                progress = 100
    elif phase == "done":
        wants_train = "confirm train" in lower or (
            "double helix" in lower and "confirm" in lower
        )
        asks_train = ("train" in lower or "double helix" in lower) and not wants_train
        if wants_train:
            actions.append({"type": "start_double_helix_train"})
            reply = (
                "Starting Double Helix on gold already in your account. "
                "Watch **My data** for the download link when training finishes."
            )
            next_phase = "done"
            progress = 100
        elif asks_train:
            reply = (
                "Download gold from **My data**, or say **confirm train** to "
                "run Double Helix QLoRA (~$15–50) on that same account gold."
            )
            next_phase = "done"
            progress = 100
        else:
            reply = (
                "I'm here. Download gold from **My data**, say **confirm train** "
                "for Double Helix, or **restart** for a new collection."
            )
            next_phase = "done"
            progress = 100
    else:
        reply = (
            "I'm here. Tell me what you want to train, or say **restart** "
            "to begin a fresh setup with me."
        )
        next_phase = "discover"
        progress = 10

    if lower.strip() in {"restart", "start over", "reset"}:
        return {
            "reply": "Starting fresh. **What AI or product are you training?**",
            "phase": "greet",
            "state_patch": {},
            "actions": [],
            "progress": 0,
            "reset": True,
        }

    # Normalize internal plan_topics → plan for storage
    if next_phase == "plan_topics":
        next_phase = "plan"

    return {
        "reply": reply,
        "phase": next_phase,
        "state_patch": patch,
        "actions": actions,
        "progress": progress,
    }


def _llm_turn(
    *,
    tenant: m.Tenant,
    messages: list[dict],
    state: dict,
    phase: str,
) -> dict[str, Any]:
    from helix.llm.client import get_llm_client_for_tenant

    client = get_llm_client_for_tenant(tenant)
    # compact history for the model
    hist: list[dict[str, Any]] = []
    for msg in messages[-16:]:
        role = msg.get("role")
        if role not in {"user", "assistant"}:
            continue
        content = msg.get("content") or ""
        if role == "assistant":
            # strip markdown bold for model noise reduction is optional
            hist.append({"role": "assistant", "content": content})
        else:
            hist.append({"role": "user", "content": content})

    # Put state snapshot as the first user turn, then dialogue.
    # Drop trailing assistant-only incomplete turns.
    from helix.services.user_material_upload import format_official_estimate

    official = format_official_estimate(official_estimate_for_state(state))
    context = (
        f"Current phase: {phase}\n"
        f"Collected state JSON:\n{json.dumps(state, ensure_ascii=False)[:4000]}\n"
        "OFFICIAL ESTIMATE (authoritative — copy these facts, do not invent others):\n"
        f"{official[:2500]}\n"
        "Respond with the required JSON object only. "
        "Do not quote dollar amounts or hour ETAs except those in the official estimate. "
        "Only emit start_pipeline after the user clearly confirms AND the official "
        "estimate says the requested volume can start (or they said start 10)."
    )
    chat_messages: list[dict[str, Any]] = [{"role": "user", "content": context}]
    for h in hist:
        # avoid duplicating the context-only first message
        chat_messages.append(h)

    resp = client.chat(
        system=SYSTEM_PROMPT, messages=chat_messages, tools=None, tool_choice=None
    )
    content = (resp.choices[0].message.content or "").strip()
    data = _extract_json_object(content)
    if not data or "reply" not in data:
        raise RuntimeError("Riu LLM returned unparseable JSON")
    # Sanitize phase
    phase_raw = str(data.get("phase") or phase).strip().lower()
    allowed = {
        "greet",
        "role",
        "discover",
        "example",
        "edge_cases",
        "own_data",
        "materials",
        "model_estimate",
        "confirm",
        "running",
        "offer_synth",
        "done",
        # aliases from older sessions
        "plan",
        "formats",
        "goals",
        "pricing",
    }
    if phase_raw not in allowed:
        phase_raw = phase if phase in allowed else "discover"
    # Normalize actions
    actions_in = data.get("actions") if isinstance(data.get("actions"), list) else []
    actions_clean: list[dict] = []
    for a in actions_in:
        if isinstance(a, dict) and a.get("type"):
            actions_clean.append({"type": str(a["type"]).strip()})
        elif isinstance(a, str) and a.strip():
            actions_clean.append({"type": a.strip()})
    try:
        progress = int(data.get("progress") or 0)
    except (TypeError, ValueError):
        progress = 0
    progress = max(0, min(100, progress))
    return {
        "reply": str(data.get("reply") or "").strip() or "Could you say a bit more?",
        "phase": phase_raw,
        "state_patch": data.get("state_patch")
        if isinstance(data.get("state_patch"), dict)
        else {},
        "actions": actions_clean,
        "progress": progress,
    }


def _apply_save_plan(
    db: Session, *, tenant: m.Tenant, state: dict
) -> dict[str, Any]:
    name = (state.get("project_name") or "My training project").strip()[:200]
    slug = "default"
    domain = (state.get("domain") or name).strip()
    mission = (state.get("mission") or "Collect high-quality training examples.").strip()
    questions = state.get("research_questions") or []
    if isinstance(questions, str):
        questions = [q.strip() for q in questions.splitlines() if q.strip()]
    categories = state.get("categories") or ["general"]
    if not isinstance(categories, list):
        categories = [str(categories)]
    sources = state.get("sources") or ["docs", "web"]
    if not isinstance(sources, list):
        sources = [str(sources)]
    targets = state.get("phase_targets") or {c: 40 for c in categories}
    if not isinstance(targets, dict):
        targets = {c: 40 for c in categories}
    instructions = state.get("notes") or (
        "Prefer quality over volume. Do not invent scrape data. Escalate when unsure."
    )
    if state.get("role_text"):
        instructions = (
            f"ROLE:{state.get('role_text')}\n"
            f"RISK:{state.get('risk_level') or 'medium'}\n"
            f"ROLE_TYPE:{state.get('role_type') or ''}\n"
            + instructions
        )
    if state.get("recommended_base_model"):
        instructions = f"MODEL:{state.get('recommended_base_model')}\n" + instructions

    existing = (
        db.query(m.ResearchProject)
        .filter_by(tenant_id=tenant.id, slug=slug)
        .first()
    )
    # Replace formats when Riu sets a primary topic (avoid contaminating with old demo topics)
    replace_formats = bool(state.get("replace_formats", True))
    topic_keys: list[str] = []
    if state.get("topic_key"):
        topic_keys = [_topic_key(str(state["topic_key"]))]
    elif not replace_formats and existing:
        prev = _load_json(existing.topic_keys_json, [])
        topic_keys = list(prev) if isinstance(prev, list) else []

    if existing:
        existing.name = name
        existing.domain = domain
        existing.mission = mission
        existing.research_questions_json = json.dumps(questions)
        existing.categories_json = json.dumps(categories)
        existing.sources_json = json.dumps(sources)
        existing.phase_targets_json = json.dumps(targets)
        if topic_keys or replace_formats:
            existing.topic_keys_json = json.dumps(topic_keys)
        existing.agent_instructions = instructions
        existing.is_active = True
        existing.updated_at = _now()
        # deactivate others
        for p in db.query(m.ResearchProject).filter_by(tenant_id=tenant.id).all():
            if p.id != existing.id:
                p.is_active = False
        db.commit()
        db.refresh(existing)
        sync_workspace_from_brief(db, tenant.id, force_queue=True)
        return {
            "ok": True,
            "action": "save_plan",
            "project": project_to_dict(existing),
            "formats_replaced": replace_formats,
            "topic_keys": topic_keys,
        }

    row = m.ResearchProject(
        id=_uid("prj_"),
        tenant_id=tenant.id,
        slug=slug,
        name=name,
        domain=domain,
        mission=mission,
        research_questions_json=json.dumps(questions),
        sources_json=json.dumps(sources),
        categories_json=json.dumps(categories),
        phase_targets_json=json.dumps(targets),
        success_metrics_json=json.dumps(
            [
                {"name": "approved_examples", "target": "grow weekly"},
                {"name": "benchmark_coverage", "target": "balanced difficulty"},
            ]
        ),
        topic_keys_json=json.dumps(topic_keys),
        agent_instructions=instructions,
        output_notes="Export JSONL with input, output, rationale, difficulty.",
        is_active=True,
        updated_at=_now(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    sync_workspace_from_brief(db, tenant.id, force_queue=True)
    return {
        "ok": True,
        "action": "save_plan",
        "project": project_to_dict(row),
        "formats_replaced": True,
        "topic_keys": topic_keys,
    }


def _apply_save_format(
    db: Session, *, tenant: m.Tenant, state: dict
) -> dict[str, Any]:
    topic = _topic_key(state.get("topic_key") or state.get("format_name") or "examples")
    display = (state.get("format_name") or topic.replace("_", " ").title())[:200]
    desc = (
        state.get("format_description")
        or f"Training examples for {state.get('domain') or display}"
    )
    sample = {
        "input": state.get("sample_input") or "How can you help me today?",
        "output": state.get("sample_output")
        or "I'd be happy to help. What do you need?",
        "rationale": state.get("sample_rationale") or "Clear and helpful",
        "difficulty": "canonical",
        "is_negative": False,
    }
    row = (
        db.query(m.TopicSchema)
        .filter_by(tenant_id=tenant.id, topic=topic)
        .first()
    )
    if row:
        row.display_name = display
        row.description = desc
        row.schema_json = json.dumps(DEFAULT_SCHEMA)
        row.sample_row_json = json.dumps(sample)
        row.export_format = "jsonl"
        row.is_active = True
        row.updated_at = _now()
    else:
        row = m.TopicSchema(
            id=_uid("ts_"),
            tenant_id=tenant.id,
            topic=topic,
            display_name=display,
            description=desc,
            schema_json=json.dumps(DEFAULT_SCHEMA),
            sample_row_json=json.dumps(sample),
            export_format="jsonl",
            is_active=True,
            updated_at=_now(),
        )
        db.add(row)

    # Replace plan formats with this primary format (avoid merging demo + new topics)
    plan = get_active_project(db, tenant.id)
    deactivated = []
    if plan:
        plan.topic_keys_json = json.dumps([topic])
        plan.updated_at = _now()
        # Deactivate other topic schemas so UI lists only the active training format
        for other in (
            db.query(m.TopicSchema)
            .filter_by(tenant_id=tenant.id, is_active=True)
            .all()
        ):
            if other.topic != topic:
                other.is_active = False
                other.updated_at = _now()
                deactivated.append(other.topic)
    db.commit()
    db.refresh(row)
    return {
        "ok": True,
        "action": "save_format",
        "schema": schema_to_dict(row),
        "formats_replaced": True,
        "deactivated_topics": deactivated,
        "active_topic": topic,
    }


def _apply_save_goals(
    db: Session, *, user_id: str, tenant_id: str, state: dict
) -> dict[str, Any]:
    scope = update_scope(
        db,
        user_id,
        tenant_id,
        gold_target_count=int(state.get("gold_target") or 5000),
        variations_per_gold=int(state.get("variations_per_gold") or 4),
        vary_parameters=["tone", "difficulty", "persona", "context", "locale"],
        auto_promote_approved=True,
    )
    return {
        "ok": True,
        "action": "save_goals",
        "gold_target": scope.gold_target_count,
        "variations_per_gold": scope.variations_per_gold,
    }


def _apply_start_pipeline(
    db: Session, *, user_id: str, tenant_id: str, state: dict
) -> dict[str, Any]:
    from helix.services.corpus import require_corpus_for_large_job

    reason = riu_start_block_reason(state)
    if reason:
        raise ValueError(reason)
    # Exploratory: force the 10-unit web job, never a 5000-row launch.
    batch_size = int(state.get("batch_size") or 5)
    total_batches = int(state.get("total_batches") or 2)
    if state.get("accept_exploratory"):
        batch_size, total_batches = 5, 2
    require_corpus_for_large_job(
        db,
        tenant_id=tenant_id,
        owner_user_id=user_id,
        batch_size=batch_size,
        total_batches=total_batches,
    )
    job = create_batch_job(
        db,
        owner_user_id=user_id,
        tenant_id=tenant_id,
        job_type="pipeline",
        quality_mode=int(state.get("quality_mode") or 2),
        batch_size=batch_size,
        total_batches=total_batches,
        auto_continue=True,
        config={"source": "riu", "exploratory": bool(state.get("accept_exploratory"))},
    )
    return {"ok": True, "action": "start_pipeline", "job": job_to_dict(job)}


def _apply_start_synthesis(
    db: Session, *, user_id: str, tenant_id: str, state: dict
) -> dict[str, Any]:
    job = create_batch_job(
        db,
        owner_user_id=user_id,
        tenant_id=tenant_id,
        job_type="synthesis",
        quality_mode=int(state.get("quality_mode") or 2),
        batch_size=int(state.get("batch_size") or 5),
        total_batches=int(state.get("total_batches") or 1),
        auto_continue=True,
        config={
            "source": "riu",
            "variations_per_gold": int(state.get("variations_per_gold") or 4),
            "parameters": ["tone", "difficulty", "persona", "context", "locale"],
        },
    )
    return {"ok": True, "action": "start_synthesis", "job": job_to_dict(job)}


def _ready_for_pipeline(state: dict) -> tuple[bool, str]:
    if not (state.get("project_name") or state.get("domain") or state.get("mission")):
        return False, "Need a project name or mission before starting."
    return True, ""


def execute_actions(
    db: Session,
    *,
    tenant: m.Tenant,
    user_id: str,
    session: m.RiuSession,
    state: dict,
    actions: list[dict],
    user_text: str = "",
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    # Deduplicate while preserving order
    seen: set[str] = set()
    ordered: list[dict] = []
    for raw in actions or []:
        if not isinstance(raw, dict):
            continue
        atype = str(raw.get("type") or "").strip()
        if not atype or atype in seen:
            continue
        seen.add(atype)
        ordered.append(raw)

    for raw in ordered:
        atype = str(raw.get("type") or "").strip()
        try:
            if atype == "save_plan":
                results.append(_apply_save_plan(db, tenant=tenant, state=state))
            elif atype == "save_format":
                results.append(_apply_save_format(db, tenant=tenant, state=state))
            elif atype == "save_goals":
                results.append(
                    _apply_save_goals(
                        db, user_id=user_id, tenant_id=tenant.id, state=state
                    )
                )
            elif atype == "start_pipeline":
                if not _wants_run(user_text):
                    results.append(
                        {
                            "ok": False,
                            "action": atype,
                            "error": "Say start to confirm a mining job.",
                        }
                    )
                    continue
                ok, reason = _ready_for_pipeline(state)
                if not ok:
                    results.append({"ok": False, "action": atype, "error": reason})
                    continue
                r = _apply_start_pipeline(
                    db, user_id=user_id, tenant_id=tenant.id, state=state
                )
                session.last_job_id = r["job"]["id"]
                results.append(r)
            elif atype == "start_synthesis":
                if not re.search(r"\b(yes|yeah|yep|variations|synth)\b", user_text.lower()):
                    results.append(
                        {
                            "ok": False,
                            "action": atype,
                            "error": "Say yes to confirm variations.",
                        }
                    )
                    continue
                r = _apply_start_synthesis(
                    db, user_id=user_id, tenant_id=tenant.id, state=state
                )
                session.last_synth_job_id = r["job"]["id"]
                results.append(r)
            elif atype == "start_double_helix_train":
                from helix.services.double_helix_train import create_train_job, job_to_dict

                low = user_text.lower()
                if "confirm train" not in low and not (
                    "double helix" in low and "confirm" in low
                ):
                    results.append(
                        {
                            "ok": False,
                            "action": atype,
                            "error": "Say confirm train to start a paid GPU job.",
                        }
                    )
                    continue
                job = create_train_job(
                    db,
                    owner_user_id=user_id,
                    tenant_id=tenant.id,
                    model_id=str(state.get("recommended_base_model") or "") or None,
                    confirm=True,
                )
                results.append(
                    {"ok": True, "action": atype, "job": job_to_dict(job)}
                )
            elif atype:
                results.append({"ok": False, "action": atype, "error": "unknown action"})
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            results.append({"ok": False, "action": atype, "error": str(exc)})
    return results


def handle_user_message(
    db: Session,
    *,
    tenant: m.Tenant,
    user: m.User,
    session: m.RiuSession,
    text: str,
) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("Message cannot be empty")

    messages: list[dict] = _load_json(session.messages_json, [])
    state: dict = _load_json(session.state_json, {})
    phase = session.phase or "greet"

    user_msg = {
        "id": _uid("msg_"),
        "role": "user",
        "content": text,
        "ts": _now().isoformat(),
    }
    messages.append(user_msg)

    try:
        from helix.services.corpus import estimate_corpus_support

        cstats = estimate_corpus_support(
            db, tenant_id=tenant.id, owner_user_id=user.id
        )
        state["corpus_docs"] = cstats["corpus_docs"]
        state["corpus_units"] = cstats["corpus_units"]
        state["attached_support"] = cstats["attached_support"]
        state["own_data_count"] = int(state.get("own_data_count") or 0) or cstats[
            "labeled_rows"
        ]
        state["materials_count"] = int(state.get("materials_count") or 0) or cstats[
            "material_rows"
        ]
    except Exception:  # noqa: BLE001
        pass

    if _user_denied_attached_data(text):
        state["has_own_data"] = False
        state["has_materials"] = False
        state["own_data_count"] = 0
        state["materials_count"] = 0
        # Keep live corpus_docs from DB; user claim is extra signal when DB is also empty.
        if int(state.get("corpus_docs") or 0) == 0:
            state["attached_support"] = 0

    if _wants_exploratory(text):
        state["accept_exploratory"] = True
        state["batch_size"] = 5
        state["total_batches"] = 2

    if text.lower().strip() in {"restart", "start over", "reset"}:
        # soft reset state but keep session
        new = create_session(db, user_id=user.id, tenant_id=tenant.id)
        return session_to_dict(new)

    turn: dict[str, Any]
    used_llm = False
    try:
        turn = _llm_turn(
            tenant=tenant,
            messages=messages,
            state=state,
            phase=phase,
        )
        used_llm = True
    except Exception:
        turn = _heuristic_turn(text, state, phase)

    if turn.get("reset"):
        new = create_session(db, user_id=user.id, tenant_id=tenant.id)
        return session_to_dict(new)

    state = _merge_state(state, turn.get("state_patch"))
    # ensure defaults
    if not state.get("topic_key") and state.get("format_name"):
        state["topic_key"] = _topic_key(state["format_name"])
    if state.get("categories") and not state.get("phase_targets"):
        state["phase_targets"] = {c: 40 for c in state["categories"][:12]}
    # Formats from Riu replace demo defaults by default
    state.setdefault("replace_formats", True)

    if _wants_exploratory(text):
        state["accept_exploratory"] = True

    # Never let the model launch a 5000-gold job the corpus gate would refuse
    actions = list(turn.get("actions") or [])
    lower_msg = text.lower()
    wants_confirm_train = "confirm train" in lower_msg or (
        "double helix" in lower_msg and "confirm" in lower_msg
    )
    if wants_confirm_train and not any(
        (a.get("type") if isinstance(a, dict) else "") == "start_double_helix_train"
        for a in actions
    ):
        actions.append({"type": "start_double_helix_train"})
        turn["actions"] = actions
    block = riu_start_block_reason(state)
    if block and not state.get("accept_exploratory"):
        actions = [a for a in actions if a.get("type") != "start_pipeline"]
        turn["actions"] = actions

    # Official estimate overwrites invented $ / hour quotes
    next_phase_guess = str(turn.get("phase") or phase)
    turn["reply"] = apply_official_riu_estimate(
        str(turn.get("reply") or ""),
        phase=next_phase_guess,
        state=state,
    )
    if block and _wants_run(text) and not state.get("accept_exploratory"):
        turn["reply"] = f"{block}\n\n{turn['reply']}"
        turn["phase"] = "confirm"

    # Monotonic setup progress (never go backward)
    prev_progress = 0
    for m in messages:
        if m.get("role") == "assistant" and m.get("progress") is not None:
            try:
                prev_progress = max(prev_progress, int(m.get("progress") or 0))
            except (TypeError, ValueError):
                pass
    try:
        raw_progress = int(turn.get("progress") or 0)
    except (TypeError, ValueError):
        raw_progress = 0
    progress = max(prev_progress, min(100, raw_progress))
    turn["progress"] = progress

    action_results = execute_actions(
        db,
        tenant=tenant,
        user_id=user.id,
        session=session,
        state=state,
        actions=turn.get("actions") or [],
        user_text=text,
    )

    next_phase = str(turn.get("phase") or phase)
    if any(r.get("action") == "start_pipeline" and r.get("ok") for r in action_results):
        next_phase = "running"
    if next_phase == "done" or (
        session.last_job_id and next_phase == "running" and "done" in (turn.get("reply") or "").lower()
    ):
        # keep running until user finishes
        pass

    # enrich reply with job ids if started
    reply = str(turn.get("reply") or "")
    for r in action_results:
        if r.get("ok") and r.get("action") == "start_pipeline" and r.get("job"):
            reply += f"\n\nMining job **{r['job']['id']}** is queued."
        if r.get("ok") and r.get("action") == "start_synthesis" and r.get("job"):
            reply += f"\n\nSynthesis job **{r['job']['id']}** is queued."
        if r.get("ok") and r.get("action") == "start_double_helix_train" and r.get("job"):
            reply += (
                f"\n\nDouble Helix train **{r['job']['id']}** is queued. "
                "Download the trained zip from **My data** when it is ready."
            )
        if not r.get("ok") and r.get("error"):
            if r.get("action") == "start_pipeline":
                reply = f"{r.get('error')}\n\n{reply}"
            else:
                reply += f"\n\n(Note: {r.get('action')} failed: {r.get('error')})"

    # Surface format replacement to the user when Riu rewrote topics
    for ar in action_results:
        if ar.get("ok") and ar.get("formats_replaced") and ar.get("active_topic"):
            reply += (
                f"\n\n_Formats updated: active format is **{ar['active_topic']}**"
                + (
                    f" (deactivated: {', '.join(ar.get('deactivated_topics') or [])})."
                    if ar.get("deactivated_topics")
                    else "."
                )
                + "_"
            )
        if ar.get("ok") and ar.get("topic_keys") is not None and ar.get("action") == "save_plan":
            keys = ar.get("topic_keys") or []
            if keys:
                reply += f"\n\n_Plan formats set to: {', '.join(keys)}_"

    assistant_msg = {
        "id": _uid("msg_"),
        "role": "assistant",
        "name": RIU_NAME,
        "content": reply,
        "phase": next_phase,
        "actions": turn.get("actions") or [],
        "action_results": action_results,
        "progress": int(turn.get("progress") or 0),
        "used_llm": used_llm,
        "ts": _now().isoformat(),
    }
    messages.append(assistant_msg)

    session.state_json = json.dumps(state)
    session.messages_json = json.dumps(messages)
    session.phase = next_phase
    if next_phase == "done":
        session.status = "completed"
    session.updated_at = _now()
    db.commit()
    db.refresh(session)

    out = session_to_dict(session)
    out["latest_reply"] = reply
    out["action_results"] = action_results
    out["progress"] = assistant_msg["progress"]
    out["used_llm"] = used_llm
    return out
