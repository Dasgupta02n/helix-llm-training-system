"""Riu agentic mailbox: Hostinger ingest, webhook token, send gate, Riu actions."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from helix.db import models as m
from helix.db.models import Base
from helix.services import mailbox as mailbox_svc
from helix.services.riu_actions import (
    _apply_mailbox_action,
    _wants_mailbox_list,
    _wants_send_mail,
)


def _uid(p: str = "") -> str:
    return f"{p}{uuid.uuid4().hex[:12]}"


def _db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return Session()


def _seed(db):
    tid, uid = _uid("ten_"), _uid("usr_")
    db.add(m.Tenant(id=tid, slug=f"s-{tid[-6:]}", name="T", plan="starter", is_active=True))
    admin = m.User(
        id=uid,
        email=f"{uid}@ex.com",
        hashed_password="x",
        is_active=True,
        email_verified=True,
        admin_approved=True,
        password_set=True,
        is_superadmin=True,
    )
    db.add(admin)
    db.commit()
    session = m.RiuSession(
        id=_uid("riu_"),
        tenant_id=tid,
        owner_user_id=uid,
        status="active",
        phase="done",
        state_json="{}",
        messages_json="[]",
    )
    db.add(session)
    db.commit()
    return admin, session


def test_parse_email_and_allowlist():
    assert mailbox_svc.parse_email_addr("Riu <riu@c7xai.in>") == "riu@c7xai.in"
    assert mailbox_svc.parse_email_addr("you@example.com") == "you@example.com"
    with patch.object(
        mailbox_svc,
        "get_settings",
        return_value=SimpleNamespace(
            mailbox_allowed_senders_list=["you@example.com", "@trusted.com"]
        ),
    ):
        assert mailbox_svc.sender_allowed("You <you@example.com>")
        assert mailbox_svc.sender_allowed("a@trusted.com")
        assert not mailbox_svc.sender_allowed("evil@other.com")


def test_hostinger_webhook_bearer_token():
    secret = "mailbox-test-secret"
    assert mailbox_svc.verify_hostinger_webhook(
        authorization=f"Bearer {secret}", secret=secret
    )
    assert not mailbox_svc.verify_hostinger_webhook(
        authorization="Bearer other", secret=secret
    )
    assert not mailbox_svc.verify_hostinger_webhook(authorization="", secret=secret)


def test_ingest_received_event_stores_unread():
    db = _db()
    event = {
        "event": "message.received",
        "mailbox": "riu@c7xai.in",
        "message": {
            "uid": 11,
            "folder": "INBOX",
            "from": {"name": "Ada", "address": "ada@example.com"},
            "to": [{"address": "riu@c7xai.in"}],
            "subject": "Hello Riu",
            "text": "Can you help with gold?",
            "message_id": "<mid-1@example.com>",
            "attachments": [],
        },
    }
    with patch.object(mailbox_svc, "mailbox_api_key", return_value=""):
        row = mailbox_svc.ingest_received_event(db, event)
    assert row is not None
    assert row.direction == "inbound"
    assert row.status == "unread"
    assert row.subject == "Hello Riu"
    assert row.provider_email_id == "INBOX:11"
    again = mailbox_svc.ingest_received_event(db, event)
    assert again.id == row.id
    assert db.query(m.MailboxMessage).count() == 1


def test_ignored_when_sender_not_allowlisted():
    db = _db()
    event = {
        "event": "message.received",
        "message": {
            "uid": 12,
            "from": {"address": "spam@evil.test"},
            "to": [{"address": "riu@c7xai.in"}],
            "subject": "Buy now",
            "text": "ignore me",
        },
    }
    with (
        patch.object(mailbox_svc, "mailbox_api_key", return_value=""),
        patch.object(
            mailbox_svc,
            "get_settings",
            return_value=SimpleNamespace(mailbox_allowed_senders_list=["you@example.com"]),
        ),
    ):
        row = mailbox_svc.ingest_received_event(db, event)
    assert row.status == "ignored"
    assert row.allowlisted is False


def test_riu_list_mailbox_action():
    db = _db()
    user, session = _seed(db)
    event = {
        "event": "message.received",
        "message": {
            "uid": 13,
            "from": {"address": "ada@example.com"},
            "to": [{"address": "riu@c7xai.in"}],
            "subject": "Need a quote",
            "text": "Please reply",
        },
    }
    with patch.object(mailbox_svc, "mailbox_api_key", return_value=""):
        mailbox_svc.ingest_received_event(db, event)
    with (
        patch.object(mailbox_svc, "can_use_riu_mailbox", return_value=True),
        patch.object(mailbox_svc, "mailbox_configured", return_value=True),
    ):
        result = _apply_mailbox_action(
            db,
            user=user,
            session=session,
            state={},
            action={"type": "list_mailbox"},
            user_text="check inbox",
        )
    assert result["ok"] is True
    assert result["mailbox"]["unread"] == 1
    assert result["mailbox"]["recent"][0]["subject"] == "Need a quote"


def test_send_without_confirm_becomes_draft():
    db = _db()
    user, session = _seed(db)
    state: dict = {}
    with (
        patch.object(mailbox_svc, "can_use_riu_mailbox", return_value=True),
        patch.object(mailbox_svc, "mailbox_configured", return_value=True),
    ):
        result = _apply_mailbox_action(
            db,
            user=user,
            session=session,
            state=state,
            action={
                "type": "send_mail",
                "to": "ada@example.com",
                "subject": "Hi",
                "body": "Hello from Riu",
            },
            user_text="draft a note to ada",
        )
    assert result["action"] == "draft_mail"
    assert state["mailbox_draft"]["to"] == "ada@example.com"
    assert db.query(m.MailboxMessage).filter_by(direction="outbound").count() == 0


def test_send_after_confirm_calls_hostinger():
    db = _db()
    user, session = _seed(db)
    fake = SimpleNamespace(
        status_code=200,
        json=lambda: {"data": {"uid": 99}},
        text="",
        content=b'{"data":{"uid":99}}',
    )
    with (
        patch.object(mailbox_svc, "can_use_riu_mailbox", return_value=True),
        patch.object(mailbox_svc, "mailbox_configured", return_value=True),
        patch.object(mailbox_svc, "mailbox_api_key", return_value="hm_test"),
        patch.object(mailbox_svc, "mailbox_from", return_value="Riu <riu@c7xai.in>"),
        patch.object(mailbox_svc, "mailbox_address", return_value="riu@c7xai.in"),
        patch.object(mailbox_svc, "resolve_mailbox_id", return_value="ACtestmailbox"),
        patch("helix.services.mailbox.httpx.post", return_value=fake) as post,
    ):
        result = _apply_mailbox_action(
            db,
            user=user,
            session=session,
            state={},
            action={
                "type": "send_mail",
                "to": "ada@example.com",
                "subject": "Hi",
                "body": "Hello from Riu",
            },
            user_text="send it",
        )
    assert result["ok"] is True
    assert post.called
    url = post.call_args.args[0] if post.call_args.args else post.call_args.kwargs.get("url")
    assert "api.mail.hostinger.com" in str(url)
    row = db.query(m.MailboxMessage).filter_by(direction="outbound").one()
    assert row.status == "sent"
    assert row.to_emails == json.dumps(["ada@example.com"])


def test_non_admin_cannot_use_mailbox():
    db = _db()
    user, session = _seed(db)
    user.is_superadmin = False
    db.commit()
    result = _apply_mailbox_action(
        db,
        user=user,
        session=session,
        state={},
        action={"type": "list_mailbox"},
        user_text="check inbox",
    )
    assert result["ok"] is False
    assert "operators" in result["error"]


def test_mailbox_intent_helpers():
    assert _wants_mailbox_list("check your inbox")
    assert _wants_send_mail("send it")
    assert not _wants_send_mail("please draft a reply")
