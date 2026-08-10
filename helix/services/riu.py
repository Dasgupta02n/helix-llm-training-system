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
from helix.services.brief import get_active_project, project_to_dict, schema_to_dict
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
2) Understand the user's answers about what AI they want to train.
3) Fill Helix configuration: research plan, example formats, library goals.
4) When enough is known, prepare the system and start mining (and optionally synthesis).
5) Never invent scraped web/data — Helix uses Apify for gathering and OpenRouter only to judge.

Tone: friendly, non-technical, short paragraphs. Avoid jargon (say "training examples" not "SFT dataset").

Conversation phases:
- greet → discover → plan → formats → goals → confirm → running → done

You MUST reply with ONLY a JSON object (no markdown fences) of this shape:
{
  "reply": "What you say to the user in plain English",
  "phase": "greet|discover|plan|formats|goals|confirm|running|done",
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
    "gold_target": 5000,
    "variations_per_gold": 4,
    "quality_mode": 2,
    "batch_size": 5,
    "total_batches": 2,
    "run_synthesis": false,
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
- Emit save_goals when gold_target / variations known (defaults 5000 and 4 ok).
- Emit start_pipeline only after user confirms they want to run (or they clearly said "go"/"start"/"run it").
- Emit start_synthesis only if user wants variations and gold goals are set; usually after pipeline is started or they already have gold.
- progress is 0–100 estimate of setup completeness.
- Merge state_patch with prior state; only include keys you want to update.
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
    return {
        "id": row.id,
        "status": row.status,
        "phase": row.phase,
        "state": _load_json(row.state_json, {}),
        "messages": _load_json(row.messages_json, []),
        "last_job_id": row.last_job_id,
        "last_synth_job_id": row.last_synth_job_id,
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
                "sources": [],
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

    if phase in {"greet", "discover"}:
        patch["project_name"] = t[:120] if t else "My AI project"
        patch["domain"] = t
        reply = (
            f"Got it — you're working on **{patch['project_name']}**.\n\n"
            "In one or two sentences: **what should this AI get better at?** "
            "(e.g. answering shipping questions politely and accurately)"
        )
        next_phase = "plan"
        progress = 20
    elif phase == "plan" and not state.get("mission"):
        patch["mission"] = (
            f"Collect high-quality training examples so the AI can: {t}"
            if t
            else "Collect high-quality training examples"
        )
        reply = (
            "Nice. **Which topics or categories should we cover?** "
            "List a few, comma-separated "
            "(e.g. billing, shipping, returns)."
        )
        next_phase = "plan"
        progress = 35
    elif phase in {"plan", "plan_topics"} and not state.get("categories"):
        cats = [c.strip() for c in re.split(r"[,;\n]+", t) if c.strip()]
        patch["categories"] = cats or ["general"]
        targets = {c: 40 for c in patch["categories"][:8]}
        patch["phase_targets"] = targets
        patch["sources"] = state.get("sources") or ["docs", "tickets", "web"]
        reply = (
            f"Topics noted: **{', '.join(patch['categories'])}**.\n\n"
            "Show me a **perfect example** of what a training row should look like.\n"
            "Reply with:\n"
            "1) a sample question/input\n"
            "2) the ideal answer/output\n\n"
            "(You can write them as two lines or paragraphs.)"
        )
        next_phase = "formats"
        progress = 50
        actions.append({"type": "save_plan"})
    elif phase == "formats":
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
        try:
            g0 = int(state.get("gold_target") or 5000)
            v0 = int(state.get("variations_per_gold") or 4)
        except (TypeError, ValueError):
            g0, v0 = 5000, 4
        reply = (
            "Great sample — I'll use that as the quality bar.\n\n"
            f"Default goals are **{g0:,} gold examples** "
            f"and **{v0} variations** each "
            f"(~{g0 * v0:,} synthesized).\n\n"
            "Reply **ok** to keep defaults, or type e.g. `gold 1000, variations 3`. "
            "Also pick quality: **best / high / balanced / cheap** (default high)."
        )
        next_phase = "goals"
        progress = 70
        actions.append({"type": "save_format"})
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
            "Here's the setup I'll apply:\n\n"
            f"• Project: **{pname}**\n"
            f"• Goal: {mission}\n"
            f"• Topics: {', '.join(cats)}\n"
            f"• Gold target: **{g:,}** · variations/gold: **{v}**\n"
            f"• Quality mode: **{q}** (1 best … 4 cheapest)\n"
            f"• First run: **{batches} batches** × "
            f"**{bsize}** items (keeps going if you sign out)\n\n"
            "Type **start** to save everything and begin collecting. "
            "Or tell me what to change."
        )
        next_phase = "confirm"
        progress = 90
        actions.append({"type": "save_goals"})
        actions.append({"type": "save_plan"})
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
                "Open **AI helpers → Your jobs** for status, or say **restart** "
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
            if state.get("run_synthesis"):
                actions.append({"type": "start_synthesis"})
            reply = (
                "Starting now. I'm saving your plan, formats, and goals, "
                "then queueing a mining job that keeps running even if you leave.\n\n"
                "You can watch progress under **AI helpers → Your jobs**, "
                "or ask me for a status update anytime."
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
    context = (
        f"Current phase: {phase}\n"
        f"Collected state JSON:\n{json.dumps(state, ensure_ascii=False)[:4000]}\n"
        "Respond with the required JSON object only. "
        "Only emit start_pipeline after the user clearly confirms."
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
        "discover",
        "plan",
        "formats",
        "goals",
        "confirm",
        "running",
        "done",
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

    existing = (
        db.query(m.ResearchProject)
        .filter_by(tenant_id=tenant.id, slug=slug)
        .first()
    )
    # Merge topic keys — never wipe prior formats when topic_key not in state yet
    prev_keys: list[str] = []
    if existing:
        prev_keys = _load_json(existing.topic_keys_json, [])
        if not isinstance(prev_keys, list):
            prev_keys = []
    topic_keys = list(prev_keys)
    if state.get("topic_key"):
        tk = _topic_key(str(state["topic_key"]))
        if tk and tk not in topic_keys:
            topic_keys.append(tk)

    if existing:
        existing.name = name
        existing.domain = domain
        existing.mission = mission
        existing.research_questions_json = json.dumps(questions)
        existing.categories_json = json.dumps(categories)
        existing.sources_json = json.dumps(sources)
        existing.phase_targets_json = json.dumps(targets)
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
        return {"ok": True, "action": "save_plan", "project": project_to_dict(existing)}

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
    return {"ok": True, "action": "save_plan", "project": project_to_dict(row)}


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

    # attach topic to active plan
    plan = get_active_project(db, tenant.id)
    if plan:
        keys = _load_json(plan.topic_keys_json, [])
        if topic not in keys:
            keys.append(topic)
            plan.topic_keys_json = json.dumps(keys)
            plan.updated_at = _now()
    db.commit()
    db.refresh(row)
    return {"ok": True, "action": "save_format", "schema": schema_to_dict(row)}


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
    job = create_batch_job(
        db,
        owner_user_id=user_id,
        tenant_id=tenant_id,
        job_type="pipeline",
        quality_mode=int(state.get("quality_mode") or 2),
        batch_size=int(state.get("batch_size") or 5),
        total_batches=int(state.get("total_batches") or 2),
        auto_continue=True,
        config={"source": "riu"},
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
                r = _apply_start_synthesis(
                    db, user_id=user_id, tenant_id=tenant.id, state=state
                )
                session.last_synth_job_id = r["job"]["id"]
                results.append(r)
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

    action_results = execute_actions(
        db,
        tenant=tenant,
        user_id=user.id,
        session=session,
        state=state,
        actions=turn.get("actions") or [],
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
        if not r.get("ok") and r.get("error"):
            reply += f"\n\n(Note: {r.get('action')} failed: {r.get('error')})"

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
