"""Gold goal cap must not count rejected rows (corpus cap_or_null regression)."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from helix.db import models as m
from helix.db.models import Base
from helix.services.library import (
    add_gold_example,
    count_gold_toward_cap,
    get_or_create_scope,
    update_scope,
)


def _uid(p: str = "") -> str:
    return f"{p}{uuid.uuid4().hex[:12]}"


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
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
    db.add(m.Tenant(id=tid, slug=f"s-{tid[-6:]}", name="T", plan="starter", is_active=True))
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
    db.add(m.Membership(id=_uid("mem_"), user_id=uid, tenant_id=tid, role="owner"))
    db.commit()
    scope = get_or_create_scope(db, uid, tid)
    update_scope(db, uid, tid, gold_target_count=10)
    return tid, uid


def test_cap_ignores_rejected_rows(db):
    """goal=10 with 5 verified + 5 rejected must still allow new verified gold."""
    tid, uid = _seed(db)
    for i in range(5):
        db.add(
            m.GoldExample(
                id=_uid("gold_"),
                owner_user_id=uid,
                tenant_id=tid,
                topic="seed_topic",
                input_text=f"seed in {i}",
                output_text=f"seed out {i} with enough length for training",
                source_kind="seed",
                verification_status="verified",
            )
        )
    for i in range(5):
        db.add(
            m.GoldExample(
                id=_uid("gold_"),
                owner_user_id=uid,
                tenant_id=tid,
                topic="refunds",
                input_text=f"bad in {i}",
                output_text=f"I don't have enough verified evidence {i}",
                source_kind="pipeline",
                verification_status="rejected",
                metadata_json=json.dumps(
                    {"rejection_reasons": ["support_refuses_or_demands_internal_docs"]}
                ),
            )
        )
    db.commit()

    total = (
        db.query(m.GoldExample)
        .filter_by(owner_user_id=uid, tenant_id=tid, is_archived=False)
        .count()
    )
    assert total == 10
    assert count_gold_toward_cap(db, owner_user_id=uid, tenant_id=tid) == 5

    g = add_gold_example(
        db,
        owner_user_id=uid,
        tenant_id=tid,
        topic="late_delivery",
        input_text="Customer: late delivery order",
        output_text=(
            "Sorry about the late delivery. Reply with your order ID and "
            "I'll check tracking and a credit for you right away."
        ),
        source_kind="corpus",
        source_ref="corpus:test:u0",
        verification_status="verified",
        enforce_cap=True,
        skip_near_duplicate=True,
    )
    assert g is not None, "cap must not treat rejected rows as filling the goal"
    assert g.source_kind == "corpus"
    assert count_gold_toward_cap(db, owner_user_id=uid, tenant_id=tid) == 6


def test_cap_still_enforced_when_verified_at_target(db):
    tid, uid = _seed(db)
    for i in range(10):
        db.add(
            m.GoldExample(
                id=_uid("gold_"),
                owner_user_id=uid,
                tenant_id=tid,
                topic="t",
                input_text=f"in {i} unique content here",
                output_text=f"out {i} unique content with enough length",
                source_kind="pipeline",
                verification_status="verified",
            )
        )
    db.commit()
    g = add_gold_example(
        db,
        owner_user_id=uid,
        tenant_id=tid,
        topic="t",
        input_text="new in",
        output_text="new out with enough length to pass",
        enforce_cap=True,
    )
    assert g is None


def test_corpus_unit_writes_when_rejected_meet_raw_count_goal(db):
    """
    Regression: goal=10, library has 10 rows (5 verified seed + 5 rejected).
    Raw count == goal but verified count == 5 → corpus unit must still write.
    """
    from helix.services.corpus import write_corpus_units_as_gold

    tid, uid = _seed(db)
    for i in range(5):
        db.add(
            m.GoldExample(
                id=_uid("gold_"),
                owner_user_id=uid,
                tenant_id=tid,
                topic="campaign_strategy",
                input_text=f"Campaign brief seed {i}",
                output_text=f"Seed output for demo row {i} with length",
                source_kind="seed",
                verification_status="verified",
            )
        )
    for i in range(5):
        db.add(
            m.GoldExample(
                id=_uid("gold_"),
                owner_user_id=uid,
                tenant_id=tid,
                topic="support_reply",
                input_text=f"bad support in {i}",
                output_text=f"Based on the available documentation: dump {i}",
                source_kind="pipeline",
                verification_status="rejected",
            )
        )
    db.commit()
    raw = (
        db.query(m.GoldExample)
        .filter_by(owner_user_id=uid, tenant_id=tid, is_archived=False)
        .count()
    )
    assert raw == 10
    assert count_gold_toward_cap(db, owner_user_id=uid, tenant_id=tid) == 5

    brief = {
        "domain": "food delivery customer support",
        "mission": "Help with late delivery",
        "categories": ["late delivery", "refunds"],
    }
    units = [
        {
            "source_ref": "corpus:corp_test:u0",
            "title": "My delivery is late",
            "evidence": (
                "If your order is more than 30 minutes late, open Help in the app. "
                "We can check tracking and often issue a credit. Share your order ID."
            ),
            "category": "late delivery",
            "url": "corpus://test/u0",
            "corpus_id": "corp_test",
        }
    ]
    out = write_corpus_units_as_gold(
        db,
        tenant_id=tid,
        owner_user_id=uid,
        brief=brief,
        units=units,
        batch_size=5,
        tenant=None,
    )
    assert out["corpus_gold_new"] == 1, out
    assert not any(d.get("status") == "cap_or_null" for d in out.get("details") or [])
    assert not any(d.get("status") == "goal_cap_reached" for d in out.get("details") or [])
    g = (
        db.query(m.GoldExample)
        .filter_by(owner_user_id=uid, tenant_id=tid, source_kind="corpus")
        .first()
    )
    assert g is not None
    assert g.verification_status == "verified"
