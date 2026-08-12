"""
End-to-end: a BYO corpus document must become candidates + evidence AND gold.

This is the regression the production retest caught — corpus save alone is not enough;
mining must promote corpus → DiscoveryCandidate/EvidenceStaging → GoldExample.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from helix.db.models import Base
from helix.db import models as m
from helix.services.corpus import add_paste, promote_corpus_into_pipeline
from helix.services.library import gold_to_dict
from helix.services.pipeline_modes import run_code_pipeline_batch


FAQ = """
Q1: My delivery is late. What should I do?
If your order is more than 30 minutes late, open the app and tap Help on the order.
We can check live tracking and often issue a credit for the wait.
Reply with your order ID and I'll look it up right away.

Q2: An item is missing from my bag.
Missing items are refunded or re-delivered depending on restaurant stock.
Share your order ID and a photo of the receipt if you have one and I'll process it.

Q3: Food arrived damaged or cold.
We refund damaged or cold food within 24 hours when you share order ID and a photo.
I'll start the refund check as soon as I have those details.
"""


def _uid(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = Session()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _seed_workspace(db):
    tenant_id = _uid("ten_")
    user_id = _uid("usr_")
    db.add(
        m.Tenant(
            id=tenant_id,
            slug=f"test-{tenant_id[-6:]}",
            name="Corpus E2E Tenant",
            plan="starter",
            is_active=True,
        )
    )
    db.add(
        m.User(
            id=user_id,
            email=f"{user_id}@example.com",
            hashed_password="x",
            full_name="Tester",
            is_active=True,
            email_verified=True,
            admin_approved=True,
            password_set=True,
        )
    )
    db.add(
        m.Membership(
            id=_uid("mem_"),
            user_id=user_id,
            tenant_id=tenant_id,
            role="owner",
        )
    )
    db.add(
        m.ResearchProject(
            id=_uid("rp_"),
            tenant_id=tenant_id,
            slug="food-support",
            name="Food Delivery Support",
            domain="food delivery customer support",
            mission="Help customers with late delivery, missing items, and refunds",
            research_questions_json=json.dumps(["How do refunds work?"]),
            sources_json=json.dumps(["web", "blog"]),
            categories_json=json.dumps(
                ["late delivery", "missing items", "refunds", "wrong items"]
            ),
            phase_targets_json=json.dumps(
                {
                    "late delivery": 40,
                    "missing items": 40,
                    "refunds": 40,
                    "wrong items": 40,
                }
            ),
            success_metrics_json=json.dumps([]),
            topic_keys_json=json.dumps(
                ["late_delivery", "missing_items", "refunds", "wrong_items"]
            ),
            agent_instructions="casual friendly support tone",
            is_active=True,
        )
    )
    db.commit()
    return tenant_id, user_id


def test_corpus_promotes_to_candidate_and_evidence(db_session):
    """promote_corpus_into_pipeline must create real candidate + full evidence body."""
    tenant_id, user_id = _seed_workspace(db_session)
    out = add_paste(
        db_session,
        tenant_id=tenant_id,
        owner_user_id=user_id,
        title="Support FAQ",
        content=FAQ,
        category="general",
    )
    assert out["ok"] is True
    doc_id = out["document"]["id"]

    promo = promote_corpus_into_pipeline(
        db_session,
        tenant_id=tenant_id,
        owner_user_id=user_id,
        brief={
            "domain": "food delivery customer support",
            "categories": ["late delivery", "missing items", "refunds"],
        },
        batch_size=5,
    )
    db_session.commit()

    assert promo["docs"] == 1
    assert promo["candidates_created"] >= 1
    assert len(promo["units"]) >= 3

    cands = (
        db_session.query(m.DiscoveryCandidate)
        .filter_by(tenant_id=tenant_id, source="corpus")
        .all()
    )
    assert len(cands) >= 3
    assert all((c.url or "").startswith("corpus://") for c in cands)
    assert any(doc_id in (c.url or "") for c in cands)

    staged = (
        db_session.query(m.EvidenceStaging)
        .filter_by(tenant_id=tenant_id)
        .all()
    )
    assert staged
    # Full FAQ body must be present — not title/snippet only
    bodies = " ".join((s.content_text or "") for s in staged)
    assert "30 minutes late" in bodies or "missing" in bodies.lower()
    assert len(max((s.content_text or "") for s in staged)) >= 80

    camps = (
        db_session.query(m.Campaign)
        .filter_by(tenant_id=tenant_id, verification_status="verified")
        .all()
    )
    assert any((c.title or "").startswith("[corpus]") for c in camps)
    ce = db_session.query(m.CampaignEvidence).filter_by(tenant_id=tenant_id).all()
    assert ce
    assert any(len(e.content_text or "") >= 80 for e in ce)


def test_corpus_reaches_gold_via_code_pipeline(db_session, monkeypatch):
    """
    Full code pipeline batch: with Apify mocked empty, corpus alone must still
    produce verified gold rows with source_kind=corpus / source_ref corpus:…
    """
    tenant_id, user_id = _seed_workspace(db_session)
    out = add_paste(
        db_session,
        tenant_id=tenant_id,
        owner_user_id=user_id,
        title="Support FAQ",
        content=FAQ,
        category="general",
    )
    assert out["ok"] is True
    doc_id = out["document"]["id"]

    # No live Apify: web discovery returns nothing so only corpus can create gold
    def _fake_discovery(ctx, **kwargs):
        return {
            "ok": True,
            "job_id": "fake",
            "results": [],
            "needs_judgment": [],
            "result_count": 0,
            "gatherer": "apify",
            "message": "mocked empty gather",
        }

    monkeypatch.setattr(
        "helix.tools.handlers.trigger_discovery", _fake_discovery
    )
    # Force template synthesis path (no network LLM) for determinism
    from helix.services import gold_quality as gq

    real_synth = gq.synthesize_gold_pair

    def _synth_no_llm(**kwargs):
        kwargs = dict(kwargs)
        kwargs["prefer_llm"] = False
        return real_synth(**kwargs)

    monkeypatch.setattr(
        "helix.services.pipeline_modes.synthesize_gold_pair", _synth_no_llm
    )

    result = run_code_pipeline_batch(
        db_session,
        tenant_id=tenant_id,
        owner_user_id=user_id,
        batch_size=5,
    )
    db_session.commit()

    # Pipeline must have seen corpus
    steps = " | ".join(result.get("steps") or [])
    assert "corpus_early" in steps or "corpus:" in steps, steps
    assert result.get("gold_new", 0) >= 1, (
        f"expected corpus gold, got result={result}"
    )

    gold_rows = (
        db_session.query(m.GoldExample)
        .filter_by(
            owner_user_id=user_id,
            tenant_id=tenant_id,
            is_archived=False,
        )
        .all()
    )
    corpus_gold = [
        g
        for g in gold_rows
        if (g.source_kind or "") == "corpus"
        or (g.source_ref or "").startswith("corpus:")
        or "user_corpus" in (g.metadata_json or "")
    ]
    assert corpus_gold, (
        f"no corpus-sourced gold; all rows={[ (g.source_kind, g.source_ref) for g in gold_rows ]}"
    )
    for g in corpus_gold:
        assert (g.verification_status or "").lower() == "verified"
        assert (g.source_ref or "").startswith("corpus:")
        assert doc_id in (g.source_ref or "") or doc_id in (g.metadata_json or "")
        # Output must be a support reply, not a refuse / empty dump
        assert len(g.output_text or "") >= 40
        assert "internal doc" not in (g.output_text or "").lower()
        assert "based on the available documentation" not in (
            g.output_text or ""
        ).lower()

    # Candidate path still present after pipeline
    assert (
        db_session.query(m.DiscoveryCandidate)
        .filter_by(tenant_id=tenant_id, source="corpus")
        .count()
        >= 1
    )


def test_gold_to_dict_exposes_rejection_reason(db_session):
    tenant_id, user_id = _seed_workspace(db_session)
    g = m.GoldExample(
        id=_uid("gold_"),
        owner_user_id=user_id,
        tenant_id=tenant_id,
        topic="refunds",
        input_text="Customer needs a refund",
        output_text="I don't have enough verified evidence. Share the policy page.",
        rationale="test",
        source_kind="pipeline",
        verification_status="rejected",
        metadata_json=json.dumps(
            {
                "rejection_reasons": ["support_refuses_or_demands_internal_docs"],
                "rejection_reason": (
                    "Support reply refuses to help or demands internal docs "
                    "from the customer"
                ),
            }
        ),
    )
    db_session.add(g)
    db_session.commit()

    d = gold_to_dict(g)
    assert d["verification_status"] == "rejected"
    assert d["rejection_reason"]
    assert "internal docs" in d["rejection_reason"].lower() or "refuses" in d[
        "rejection_reason"
    ].lower()
    assert d["rejection_reasons"] == ["support_refuses_or_demands_internal_docs"]
