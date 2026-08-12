"""Reject gold poisoned by cross-plan corpus contamination."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from helix.db import models as m
from helix.db.models import Base
from helix.services.gold_quality import (
    backfill_reject_cross_domain_gold,
    cross_domain_reject_reasons,
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


def test_detect_food_under_hr_topic():
    reasons = cross_domain_reject_reasons(
        topic="pto_accrual_and_carryover",
        input_text=(
            "[corpus] Q: My order arrived late, what can I do?\n"
            "Internal notes: late delivery refund order ID"
        ),
        output_text=(
            "Sorry about the late delivery. Reply with your order ID and "
            "I'll start a refund check for missing items."
        ),
        metadata_json=json.dumps(
            {"domain": "HR policy assistant for employee handbook Q&A"}
        ),
    )
    assert "cross_domain_contamination" in reasons


def test_detect_pto_under_remote_topic():
    reasons = cross_domain_reject_reasons(
        topic="remote_and_hybrid_work_eligibility",
        input_text="When does PTO accrual start?",
        output_text=(
            "PTO accrues starting the first day of the second full month. "
            "Unused PTO carries over up to 5 days."
        ),
        metadata_json=json.dumps({"domain": "HR policy"}),
    )
    assert "topic_content_mismatch" in reasons


def test_clean_hr_pto_not_rejected():
    reasons = cross_domain_reject_reasons(
        topic="pto_accrual_and_carryover",
        input_text="When does PTO accrual start?",
        output_text=(
            "PTO typically starts accruing after a short wait. "
            "Share your start date month and I'll confirm your balance path."
        ),
        metadata_json=json.dumps({"domain": "HR policy assistant"}),
    )
    assert reasons == []


def test_backfill_rejects_poisoned_rows(db):
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
    poison_ids = ["gold_4932cc32dcd1", "gold_7cd5960226b2"]
    for i, gid in enumerate(poison_ids):
        db.add(
            m.GoldExample(
                id=gid,
                owner_user_id=uid,
                tenant_id=tid,
                topic="pto_accrual_and_carryover" if i == 0 else "hr_policy_reply",
                input_text=(
                    f"[corpus] Q: My order arrived late, what can I do? case {i}"
                ),
                output_text=(
                    "Sorry about late delivery and missing items. "
                    "Share order ID and I'll process a refund for your bag."
                ),
                source_kind="corpus",
                verification_status="verified",
                metadata_json=json.dumps(
                    {"domain": "HR policy assistant for employee handbook Q&A"}
                ),
            )
        )
    # Clean HR gold should stay verified
    clean_id = _uid("gold_")
    db.add(
        m.GoldExample(
            id=clean_id,
            owner_user_id=uid,
            tenant_id=tid,
            topic="pto_accrual_and_carryover",
            input_text="When does PTO accrual start for full-time staff?",
            output_text=(
                "PTO usually starts accruing after the first full months of employment. "
                "Tell me your start month and I'll map the next step."
            ),
            source_kind="corpus",
            verification_status="verified",
            metadata_json=json.dumps({"domain": "HR policy assistant"}),
        )
    )
    db.commit()

    out = backfill_reject_cross_domain_gold(
        db, owner_user_id=uid, tenant_id=tid
    )
    assert out["newly_rejected"] >= 2
    for gid in poison_ids:
        g = db.query(m.GoldExample).filter_by(id=gid).first()
        assert g.verification_status == "rejected"
        meta = json.loads(g.metadata_json or "{}")
        assert "cross_domain_contamination" in (meta.get("rejection_reasons") or [])
        assert meta.get("rejection_reason")
        assert "cross-domain" in (g.rationale or "").lower()

    clean = db.query(m.GoldExample).filter_by(id=clean_id).first()
    assert clean.verification_status == "verified"
