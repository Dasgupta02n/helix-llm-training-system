"""Unlabeled materials → trainable gold pairs + Riu materials/pricing flow."""

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
from helix.services.riu import _heuristic_turn
from helix.services.user_material_upload import (
    USER_MATERIAL_SOURCE_KIND,
    convert_materials_to_pairs,
    estimate_setup_pricing,
    import_material_zip_as_trainable,
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


def test_convert_script_sections():
    script = """# Opening

Hi, thanks for taking my call. I'm calling about your shipping options.

# Objection handling

If they say it's too expensive, explain the value of faster delivery and warranties.

# Close

Ask for the order confirmation number and thank them.
"""
    pairs = convert_materials_to_pairs(
        [("tele_sales_script.md", script)], domain="tele sales AI"
    )
    assert len(pairs) >= 2
    assert pairs[0]["input"]
    assert pairs[0]["output"]
    assert "tele" in pairs[0]["topic"] or "script" in pairs[0]["topic"]


def test_import_materials_zip(db):
    tid, uid = _seed(db)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "rulebook.txt",
            "Rule 1: The referee must stop play on foul.\n\n"
            "Rule 2: Yellow card is a caution. Red card is dismissal.\n\n"
            "Rule 3: Extra time is two periods of 15 minutes.",
        )
    buf.seek(0)
    out = import_material_zip_as_trainable(
        db,
        owner_user_id=uid,
        tenant_id=tid,
        fileobj=buf,
        filename="rules.zip",
        domain="game referee",
    )
    assert out["ok"] is True
    assert out["created"] >= 1
    assert out["source_kind"] == USER_MATERIAL_SOURCE_KIND
    rows = (
        db.query(m.GoldExample)
        .filter_by(owner_user_id=uid, source_kind=USER_MATERIAL_SOURCE_KIND)
        .all()
    )
    assert len(rows) >= 1
    meta = json.loads(rows[0].metadata_json or "{}")
    assert meta.get("converted_from_unlabeled") is True
    assert meta.get("for_double_helix") is True


def test_estimate_pricing_includes_caps():
    p = estimate_setup_pricing(
        {
            "gold_target": 1000,
            "batch_size": 5,
            "total_batches": 2,
            "quality_mode": 2,
            "own_data_count": 10,
            "materials_count": 5,
        }
    )
    assert p["mining_target_all_in_usd"] == 35.0
    assert p["first_job_unit_cap_usd"] == 0.35  # 10 units * 35/1000
    assert p["your_labeled_rows"] == 10
    assert p["your_material_rows"] == 5
    assert p["requested_exceeds_corpus"] is True
    assert len(p["summary_lines"]) >= 3


def test_heuristic_own_data_to_materials_to_confirm():
    # Skip labeled → materials question
    t1 = _heuristic_turn("no", {}, "own_data")
    assert t1["phase"] == "materials"
    assert "script" in t1["reply"].lower() or "rulebook" in t1["reply"].lower()

    # Skip materials → pricing/confirm
    t2 = _heuristic_turn("skip", {"gold_target": 1000, "batch_size": 5, "total_batches": 1}, "materials")
    assert t2["phase"] == "confirm"
    assert "pricing" in t2["reply"].lower() or "$" in t2["reply"]
    assert t2["state_patch"].get("pricing_estimate")

    # Yes materials → upload prompt
    t3 = _heuristic_turn("yes I have a rulebook", {}, "materials")
    assert t3["phase"] == "materials"
    assert t3["state_patch"].get("materials_awaiting_upload") is True
