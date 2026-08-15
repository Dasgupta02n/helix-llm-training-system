"""Shared live-process signals: heartbeat age + stuck vs working."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

QUIET_AFTER_SEC = 15
STALE_AFTER_SEC = 45


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def heartbeat_fields(
    updated_at: datetime | None,
    *,
    running: bool,
    started_at: datetime | None = None,
) -> dict[str, Any]:
    now = _now()
    upd = _aware(updated_at)
    start = _aware(started_at)
    age = None if upd is None else max(0, int((now - upd).total_seconds()))
    elapsed = None if start is None else max(0, int((now - start).total_seconds()))
    if not running:
        state = "idle"
        label = "Not running"
    elif age is None:
        state = "live"
        label = "Just started"
    elif age <= QUIET_AFTER_SEC:
        state = "live"
        label = f"Live · last signal {age}s ago"
    elif age <= STALE_AFTER_SEC:
        state = "quiet"
        label = f"Still working · no new step for {age}s (heartbeat keeps this honest)"
    else:
        state = "stale"
        label = f"No new step for {age}s — may be stuck or waiting on a slow provider"
    return {
        "heartbeat_age_seconds": age,
        "elapsed_seconds": elapsed,
        "live_state": state,
        "live_label": label,
    }


# Step-log copy must not name vendors. Longer phrases first so they are not
# half-replaced by a shorter name match.
_VENDOR_SUBS = (
    (re.compile(r"\(Apify/code\)", re.I), ""),
    (re.compile(r"Apify/code", re.I), "the gather step"),
    (re.compile(r"\bOpenRouter\b", re.I), "the model"),
    (re.compile(r"\bApify\b", re.I), "gather"),
    (re.compile(r"\bRunPod\b", re.I), "training"),
    (re.compile(r"\bHugging\s*Face\b", re.I), "model storage"),
    (re.compile(r"\bHuggingFace\b", re.I), "model storage"),
    (re.compile(r"\bHostinger\b", re.I), "the server"),
    (re.compile(r"\bHF Hub\b", re.I), "model storage"),
    (re.compile(r"\bOR \$", re.I), "model $"),
)


def public_activity_text(msg: str | None) -> str:
    out = (msg or "").strip()
    if not out:
        return ""
    for pat, repl in _VENDOR_SUBS:
        out = pat.sub(repl, out)
    return re.sub(r" {2,}", " ", out).strip()
