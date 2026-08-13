"""Spend-cap pause requires explicit user consent to continue."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from helix.db import models as m
from helix.db.models import Base
from helix.services.batch_jobs import (
    cancel_job,
    continue_past_spend_cap,
    create_batch_job,
    job_to_dict,
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


def test_continue_past_cap_resumes_remaining_batches(db):
    tid, uid = _seed(db)
    job = create_batch_job(
        db,
        owner_user_id=uid,
        tenant_id=tid,
        job_type="pipeline",
        batch_size=5,
        total_batches=3,
    )
    # Simulate pause after batch 1
    job.status = "paused_spend_cap"
    job.completed_batches = 1
    job.cost_usd = 0.5
    job.spend_cap_override = False
    db.commit()

    d0 = job_to_dict(job)
    assert d0["needs_spend_consent"] is True

    out = continue_past_spend_cap(db, job.id, uid)
    assert out is not None
    assert out.status == "pending"
    assert out.spend_cap_override is True
    assert out.finished_at is None

    d1 = job_to_dict(out)
    assert d1["needs_spend_consent"] is False
    assert d1["spend_cap_override"] is True


def test_continue_past_cap_completes_when_no_remaining(db):
    tid, uid = _seed(db)
    job = create_batch_job(
        db,
        owner_user_id=uid,
        tenant_id=tid,
        job_type="pipeline",
        batch_size=5,
        total_batches=1,
    )
    job.status = "paused_spend_cap"
    job.completed_batches = 1
    job.cost_usd = 0.4
    db.commit()

    out = continue_past_spend_cap(db, job.id, uid)
    assert out.status == "completed"
    assert out.spend_cap_override is True


def test_cancel_from_paused_spend_cap(db):
    tid, uid = _seed(db)
    job = create_batch_job(
        db,
        owner_user_id=uid,
        tenant_id=tid,
        job_type="pipeline",
        batch_size=5,
        total_batches=2,
    )
    job.status = "paused_spend_cap"
    job.completed_batches = 1
    db.commit()

    out = cancel_job(db, job.id, uid)
    assert out.status == "cancelled"
    assert "spend cap" in (out.progress_message or "").lower()


def test_continue_is_noop_when_not_paused(db):
    tid, uid = _seed(db)
    job = create_batch_job(
        db,
        owner_user_id=uid,
        tenant_id=tid,
        job_type="pipeline",
        batch_size=5,
        total_batches=1,
    )
    assert job.status == "pending"
    out = continue_past_spend_cap(db, job.id, uid)
    assert out.status == "pending"
    assert not out.spend_cap_override
