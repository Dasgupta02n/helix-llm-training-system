"""P1: large mining jobs need attached corpus; Riu estimate is honest."""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from helix.db import models as m
from helix.db.models import Base
from helix.services.corpus import (
    LARGE_PIPELINE_UNITS,
    add_paste,
    estimate_corpus_support,
    require_corpus_for_large_job,
)
from helix.services.user_material_upload import estimate_setup_pricing


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
    db.commit()
    return tid, uid


def test_large_job_blocked_without_corpus(db):
    tid, uid = _seed(db)
    try:
        require_corpus_for_large_job(
            db,
            tenant_id=tid,
            owner_user_id=uid,
            batch_size=5,
            total_batches=3,
        )
        assert False, "should have required corpus"
    except ValueError as e:
        assert "corpus" in str(e).lower()


def test_small_job_allowed_without_corpus(db):
    tid, uid = _seed(db)
    stats = require_corpus_for_large_job(
        db,
        tenant_id=tid,
        owner_user_id=uid,
        batch_size=5,
        total_batches=2,
    )
    assert stats["large"] is False
    assert stats["corpus_docs"] == 0


def test_large_job_ok_with_corpus_paste(db):
    tid, uid = _seed(db)
    pid = _uid("prj_")
    db.add(
        m.ResearchProject(
            id=pid,
            tenant_id=tid,
            name="CC Sales",
            slug="default",
            domain="Credit Card Sales Pro",
            mission="Educate consumers",
            is_active=True,
            categories_json='["APR"]',
            sources_json='["consumer credit card education"]',
        )
    )
    db.commit()
    add_paste(
        db,
        tenant_id=tid,
        owner_user_id=uid,
        title="APR FAQ",
        content=(
            "Q1: What is APR?\nAnnual percentage rate is the yearly cost of credit.\n\n"
            "Q2: How is interest calculated?\nIssuers typically use average daily balance.\n\n"
            "Q3: What is a grace period?\nA window where new purchases accrue no interest if paid in full."
        ),
        category="APR",
        project_id=pid,
    )
    stats = require_corpus_for_large_job(
        db,
        tenant_id=tid,
        owner_user_id=uid,
        batch_size=5,
        total_batches=4,
    )
    assert stats["corpus_docs"] >= 1
    assert stats["corpus_units"] >= 1


def test_estimate_does_not_echo_1000_when_corpus_is_thin():
    p = estimate_setup_pricing(
        {
            "gold_target": 1000,
            "batch_size": 5,
            "total_batches": 2,
            "quality_mode": 2,
            "own_data_count": 0,
            "materials_count": 0,
            "corpus_docs": 1,
            "corpus_units": 8,
            "attached_support": 8,
        }
    )
    assert p["requested_exceeds_corpus"] is True
    assert p["attached_support"] == 8
    blob = " ".join(p["summary_lines"]).lower()
    assert "8" in blob
    assert "1000" in blob or "1,000" in blob
    assert "not" in blob or "above" in blob or "pretend" in blob
    assert LARGE_PIPELINE_UNITS == 10


def test_estimate_corpus_support_empty(db):
    tid, uid = _seed(db)
    stats = estimate_corpus_support(db, tenant_id=tid, owner_user_id=uid)
    assert stats["corpus_docs"] == 0
    assert stats["attached_support"] == 0
