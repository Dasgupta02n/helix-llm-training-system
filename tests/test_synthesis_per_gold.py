"""New gold must get variations even if an old account-wide synth cap is full."""

from __future__ import annotations

import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from helix.db import models as m
from helix.db.models import Base
from helix.services.synthesis import run_synthesis


def _uid(p: str = "") -> str:
    return f"{p}{uuid.uuid4().hex[:12]}"


def test_synth_fills_new_gold_when_account_cap_is_full():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
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
    db.add(
        m.UserDataScope(
            id=_uid("scp_"),
            user_id=uid,
            tenant_id=tid,
            gold_target_count=5,
            variations_per_gold=4,
        )
    )
    old = m.GoldExample(
        id=_uid("gld_"),
        owner_user_id=uid,
        tenant_id=tid,
        topic="refunds",
        input_text="Want a refund",
        output_text="Approve",
        verification_status="verified",
        source_kind="mined",
    )
    new = m.GoldExample(
        id=_uid("gld_"),
        owner_user_id=uid,
        tenant_id=tid,
        topic="helpdesk",
        input_text="Cannot log into Okta",
        output_text="reset password",
        verification_status="verified",
        source_kind="mined",
    )
    db.add_all([old, new])
    for i in range(4):
        db.add(
            m.SyntheticExample(
                id=_uid("syn_"),
                owner_user_id=uid,
                tenant_id=tid,
                gold_id=old.id,
                topic="refunds",
                input_text=f"var {i}",
                output_text="Approve",
                variation_index=i + 1,
            )
        )
    db.commit()

    out = run_synthesis(
        db,
        owner_user_id=uid,
        tenant_id=tid,
        variations_per_gold=4,
        max_golds=5,
        use_llm=False,
        gold_ids=[new.id],
    )
    assert out["ok"] is True
    assert out["synthesized_count"] == 4
    fresh = (
        db.query(m.SyntheticExample)
        .filter_by(gold_id=new.id, is_archived=False)
        .count()
    )
    assert fresh == 4
    db.close()
    engine.dispose()
