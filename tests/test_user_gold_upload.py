"""BYO zip → gold-format import for Double Helix."""

from __future__ import annotations

import io
import json
import uuid
import zipfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from helix.db import models as m
from helix.db.models import Base
from helix.services.user_gold_upload import (
    USER_UPLOAD_SOURCE_KIND,
    extract_pairs_from_zip,
    import_zip_as_gold,
)


def _uid(p: str = "") -> str:
    return f"{p}{uuid.uuid4().hex[:12]}"


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _seed(db):
    tid, uid = _uid("ten_"), _uid("usr_")
    db.add(
        m.Tenant(
            id=tid, slug=f"s-{tid[-6:]}", name="T", plan="starter", is_active=True
        )
    )
    db.add(
        m.User(
            id=uid,
            email=f"{uid}@ex.com",
            hashed_password="x",
            is_active=True,
            email_verified=True,
            admin_approved=True,
            password_set=True,
        )
    )
    db.commit()
    return tid, uid


def _zip_with_jsonl(rows: list[dict]) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        body = "\n".join(json.dumps(r) for r in rows)
        zf.writestr("examples.jsonl", body)
    buf.seek(0)
    return buf


def test_extract_jsonl_pairs():
    buf = _zip_with_jsonl(
        [
            {"input": "When does PTO start?", "output": "After the first full month."},
            {"question": "Q2", "answer": "A2"},
        ]
    )
    rows, meta = extract_pairs_from_zip(buf)
    assert meta["pairs_found"] == 2
    assert rows[0]["input"].startswith("When does")
    assert rows[1]["output"] == "A2"


def test_import_zip_creates_user_upload_gold(db):
    tid, uid = _seed(db)
    buf = _zip_with_jsonl(
        [
            {
                "input": "How do I reset my password?",
                "output": "Use Forgot password on the login page.",
                "topic": "auth",
            },
            {
                "prompt": "Ship times?",
                "completion": "3–5 business days standard.",
            },
        ]
    )
    out = import_zip_as_gold(
        db,
        owner_user_id=uid,
        tenant_id=tid,
        fileobj=buf,
        filename="my_data.zip",
    )
    assert out["ok"] is True
    assert out["created"] == 2
    assert out["source_kind"] == USER_UPLOAD_SOURCE_KIND
    rows = (
        db.query(m.GoldExample)
        .filter_by(owner_user_id=uid, source_kind=USER_UPLOAD_SOURCE_KIND)
        .all()
    )
    assert len(rows) == 2
    meta = json.loads(rows[0].metadata_json or "{}")
    assert meta.get("for_double_helix") is True
    assert meta.get("upload_batch_id")


def test_empty_zip_fails_gracefully(db):
    tid, uid = _seed(db)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "no pairs here")
    buf.seek(0)
    out = import_zip_as_gold(
        db, owner_user_id=uid, tenant_id=tid, fileobj=buf
    )
    assert out["ok"] is False
    assert out["created"] == 0


def test_heuristic_own_data_phase():
    from helix.services.riu import _heuristic_turn

    # After goals → own_data question already answered yes
    turn = _heuristic_turn("yes I have a zip", {}, "own_data")
    assert turn["phase"] == "own_data"
    assert turn["state_patch"].get("has_own_data") is True
    assert turn["state_patch"].get("own_data_awaiting_upload") is True

    # Skip labeled → materials (unlabeled) phase, not jump to confirm
    turn2 = _heuristic_turn("skip", {}, "own_data")
    assert turn2["phase"] == "materials"
    assert turn2["state_patch"].get("own_data_awaiting_upload") is False
