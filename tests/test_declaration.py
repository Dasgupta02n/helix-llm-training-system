"""Trained-model download requires an undeletable liability declaration."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from helix.db import models as m
from helix.db.models import Base
from helix.services.declaration import (
    DECLARATION_VERSION,
    accept_declaration,
    get_acceptance,
    list_acceptances_for_user,
    retain_after_account_delete,
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


def _user(db):
    uid = _uid("usr_")
    u = m.User(
        id=uid,
        email=f"{uid}@ex.com",
        hashed_password="x",
        full_name="Pat",
        is_active=True,
        email_verified=True,
        admin_approved=True,
        password_set=True,
    )
    db.add(u)
    db.commit()
    return u


def test_accept_requires_confirm(db, monkeypatch):
    monkeypatch.setattr(
        "helix.services.declaration._email_copy", lambda *a, **k: None
    )
    u = _user(db)
    with pytest.raises(ValueError, match="accept"):
        accept_declaration(
            db, user=u, tenant_id="t", train_job_id="dht_1", confirm=False
        )


def test_accept_stores_undeletable_copy_and_is_idempotent(db, monkeypatch):
    sent = {"n": 0}

    def _fake_email(db, row, *, name):
        sent["n"] += 1
        row.email_status = "sent"
        db.commit()

    monkeypatch.setattr("helix.services.declaration._email_copy", _fake_email)
    u = _user(db)
    row = accept_declaration(
        db, user=u, tenant_id="ten", train_job_id="dht_abc", confirm=True
    )
    assert row.email == u.email
    assert row.declaration_version == DECLARATION_VERSION
    assert "not liable" in row.declaration_text.lower()
    assert get_acceptance(db, email=u.email, train_job_id="dht_abc")
    again = accept_declaration(
        db, user=u, tenant_id="ten", train_job_id="dht_abc", confirm=True
    )
    assert again.id == row.id
    listed = list_acceptances_for_user(db, email=u.email, owner_user_id=u.id)
    assert len(listed) == 1
    assert listed[0].id == row.id


def test_account_delete_keeps_declaration_by_email(db, monkeypatch):
    monkeypatch.setattr(
        "helix.services.declaration._email_copy", lambda *a, **k: None
    )
    u = _user(db)
    accept_declaration(
        db, user=u, tenant_id="ten", train_job_id="dht_keep", confirm=True
    )
    n = retain_after_account_delete(db, user=u)
    assert n == 1
    email = u.email
    uid = u.id
    # Simulate account removal the way auth does (user row may remain inactive).
    u.is_active = False
    db.commit()
    still = get_acceptance(db, email=email, train_job_id="dht_keep")
    assert still is not None
    assert still.email == email
    assert still.owner_user_id == uid
    assert still.account_deleted_at is not None
    # No delete helper exists on the service — listing still works by email.
    assert list_acceptances_for_user(db, email=email)
