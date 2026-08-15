"""Unlabeled materials (scripts, rulebooks, notes) → trainable gold-format rows."""

from __future__ import annotations

import html
import io
import re
import uuid
import zipfile
from typing import Any, BinaryIO

from sqlalchemy.orm import Session

from helix.db import models as m
from helix.services.library import add_gold_example, gold_to_dict

USER_MATERIAL_SOURCE_KIND = "user_material"

MAX_ZIP_BYTES = 25 * 1024 * 1024
MAX_PAIRS = 2000
MAX_FILES = 150
CHUNK_CHARS = 1100
MIN_CHUNK_CHARS = 80


def _uid(prefix: str = "mat_") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _strip_html(raw: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", raw)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"[ \t]+", " ", text)


def _normalize_text(raw: str) -> str:
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_sections(text: str) -> list[str]:
    """Split freeform docs into training-sized sections."""
    text = _normalize_text(text)
    if not text:
        return []
    # Prefer markdown ATX headings at line starts — keep each heading block separate
    heading_parts = re.split(r"(?m)(?=^#{1,3}\s+)", text)
    heading_parts = [p.strip() for p in heading_parts if p and p.strip()]
    if len(heading_parts) > 1:
        chunks: list[str] = []
        for p in heading_parts:
            if len(p) <= CHUNK_CHARS:
                if len(p) >= MIN_CHUNK_CHARS:
                    chunks.append(p)
            else:
                for i in range(0, len(p), CHUNK_CHARS):
                    piece = p[i : i + CHUNK_CHARS].strip()
                    if len(piece) >= MIN_CHUNK_CHARS:
                        chunks.append(piece)
        return chunks or [text[:CHUNK_CHARS]]

    # Blank-line paragraphs, then pack up to CHUNK_CHARS
    parts = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
    if not parts:
        parts = [text]
    chunks = []
    buf = ""
    for p in parts:
        if len(buf) + len(p) + 2 <= CHUNK_CHARS:
            buf = f"{buf}\n\n{p}".strip() if buf else p
        else:
            if buf and len(buf) >= MIN_CHUNK_CHARS:
                chunks.append(buf)
            if len(p) <= CHUNK_CHARS:
                buf = p
            else:
                for i in range(0, len(p), CHUNK_CHARS):
                    piece = p[i : i + CHUNK_CHARS].strip()
                    if len(piece) >= MIN_CHUNK_CHARS:
                        chunks.append(piece)
                buf = ""
    if buf and len(buf) >= MIN_CHUNK_CHARS:
        chunks.append(buf)
    elif buf and not chunks:
        chunks.append(buf)
    return chunks


def _topic_from_name(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    base = re.sub(r"\.[^.]+$", "", base)
    s = re.sub(r"[^a-zA-Z0-9_\-]+", "_", base.lower()).strip("_")
    return (s or "user_material")[:80]


def _prompt_for_chunk(
    *,
    domain: str,
    filename: str,
    chunk: str,
    index: int,
) -> tuple[str, str]:
    """Build a gold-format (input, output) pair from unlabeled material."""
    first = chunk.split("\n", 1)[0].strip()[:120]
    domain_bit = domain or "this domain"
    # input = instruction the model should follow; output = material knowledge
    inp = (
        f"You are trained on internal materials for {domain_bit}. "
        f"Using the knowledge from “{filename}” (section {index + 1}), "
        f"produce a clear, faithful answer or scripted response covering:\n"
        f"{first}"
    )
    out = chunk.strip()[:4000]
    return inp, out


def extract_text_files_from_zip(
    fileobj: BinaryIO,
) -> list[tuple[str, str]]:
    """Return list of (filename, text) from zip."""
    try:
        zf = zipfile.ZipFile(fileobj)
    except zipfile.BadZipFile as e:
        raise ValueError("Not a valid zip file") from e

    out: list[tuple[str, str]] = []
    with zf:
        names = [
            n
            for n in zf.namelist()
            if not n.endswith("/") and not n.startswith("__MACOSX")
        ]
        for name in names[:MAX_FILES]:
            lower = name.lower()
            if not any(
                lower.endswith(ext)
                for ext in (
                    ".txt",
                    ".md",
                    ".markdown",
                    ".html",
                    ".htm",
                    ".csv",
                    ".json",
                    ".jsonl",
                    ".rst",
                    ".log",
                )
            ):
                continue
            try:
                raw = zf.read(name)
            except Exception:  # noqa: BLE001
                continue
            if len(raw) > 4 * 1024 * 1024:
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("latin-1", errors="replace")
            if lower.endswith((".html", ".htm")):
                text = _strip_html(text)
            text = _normalize_text(text)
            if len(text) >= MIN_CHUNK_CHARS:
                out.append((name, text))
    return out


def convert_materials_to_pairs(
    files: list[tuple[str, str]],
    *,
    domain: str = "",
    max_pairs: int = MAX_PAIRS,
) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    for fname, text in files:
        topic = _topic_from_name(fname)
        sections = _split_sections(text)
        for i, sec in enumerate(sections):
            if len(pairs) >= max_pairs:
                return pairs
            inp, out = _prompt_for_chunk(
                domain=domain, filename=fname, chunk=sec, index=i
            )
            pairs.append(
                {
                    "topic": topic,
                    "input": inp,
                    "output": out,
                    "rationale": (
                        f"Auto-converted from unlabeled material “{fname}” "
                        f"(section {i + 1}) into gold-format training pairs."
                    ),
                    "difficulty": "moderate",
                    "is_negative": False,
                    "source_file": fname,
                    "section_index": i,
                }
            )
    return pairs


def import_material_zip_as_trainable(
    db: Session,
    *,
    owner_user_id: str,
    tenant_id: str,
    fileobj: BinaryIO,
    filename: str = "materials.zip",
    domain: str = "",
    enforce_cap: bool = True,
) -> dict[str, Any]:
    """
    Zip of freeform docs → gold-format trainable rows (source_kind=user_material).
    Also stores original text in corpus for mining evidence when useful.
    """
    batch_id = _uid("mby_")
    files = extract_text_files_from_zip(fileobj)
    if not files:
        return {
            "ok": False,
            "error": (
                "No readable text documents found. Include .txt, .md, .html, "
                ".csv, or .json files (scripts, rulebooks, notes, formulas)."
            ),
            "created": 0,
            "upload_batch_id": batch_id,
        }

    pairs = convert_materials_to_pairs(files, domain=domain)
    if not pairs:
        return {
            "ok": False,
            "error": "Documents were too short to convert into training pairs.",
            "created": 0,
            "upload_batch_id": batch_id,
            "files_read": len(files),
        }

    # Optional: keep raw docs in corpus for plan-scoped mining
    try:
        from helix.services.corpus import add_paste

        for fname, text in files[:20]:
            add_paste(
                db,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                title=f"[material] {fname}",
                content=text[:50000],
                category="user_material",
            )
    except Exception:  # noqa: BLE001
        pass

    created = 0
    skipped = 0
    samples: list[dict] = []
    for i, row in enumerate(pairs):
        g = add_gold_example(
            db,
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            topic=row["topic"],
            input_text=row["input"],
            output_text=row["output"],
            rationale=row.get("rationale"),
            difficulty=row.get("difficulty") or "moderate",
            is_negative=False,
            source_kind=USER_MATERIAL_SOURCE_KIND,
            source_ref=f"user_material:{batch_id}:{i}",
            verification_status="verified",
            metadata={
                "origin": "user_material",
                "upload_batch_id": batch_id,
                "upload_filename": (filename or "materials.zip")[:200],
                "source_file": row.get("source_file"),
                "section_index": row.get("section_index"),
                "for_double_helix": True,
                "converted_from_unlabeled": True,
                "is_seed": False,
            },
            enforce_cap=enforce_cap,
            skip_near_duplicate=True,
        )
        if g is None:
            skipped += 1
            continue
        created += 1
        if len(samples) < 2:
            samples.append(gold_to_dict(g))

    return {
        "ok": True,
        "created": created,
        "skipped": skipped,
        "files_read": len(files),
        "pairs_built": len(pairs),
        "upload_batch_id": batch_id,
        "source_kind": USER_MATERIAL_SOURCE_KIND,
        "for_double_helix": True,
        "samples": samples,
        "message": (
            f"Converted {len(files)} document(s) into {created} trainable "
            f"gold-format row(s) (batch {batch_id}). Download from My data · "
            "Export materials. Usable with curated gold and your labeled uploads."
        ),
    }


def estimate_setup_pricing(state: dict[str, Any]) -> dict[str, Any]:
    """Rough all-in pricing estimate for Riu confirmation step."""
    from helix.services.cost_tracking import (
        DOUBLE_HELIX_TRAINING_COST_MAX_USD,
        DOUBLE_HELIX_TRAINING_COST_MIN_USD,
        GOLD_COST_CAP_USD_PER_1000,
        estimate_units_usd,
        format_row_rate,
        gold_spend_cap_usd,
    )

    try:
        gold_target = int(state.get("gold_target") or 5000)
    except (TypeError, ValueError):
        gold_target = 5000
    try:
        batches = int(state.get("total_batches") or 2)
        bsize = int(state.get("batch_size") or 5)
    except (TypeError, ValueError):
        batches, bsize = 2, 5
    try:
        q = int(state.get("quality_mode") or 2)
    except (TypeError, ValueError):
        q = 2

    first_job_units = max(1, batches * bsize)
    with_lo, with_hi = estimate_units_usd(gold_target, no_corpus=False)
    none_lo, none_hi = estimate_units_usd(gold_target, no_corpus=True)
    target_mining_usd = with_hi
    # Quality mode soft multiplier for narrative (cap is still hard)
    mode_note = {
        1: "best quality (higher judge cost)",
        2: "high quality (default)",
        3: "balanced",
        4: "lowest cost lean mode",
    }.get(q, "high quality")

    own = int(state.get("own_data_count") or 0)
    mats = int(state.get("materials_count") or 0)
    corpus_docs = int(state.get("corpus_docs") or 0)
    corpus_units = int(state.get("corpus_units") or 0)
    attached = int(state.get("attached_support") or 0)
    if attached <= 0:
        attached = corpus_units + own + mats
    no_sources = corpus_docs <= 0 and attached <= 0
    first_job_cap = gold_spend_cap_usd(first_job_units, no_corpus=no_sources)

    honest_lines: list[str] = []
    if corpus_docs or corpus_units:
        honest_lines.append(
            f"Attached corpus: **{corpus_docs}** doc(s) → about **{corpus_units}** "
            "trainable pairs we can extract."
        )
    else:
        honest_lines.append(
            "No corpus is attached for this plan. A **large** mining job "
            "(more than 10 units) will be blocked until you paste source material "
            "under My data."
        )
    if gold_target > attached:
        honest_lines.append(
            f"You asked for **{gold_target:,}** gold. Your attached data supports "
            f"about **{attached:,}** pairs (corpus {corpus_units} + labeled {own} + "
            f"materials {mats}). I will **not** pretend we can mint {gold_target:,} "
            f"from that. With your sources, {gold_target:,} gold is about "
            f"**${with_lo:,.0f}–${with_hi:,.0f}** ({format_row_rate()})."
        )
    else:
        honest_lines.append(
            f"Requested **{gold_target:,}** gold is within attached support "
            f"(~{attached:,} pairs)."
        )

    can_start_requested = not (
        gold_target > 10 and corpus_docs <= 0 and attached <= 0
    )
    if not can_start_requested:
        honest_lines.append(
            f"Web-research-only starts with **{first_job_units}** examples "
            f"(cap **${first_job_cap:.2f}**, {format_row_rate(no_corpus=True)}). "
            "Then we review those 10 one-by-one, generate **10 more** as proof, "
            "and only then scale. "
            f"No-source scale for **{gold_target:,}** is about "
            f"**${none_lo:,.0f}–${none_hi:,.0f}**. Type **start 10**."
        )
    else:
        honest_lines.append(
            f"First job hard cap: **${first_job_cap:.2f}** for **{first_job_units}** "
            f"units ({batches}×{bsize}). Type **start** to queue that job."
        )

    return {
        "gold_target": gold_target,
        "quality_mode": q,
        "quality_label": mode_note,
        "first_job_batches": batches,
        "first_job_batch_size": bsize,
        "first_job_unit_cap_usd": first_job_cap,
        "mining_target_all_in_usd": target_mining_usd,
        "cost_per_1000_gold_usd": GOLD_COST_CAP_USD_PER_1000,
        "double_helix_training_usd_min": DOUBLE_HELIX_TRAINING_COST_MIN_USD,
        "double_helix_training_usd_max": DOUBLE_HELIX_TRAINING_COST_MAX_USD,
        "your_labeled_rows": own,
        "your_material_rows": mats,
        "corpus_docs": corpus_docs,
        "corpus_units": corpus_units,
        "attached_support": attached,
        "requested_exceeds_corpus": gold_target > attached,
        "can_start_requested": can_start_requested,
        "first_job_units": first_job_units,
        "summary_lines": [
            "Usage on your counter is **2 ×** what the underlying services bill "
            "(model, gather, training compute, and any other metered service). "
            f"Typical gold with sources still lands around {format_row_rate()}. "
            f"**{gold_target:,}** gold ≈ **${with_lo:,.0f}–${with_hi:,.0f}** "
            "if we can actually produce it.",
            f"First job: **{first_job_units}** units, spend cap **${first_job_cap:.2f}** "
            f"({batches}×{bsize}). Jobs pause if trajectory would exceed the cap.",
            f"Quality mode **{q}** — {mode_note}. Time for this first job is "
            f"**one small batch (minutes, not hours)** — not a 3–6 hour 5,000-row run.",
            *honest_lines,
            (
                f"Your uploads already in library: **{own}** labeled gold + **{mats}** "
                "material-converted rows (downloadable; Double Helix ready)."
                if (own or mats)
                else "No labeled/material uploads yet."
            ),
            f"Optional later: **Double Helix** training ~**"
            f"${DOUBLE_HELIX_TRAINING_COST_MIN_USD:.0f}–"
            f"${DOUBLE_HELIX_TRAINING_COST_MAX_USD:.0f}** per job (not charged now).",
        ],
    }


def format_official_estimate(pricing: dict[str, Any], *, project: str = "") -> str:
    """User-facing block. Riu must show this instead of invented $ / hour quotes."""
    lines = list(pricing.get("summary_lines") or [])
    head = "Official Helix estimate (same per-row rates the jobs use):"
    if project:
        head = f"{head}\n• Project: **{project}**"
    body = "\n".join(f"• {ln}" if not ln.startswith("•") else ln for ln in lines)
    return f"{head}\n{body}"
