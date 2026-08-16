"""Synthesize training variations from user gold examples."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from helix.config import get_settings
from helix.db import models as m
from helix.llm.client import cost_from_usage, get_llm_client_for_tenant
from helix.services.cost_tracking import record_openrouter_spend
from helix.services.library import (
    AVAILABLE_VARY_PARAMETERS,
    get_or_create_scope,
    library_stats,
)

PARAM_LABELS = {p["key"]: p["label"] for p in AVAILABLE_VARY_PARAMETERS}


def _uid(prefix: str = "syn_") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _template_variations(
    gold: m.GoldExample,
    params: list[str],
    n: int,
) -> list[dict[str, Any]]:
    """Offline / fallback variations when LLM is unavailable."""
    tones = ["formal", "friendly", "concise", "empathetic", "expert"]
    personas = ["new customer", "power user", "busy professional", "skeptical buyer"]
    locales = ["US English", "UK English", "simple plain English"]
    difficulties = ["canonical", "moderate", "edge-case"]
    lengths = ["shorter", "standard", "more detailed"]
    channels = ["chat", "email", "support ticket"]
    contexts = [
        "first contact",
        "follow-up after delay",
        "after a previous failed attempt",
        "high urgency",
    ]

    out: list[dict[str, Any]] = []
    for i in range(n):
        varied: dict[str, str] = {}
        prefix_notes = []
        if "tone" in params:
            t = tones[i % len(tones)]
            varied["tone"] = t
            prefix_notes.append(f"Tone: {t}")
        if "persona" in params:
            p = personas[i % len(personas)]
            varied["persona"] = p
            prefix_notes.append(f"Persona: {p}")
        if "locale" in params:
            loc = locales[i % len(locales)]
            varied["locale"] = loc
            prefix_notes.append(f"Locale: {loc}")
        if "difficulty" in params:
            d = difficulties[i % len(difficulties)]
            varied["difficulty"] = d
        else:
            d = gold.difficulty
        if "length" in params:
            L = lengths[i % len(lengths)]
            varied["length"] = L
            prefix_notes.append(f"Length: {L}")
        if "channel" in params:
            ch = channels[i % len(channels)]
            varied["channel"] = ch
            prefix_notes.append(f"Channel: {ch}")
        if "context" in params:
            ctx = contexts[i % len(contexts)]
            varied["context"] = ctx
            prefix_notes.append(f"Context: {ctx}")

        note = "; ".join(prefix_notes) if prefix_notes else f"Variation {i + 1}"
        inp = gold.input_text
        if "persona" in varied or "context" in varied:
            bits = []
            if "persona" in varied:
                bits.append(f"[{varied['persona']}]")
            if "context" in varied:
                bits.append(f"({varied['context']})")
            inp = f"{' '.join(bits)} {gold.input_text}".strip()

        out_text = gold.output_text
        if varied.get("tone") == "concise":
            # keep first sentence-ish
            out_text = re.split(r"(?<=[.!?])\s+", gold.output_text.strip())[0]
        elif varied.get("length") == "more detailed":
            out_text = (
                f"{gold.output_text.rstrip()} "
                f"(Additional reassurance for this variation: we remain available if you need more help.)"
            )
        elif varied.get("length") == "shorter":
            out_text = re.split(r"(?<=[.!?])\s+", gold.output_text.strip())[0]

        rationale = gold.rationale or ""
        if note:
            rationale = f"{rationale} | Varied: {note}".strip(" |")

        out.append(
            {
                "input": inp,
                "output": out_text,
                "rationale": rationale,
                "difficulty": varied.get("difficulty", d),
                "is_negative": gold.is_negative,
                "varied_parameters": varied,
                "variation_index": i + 1,
            }
        )
    return out


def _llm_variations(
    db: Session,
    tenant: m.Tenant,
    gold: m.GoldExample,
    params: list[str],
    n: int,
) -> list[dict[str, Any]]:
    """Ask the model for structured variations of one gold example."""
    client = get_llm_client_for_tenant(tenant)
    param_desc = ", ".join(PARAM_LABELS.get(p, p) for p in params) or "surface phrasing"
    prompt = f"""You create high-quality synthetic training variations from a GOLD example.
Preserve the underlying business logic and correctness. Only vary: {param_desc}.

GOLD INPUT:
{gold.input_text}

GOLD OUTPUT:
{gold.output_text}

GOLD RATIONALE:
{gold.rationale or "(none)"}

Return ONLY a JSON array of {n} objects with keys:
input, output, rationale, difficulty (canonical|moderate|edge-case),
is_negative (boolean), varied_parameters (object of what you changed).
No markdown fences."""

    resp = client.chat(
        system=(
            "You are a careful synthetic data generator for LLM training. "
            "Never invent unsafe policies. Keep answers faithful to the gold logic."
        ),
        messages=[{"role": "user", "content": prompt}],
        tools=None,
    )
    usage = getattr(resp, "usage", None)
    if usage:
        amt, _src = cost_from_usage(usage, model=client.model)
        if amt > 0:
            record_openrouter_spend(tenant, amt)
            # Stash on tenant for this call chain via attribute (cleared by caller)
            prev = float(getattr(tenant, "_synth_cost_usd", 0.0) or 0.0)
            setattr(tenant, "_synth_cost_usd", prev + amt)
    text = (resp.choices[0].message.content or "").strip()
    # strip fences if any
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("Model did not return a JSON array")
    cleaned: list[dict[str, Any]] = []
    for i, item in enumerate(data[:n]):
        if not isinstance(item, dict):
            continue
        cleaned.append(
            {
                "input": str(item.get("input") or gold.input_text),
                "output": str(item.get("output") or gold.output_text),
                "rationale": str(item.get("rationale") or gold.rationale or ""),
                "difficulty": str(item.get("difficulty") or gold.difficulty),
                "is_negative": bool(item.get("is_negative", gold.is_negative)),
                "varied_parameters": item.get("varied_parameters") or {"note": f"var_{i+1}"},
                "variation_index": i + 1,
            }
        )
    # pad with templates if model returned fewer
    if len(cleaned) < n:
        pad = _template_variations(gold, params, n - len(cleaned))
        for j, p in enumerate(pad):
            p["variation_index"] = len(cleaned) + j + 1
        cleaned.extend(pad)
    return cleaned


def run_synthesis(
    db: Session,
    *,
    owner_user_id: str,
    tenant_id: str,
    variations_per_gold: int | None = None,
    parameters: list[str] | None = None,
    gold_ids: list[str] | None = None,
    max_golds: int | None = None,
    use_llm: bool = True,
    progress_cb: Any | None = None,
) -> dict[str, Any]:
    """
    Create synthetic rows from the user's gold library.
    Respects user scope targets; stores forever under owner_user_id.
    """
    settings = get_settings()
    scope = get_or_create_scope(db, owner_user_id, tenant_id)
    n_var = variations_per_gold if variations_per_gold is not None else scope.variations_per_gold
    n_var = max(1, min(int(n_var), settings.max_variations_per_gold))

    if parameters is None:
        try:
            parameters = json.loads(scope.vary_parameters_json or "[]")
        except json.JSONDecodeError:
            parameters = ["tone", "difficulty"]
    allowed = {p["key"] for p in AVAILABLE_VARY_PARAMETERS}
    parameters = [p for p in parameters if p in allowed] or ["tone", "difficulty"]

    batch_cap = max_golds if max_golds is not None else settings.max_synthesis_batch_golds
    batch_cap = max(1, min(int(batch_cap), settings.max_synthesis_batch_golds))

    stats = library_stats(db, owner_user_id, tenant_id)

    q = db.query(m.GoldExample).filter_by(
        owner_user_id=owner_user_id,
        tenant_id=tenant_id,
        is_archived=False,
        verification_status="verified",
    )
    if gold_ids:
        q = q.filter(m.GoldExample.id.in_(gold_ids))
    gold_rows = q.order_by(m.GoldExample.created_at.desc()).all()
    if not gold_rows:
        return {
            "ok": False,
            "message": "No verified gold examples in your account yet. Promote curated data first.",
            "stats": stats,
        }

    # Per-gold remaining, not the account-wide 5×4 cap. New gold after a
    # previous project must still get its own variations.
    needy: list[tuple[m.GoldExample, int]] = []
    for gold in gold_rows:
        have = (
            db.query(m.SyntheticExample)
            .filter_by(gold_id=gold.id, is_archived=False)
            .count()
        )
        deficit = max(0, n_var - have)
        if deficit:
            needy.append((gold, deficit))
        if len(needy) >= batch_cap:
            break
    if not needy:
        return {
            "ok": False,
            "message": "Every verified gold row already has its variations.",
            "stats": stats,
        }

    needed_slots = sum(d for _, d in needy)
    verified_n = int(stats.get("gold_verified_count") or len(gold_rows))
    if int(scope.gold_target_count or 1) < verified_n:
        scope.gold_target_count = verified_n
        db.commit()
        stats = library_stats(db, owner_user_id, tenant_id)

    golds = [g for g, _ in needy]
    max_new = needed_slots
    deficits = {g.id: d for g, d in needy}

    run = m.SynthesisRun(
        id=_uid("srun_"),
        owner_user_id=owner_user_id,
        tenant_id=tenant_id,
        status="running",
        gold_requested=len(golds),
        gold_processed=0,
        variations_per_gold=n_var,
        synthesized_count=0,
        parameters_json=json.dumps(parameters),
    )
    db.add(run)
    db.commit()

    tenant = db.query(m.Tenant).filter_by(id=tenant_id).first()
    if tenant is not None:
        setattr(tenant, "_synth_cost_usd", 0.0)
    created = 0
    errors: list[str] = []

    def _progress(msg: str) -> None:
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:  # noqa: BLE001
                pass

    _progress(f"Synthesizing from {len(golds)} gold row(s) ({n_var} variation(s) each)…")
    for i, gold in enumerate(golds, start=1):
        if created >= max_new:
            break
        need = min(deficits.get(gold.id, n_var), max_new - created)
        try:
            if use_llm and tenant and get_settings().llm_provider != "none":
                try:
                    variations = _llm_variations(db, tenant, gold, parameters, need)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{gold.id}: LLM failed ({e}); used templates")
                    variations = _template_variations(gold, parameters, need)
            else:
                variations = _template_variations(gold, parameters, need)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{gold.id}: {e}")
            continue

        for v in variations:
            if created >= max_new:
                break
            db.add(
                m.SyntheticExample(
                    id=_uid("syn_"),
                    owner_user_id=owner_user_id,
                    tenant_id=tenant_id,
                    gold_id=gold.id,
                    topic=gold.topic,
                    input_text=v["input"],
                    output_text=v["output"],
                    rationale=v.get("rationale"),
                    difficulty=v.get("difficulty") or gold.difficulty,
                    is_negative=bool(v.get("is_negative")),
                    variation_index=int(v.get("variation_index") or 1),
                    varied_parameters_json=json.dumps(v.get("varied_parameters") or {}),
                    synthesis_run_id=run.id,
                )
            )
            created += 1
        run.gold_processed += 1
        run.synthesized_count = created
        db.commit()
        _progress(
            f"Gold {i}/{len(golds)} done — {created} synthetic row(s) so far"
            + (f" · last: {errors[-1][:80]}" if errors else "")
        )

    run.status = "completed"
    run.finished_at = _now()
    if errors:
        run.error = "; ".join(errors[:20])
        run.notes = f"Completed with {len(errors)} warnings"
    db.commit()

    openrouter_cost = float(getattr(tenant, "_synth_cost_usd", 0.0) or 0.0) if tenant else 0.0
    return {
        "ok": True,
        "run_id": run.id,
        "gold_processed": run.gold_processed,
        "synthesized_count": created,
        "variations_per_gold": n_var,
        "parameters": parameters,
        "warnings": errors,
        "stats": library_stats(db, owner_user_id, tenant_id),
        "openrouter_cost_usd": round(openrouter_cost, 6),
        "apify_cost_usd": 0.0,
        "cost_usd": round(openrouter_cost, 6),
        "message": f"Created {created} synthesized examples from {run.gold_processed} gold rows. Stored forever in your account.",
    }
