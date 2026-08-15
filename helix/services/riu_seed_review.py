"""No-resource scale path: review 10 gold → proof 10 → then 1,000 at a higher rate."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from helix.db import models as m
from helix.services.cost_tracking import GOLD_COST_NO_CORPUS_USD_PER_1000

REVIEW_PARAMS: list[tuple[str, str]] = [
    ("input", "Is this a real question someone in this role would ask?"),
    ("output", "Is this the ideal answer — specific and actually helpful?"),
    ("tone", "Is the voice right (tone, formality, empathy)?"),
    ("facts", "Any domain facts that are wrong, missing, or too generic?"),
    ("avoid", "What should we never do in answers like this?"),
]

SKIP_GOLD = (
    "this gold is good",
    "approve gold",
    "next gold",
    "skip gold",
    "looks good",
    "good enough",
)


def _load(raw: str | None) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        data = {}
    return data if isinstance(data, dict) else {}


def review_state(state: dict[str, Any]) -> dict[str, Any]:
    rs = state.get("seed_review")
    if not isinstance(rs, dict):
        rs = {}
        state["seed_review"] = rs
    rs.setdefault("gold_ids", [])
    rs.setdefault("index", 0)
    rs.setdefault("param_index", 0)
    rs.setdefault("notes", {})
    rs.setdefault("understanding", "")
    rs.setdefault("proof_job_id", "")
    rs.setdefault("proof_confirmed", False)
    rs.setdefault("scale_ready", False)
    return rs


def wants_no_resource_scale(state: dict[str, Any]) -> bool:
    try:
        target = int(state.get("gold_target") or 0)
    except (TypeError, ValueError):
        target = 0
    corpus = int(state.get("corpus_docs") or 0)
    attached = int(state.get("attached_support") or 0)
    return target > 10 and corpus <= 0 and attached <= 0


def load_latest_gold(
    db: Session, *, owner_user_id: str, tenant_id: str, limit: int = 10
) -> list[m.GoldExample]:
    return (
        db.query(m.GoldExample)
        .filter_by(
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            is_archived=False,
        )
        .filter(m.GoldExample.verification_status != "rejected")
        .order_by(m.GoldExample.created_at.desc())
        .limit(limit)
        .all()
    )


def begin_review(
    db: Session, *, state: dict[str, Any], owner_user_id: str, tenant_id: str
) -> str:
    rows = list(
        reversed(load_latest_gold(db, owner_user_id=owner_user_id, tenant_id=tenant_id, limit=10))
    )
    rs = review_state(state)
    rs["gold_ids"] = [g.id for g in rows]
    rs["index"] = 0
    rs["param_index"] = 0
    rs["notes"] = rs.get("notes") or {}
    rs["proof_confirmed"] = False
    rs["scale_ready"] = False
    state["no_corpus_scale"] = True
    if not rows:
        return (
            "The exploratory job finished but I have no gold to review yet. "
            "We can retry **start 10**, or paste source material under **My data**."
        )
    n = len(rows)
    target = int(state.get("gold_target") or 1000)
    return (
        f"The first **{n}** gold rows are in. You asked for **{target:,}** "
        "with no source material, so we will not jump to that number yet.\n\n"
        "Next: we walk **each of these 10** (or however many we got), "
        "**one parameter at a time**. I store what you say. Then I generate "
        "**10 more** as a proof I understood. After you confirm or edit those, "
        f"I scale to **{target:,}** at the **no-source rate "
        f"${GOLD_COST_NO_CORPUS_USD_PER_1000:.0f} / 1,000** "
        f"(higher than the usual $35, because there is no corpus to extract from).\n\n"
        + _prompt_current(db, state)
    )


def _gold(db: Session, gold_id: str) -> m.GoldExample | None:
    return db.query(m.GoldExample).filter_by(id=gold_id).first()


def _prompt_current(db: Session, state: dict[str, Any]) -> str:
    rs = review_state(state)
    ids = rs.get("gold_ids") or []
    i = int(rs.get("index") or 0)
    if i >= len(ids):
        return compile_understanding(db, state)
    g = _gold(db, ids[i])
    if not g:
        rs["index"] = i + 1
        rs["param_index"] = 0
        return _prompt_current(db, state)
    p_i = int(rs.get("param_index") or 0)
    if p_i >= len(REVIEW_PARAMS):
        rs["index"] = i + 1
        rs["param_index"] = 0
        return _prompt_current(db, state)
    key, question = REVIEW_PARAMS[p_i]
    return (
        f"**Gold {i + 1} of {len(ids)}** — parameter **{p_i + 1}/{len(REVIEW_PARAMS)}** ({key})\n\n"
        f"**Input:** {g.input_text}\n\n"
        f"**Output:** {g.output_text}\n\n"
        f"{question}\n\n"
        "Reply in one short note. Or say **this gold is good** to accept the rest of "
        "the parameters on this row and move on."
    )


def apply_review_reply(db: Session, *, state: dict[str, Any], text: str) -> dict[str, Any]:
    rs = review_state(state)
    ids = rs.get("gold_ids") or []
    i = int(rs.get("index") or 0)
    low = (text or "").strip().lower()
    if i >= len(ids):
        return {
            "reply": compile_understanding(db, state),
            "phase": "proof_wait",
            "progress": 88,
            "actions": [{"type": "start_proof_batch"}],
            "state_patch": {},
        }

    gid = ids[i]
    notes: dict[str, Any] = rs.setdefault("notes", {}).setdefault(gid, {})
    skip = any(p in low for p in SKIP_GOLD)
    if skip:
        for key, _q in REVIEW_PARAMS:
            notes.setdefault(key, "approved as-is")
        rs["index"] = i + 1
        rs["param_index"] = 0
    else:
        p_i = int(rs.get("param_index") or 0)
        key, _q = REVIEW_PARAMS[min(p_i, len(REVIEW_PARAMS) - 1)]
        notes[key] = (text or "").strip()[:800]
        rs["param_index"] = p_i + 1
        if rs["param_index"] >= len(REVIEW_PARAMS):
            rs["index"] = i + 1
            rs["param_index"] = 0

    if int(rs.get("index") or 0) >= len(ids):
        reply = compile_understanding(db, state)
        return {
            "reply": reply,
            "phase": "proof_wait",
            "progress": 88,
            "actions": [{"type": "start_proof_batch"}],
            "state_patch": {},
        }

    return {
        "reply": "Saved.\n\n" + _prompt_current(db, state),
        "phase": "review_seed",
        "progress": 70 + min(18, int(rs["index"]) * 2),
        "actions": [],
        "state_patch": {},
    }


def compile_understanding(db: Session, state: dict[str, Any]) -> str:
    rs = review_state(state)
    bits: list[str] = []
    for gid in rs.get("gold_ids") or []:
        g = _gold(db, gid)
        notes = (rs.get("notes") or {}).get(gid) or {}
        if not g:
            continue
        line = f"- Q: {(g.input_text or '')[:120]}"
        for key, _q in REVIEW_PARAMS:
            if notes.get(key):
                line += f" | {key}: {notes[key][:160]}"
        bits.append(line)
    understanding = (
        "Seed-review understanding (no attached corpus):\n"
        + "\n".join(bits[:12])
    )
    rs["understanding"] = understanding[:4000]
    state["seed_understanding"] = rs["understanding"]
    target = int(state.get("gold_target") or 1000)
    usd = round((target / 1000.0) * GOLD_COST_NO_CORPUS_USD_PER_1000, 2)
    return (
        "I have notes on all **10** (or however many we reviewed). "
        "I will now generate **10 more gold** as a proof I understood you.\n\n"
        f"After you confirm or edit those 10, I will scale to **{target:,}** "
        f"at **${GOLD_COST_NO_CORPUS_USD_PER_1000:.0f}/1,000** ≈ **${usd:,.2f}** "
        "(no-source rate).\n\n"
        "Starting the proof batch now."
    )


def proof_ready_message(db: Session, *, state: dict[str, Any], owner_user_id: str, tenant_id: str) -> str:
    rows = load_latest_gold(db, owner_user_id=owner_user_id, tenant_id=tenant_id, limit=10)
    preview = []
    for i, g in enumerate(reversed(rows), start=1):
        preview.append(
            f"{i}. **{(g.input_text or '')[:140]}** → {(g.output_text or '')[:160]}"
        )
        if i >= 10:
            break
    body = "\n".join(preview) or "(no new gold yet)"
    return (
        "Proof batch finished. Here are the latest gold rows:\n\n"
        f"{body}\n\n"
        "Say **confirm scale** if these match what you taught me "
        "(or tell me what to edit). Then I will produce the remaining gold "
        f"toward **{int(state.get('gold_target') or 1000):,}** at the no-source rate."
    )


def scale_batch_plan(state: dict[str, Any]) -> tuple[int, int]:
    """batch_size, total_batches to approach the remaining target."""
    try:
        target = int(state.get("gold_target") or 1000)
    except (TypeError, ValueError):
        target = 1000
    target = max(20, min(target, 5000))
    already = 20  # 10 seed + 10 proof (best-effort)
    remaining = max(10, target - already)
    batch_size = 10
    batches = (remaining + batch_size - 1) // batch_size
    if batches > 100:
        batch_size = min(100, (remaining + 99) // 100)
        batches = (remaining + batch_size - 1) // batch_size
    return max(1, batch_size), max(1, min(100, batches))
