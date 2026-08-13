"""Riu conversational helper API."""

from __future__ import annotations

import io
import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from helix.api.deps import get_current_user
from helix.db import models as m
from helix.db.session import get_db
from helix.services import riu as riu_service
from helix.services.user_gold_upload import MAX_ZIP_BYTES, import_zip_as_gold
from helix.services.user_material_upload import import_material_zip_as_trainable

router = APIRouter(prefix="/api/t/{slug}/riu", tags=["riu"])


def _tenant_for(user: m.User, slug: str, db: Session) -> m.Tenant:
    tenant = db.query(m.Tenant).filter_by(slug=slug, is_active=True).first()
    if not tenant:
        raise HTTPException(404, "Workspace not found")
    if not user.is_superadmin:
        mem = (
            db.query(m.Membership)
            .filter_by(user_id=user.id, tenant_id=tenant.id)
            .first()
        )
        if not mem:
            raise HTTPException(403, "Forbidden")
    return tenant


class RiuMessageIn(BaseModel):
    message: str = Field(min_length=1, max_length=8000)


@router.get("/session")
def get_session(
    slug: str,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    row = riu_service.get_or_create_session(
        db, user_id=user.id, tenant_id=tenant.id
    )
    return riu_service.session_to_dict(row)


@router.post("/session")
def new_session(
    slug: str,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    row = riu_service.create_session(db, user_id=user.id, tenant_id=tenant.id)
    return riu_service.session_to_dict(row)


@router.post("/message")
def post_message(
    slug: str,
    body: RiuMessageIn,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    row = riu_service.get_or_create_session(
        db, user_id=user.id, tenant_id=tenant.id
    )
    try:
        return riu_service.handle_user_message(
            db, tenant=tenant, user=user, session=row, text=body.message
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Riu error: {e}") from e


@router.post("/upload-gold-zip")
async def riu_upload_gold_zip(
    slug: str,
    file: UploadFile = File(...),
    topic: str = Form("user_upload"),
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """
    Bring-your-own labeled zip during Riu setup.
    Saves as gold-format rows and updates the active Riu session state.
    """
    tenant = _tenant_for(user, slug, db)
    name = (file.filename or "upload.zip").lower()
    if not name.endswith(".zip"):
        raise HTTPException(400, "Please upload a .zip file")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Empty file")
    if len(raw) > MAX_ZIP_BYTES:
        raise HTTPException(
            400, f"Zip too large (max {MAX_ZIP_BYTES // (1024 * 1024)} MB)"
        )
    out = import_zip_as_gold(
        db,
        owner_user_id=user.id,
        tenant_id=tenant.id,
        fileobj=io.BytesIO(raw),
        filename=file.filename or "upload.zip",
        default_topic=(topic or "user_upload").strip()[:80] or "user_upload",
        enforce_cap=True,
    )
    if not out.get("ok"):
        raise HTTPException(400, out.get("error") or "Import failed")

    # Annotate active Riu session so setup can continue
    session = riu_service.get_or_create_session(
        db, user_id=user.id, tenant_id=tenant.id
    )
    try:
        state = json.loads(session.state_json or "{}")
    except json.JSONDecodeError:
        state = {}
    if not isinstance(state, dict):
        state = {}
    state["has_own_data"] = True
    state["own_data_awaiting_upload"] = False
    state["own_data_uploaded"] = True
    state["own_data_count"] = int(state.get("own_data_count") or 0) + int(
        out.get("created") or 0
    )
    state["own_data_batch_id"] = out.get("upload_batch_id")
    session.state_json = json.dumps(state)
    # Append a short assistant note into the chat transcript
    try:
        messages = json.loads(session.messages_json or "[]")
    except json.JSONDecodeError:
        messages = []
    if not isinstance(messages, list):
        messages = []
    messages.append(
        {
            "id": riu_service._uid("msg_"),
            "role": "assistant",
            "name": riu_service.RIU_NAME,
            "content": (
                f"Received your zip — saved **{out.get('created', 0)}** gold-format "
                f"example(s). They're in **My data** (Export my uploads) and ready "
                f"for Double Helix later.\n\n"
                "Reply **done** or **continue** to talk about other materials "
                "(scripts, rulebooks…), or upload another labeled zip."
            ),
            "phase": session.phase or "own_data",
            "progress": 85,
            "ts": riu_service._now().isoformat(),
        }
    )
    session.messages_json = json.dumps(messages)
    if (session.phase or "") in {"goals", "own_data", "greet", "discover", "formats"}:
        session.phase = "own_data"
    db.commit()
    db.refresh(session)
    return {
        **out,
        "session": riu_service.session_to_dict(session),
    }


@router.post("/upload-materials-zip")
async def riu_upload_materials_zip(
    slug: str,
    file: UploadFile = File(...),
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Unlabeled materials zip during Riu setup → converted trainable gold rows."""
    tenant = _tenant_for(user, slug, db)
    name = (file.filename or "materials.zip").lower()
    if not name.endswith(".zip"):
        raise HTTPException(400, "Please upload a .zip file")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Empty file")
    if len(raw) > MAX_ZIP_BYTES:
        raise HTTPException(
            400, f"Zip too large (max {MAX_ZIP_BYTES // (1024 * 1024)} MB)"
        )
    domain = ""
    try:
        state0 = json.loads(
            (
                riu_service.get_or_create_session(
                    db, user_id=user.id, tenant_id=tenant.id
                ).state_json
                or "{}"
            )
        )
        if isinstance(state0, dict):
            domain = str(state0.get("domain") or state0.get("project_name") or "")
    except Exception:  # noqa: BLE001
        pass

    out = import_material_zip_as_trainable(
        db,
        owner_user_id=user.id,
        tenant_id=tenant.id,
        fileobj=io.BytesIO(raw),
        filename=file.filename or "materials.zip",
        domain=domain,
        enforce_cap=True,
    )
    if not out.get("ok"):
        raise HTTPException(400, out.get("error") or "Import failed")

    session = riu_service.get_or_create_session(
        db, user_id=user.id, tenant_id=tenant.id
    )
    try:
        state = json.loads(session.state_json or "{}")
    except json.JSONDecodeError:
        state = {}
    if not isinstance(state, dict):
        state = {}
    state["has_materials"] = True
    state["materials_awaiting_upload"] = False
    state["materials_uploaded"] = True
    state["materials_count"] = int(state.get("materials_count") or 0) + int(
        out.get("created") or 0
    )
    state["materials_batch_id"] = out.get("upload_batch_id")
    session.state_json = json.dumps(state)
    try:
        messages = json.loads(session.messages_json or "[]")
    except json.JSONDecodeError:
        messages = []
    if not isinstance(messages, list):
        messages = []
    messages.append(
        {
            "id": riu_service._uid("msg_"),
            "role": "assistant",
            "name": riu_service.RIU_NAME,
            "content": (
                f"Converted your materials into **{out.get('created', 0)}** trainable "
                f"gold-format row(s). Download from **My data · Export materials**. "
                "They combine with curated gold and your labeled uploads.\n\n"
                "Reply **done** or **continue** for the **pricing estimate** and "
                "final summary, or upload another materials zip."
            ),
            "phase": "materials",
            "progress": 90,
            "ts": riu_service._now().isoformat(),
        }
    )
    session.messages_json = json.dumps(messages)
    session.phase = "materials"
    db.commit()
    db.refresh(session)
    return {
        **out,
        "session": riu_service.session_to_dict(session),
    }
