"""Corpus must be plan-scoped: Plan A docs never feed Plan B mining gold."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from helix.db import models as m
from helix.db.models import Base
from helix.services.corpus import (
    add_paste,
    document_matches_brief,
    infer_category_from_text,
    list_corpus,
    promote_corpus_into_pipeline,
    write_corpus_units_as_gold,
)
from helix.services.pipeline_modes import run_code_pipeline_batch


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


def _workspace(db):
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
    db.add(m.Membership(id=_uid("mem_"), user_id=uid, tenant_id=tid, role="owner"))
    food_id = _uid("rp_")
    hr_id = _uid("rp_")
    db.add(
        m.ResearchProject(
            id=food_id,
            tenant_id=tid,
            slug="food-support",
            name="Food Delivery Support",
            domain="food delivery customer support",
            mission="Help with late delivery and refunds",
            research_questions_json="[]",
            sources_json=json.dumps(["web"]),
            categories_json=json.dumps(
                ["late delivery", "missing items", "refunds"]
            ),
            phase_targets_json="{}",
            success_metrics_json="[]",
            topic_keys_json=json.dumps(["late_delivery", "refunds"]),
            is_active=False,
        )
    )
    db.add(
        m.ResearchProject(
            id=hr_id,
            tenant_id=tid,
            slug="hr-policy",
            name="HR Policy",
            domain="HR policy assistant for employee handbook Q&A",
            mission="Answer PTO and remote work questions",
            research_questions_json="[]",
            sources_json=json.dumps(["web"]),
            categories_json=json.dumps(
                ["pto", "pto accrual and carryover", "remote and hybrid work eligibility"]
            ),
            phase_targets_json="{}",
            success_metrics_json="[]",
            topic_keys_json=json.dumps(
                ["pto_accrual_and_carryover", "remote_and_hybrid_work_eligibility"]
            ),
            is_active=True,
        )
    )
    db.commit()
    return tid, uid, food_id, hr_id


FOOD_FAQ = """
Q1: My order arrived late, what can I do?
If delivery is more than 30 minutes late, open Help in the app. We often issue a credit for late delivery. Share your order ID.
Q2: An item is missing from my bag.
Missing items are refunded or re-delivered. Reply with order ID and a photo of the receipt.
"""

HR_FAQ = """
Q1: When does PTO accrual start?
PTO accrues starting the first day of the second full month of employment. Full-time staff accrue 1.25 days per month.
Q2: Can unused PTO carry over?
Unused PTO carries over up to 5 days into the next calendar year.
"""


def test_plan_a_corpus_never_in_plan_b_promote(db):
    tid, uid, food_id, hr_id = _workspace(db)
    # Upload under food plan
    food_paste = add_paste(
        db,
        tenant_id=tid,
        owner_user_id=uid,
        title="Late Delivery & Missing Items FAQ",
        content=FOOD_FAQ,
        category="late delivery",
        project_id=food_id,
    )
    assert food_paste["ok"]
    assert food_paste["document"]["project_id"] == food_id

    # Active plan is HR
    hr_brief = {
        "id": hr_id,
        "project_id": hr_id,
        "domain": "HR policy assistant for employee handbook Q&A",
        "mission": "Answer PTO questions",
        "categories": [
            "pto",
            "pto accrual and carryover",
            "remote and hybrid work eligibility",
        ],
    }
    listed = list_corpus(
        db,
        tenant_id=tid,
        owner_user_id=uid,
        project_id=hr_id,
        brief=hr_brief,
        scope_to_plan=True,
    )
    assert all(d.project_id != food_id or d.id != food_paste["document"]["id"] for d in listed)
    assert food_paste["document"]["id"] not in {d.id for d in listed}

    promo = promote_corpus_into_pipeline(
        db,
        tenant_id=tid,
        owner_user_id=uid,
        brief=hr_brief,
        batch_size=5,
    )
    assert promo["docs"] == 0 or all(
        u.get("corpus_id") != food_paste["document"]["id"] for u in promo.get("units") or []
    )
    assert not any(
        "late delivery" in (u.get("evidence") or "").lower()
        or "order id" in (u.get("evidence") or "").lower()
        for u in promo.get("units") or []
    ), promo


def test_plan_b_mining_does_not_create_gold_from_plan_a_corpus(db, monkeypatch):
    tid, uid, food_id, hr_id = _workspace(db)
    add_paste(
        db,
        tenant_id=tid,
        owner_user_id=uid,
        title="Late Delivery & Missing Items FAQ",
        content=FOOD_FAQ,
        category="late delivery",
        project_id=food_id,
    )
    add_paste(
        db,
        tenant_id=tid,
        owner_user_id=uid,
        title="PTO handbook FAQ",
        content=HR_FAQ,
        category="pto",
        project_id=hr_id,
    )

    monkeypatch.setattr(
        "helix.tools.handlers.trigger_discovery",
        lambda *a, **k: {
            "ok": True,
            "job_id": "x",
            "results": [],
            "needs_judgment": [],
            "result_count": 0,
        },
    )
    from helix.services import gold_quality as gq

    real = gq.synthesize_gold_pair

    def _no_llm(**kw):
        kw = dict(kw)
        kw["prefer_llm"] = False
        return real(**kw)

    monkeypatch.setattr("helix.services.gold_quality.synthesize_gold_pair", _no_llm)

    result = run_code_pipeline_batch(
        db, tenant_id=tid, owner_user_id=uid, batch_size=5
    )
    db.commit()

    gold_rows = (
        db.query(m.GoldExample)
        .filter_by(owner_user_id=uid, tenant_id=tid, is_archived=False)
        .all()
    )
    # No food-delivery order/refund content under HR mining
    for g in gold_rows:
        blob = f"{g.input_text}\n{g.output_text}".lower()
        assert "order id" not in blob and "arrived late" not in blob, (
            g.topic,
            g.input_text[:120],
        )
        assert "missing items" not in blob or "pto" in blob

    corpus_gold = [g for g in gold_rows if (g.source_kind or "") == "corpus"]
    assert corpus_gold, f"expected HR corpus gold; result={result}"
    for g in corpus_gold:
        blob = f"{g.input_text}\n{g.output_text}".lower()
        assert any(x in blob for x in ("pto", "accrue", "carry", "leave", "handbook"))


def test_infer_category_pto_not_remote():
    cats = [
        "pto",
        "pto accrual and carryover",
        "remote and hybrid work eligibility",
    ]
    text = (
        "When does PTO accrual start? PTO accrues starting the first day of the "
        "second full month. Unused PTO carries over up to 5 days."
    )
    cat = infer_category_from_text(text, cats)
    assert "remote" not in cat.lower()
    assert "pto" in cat.lower() or "accrual" in cat.lower() or "carry" in cat.lower()


def test_document_matches_brief_by_project_id(db):
    tid, uid, food_id, hr_id = _workspace(db)
    food = add_paste(
        db,
        tenant_id=tid,
        owner_user_id=uid,
        title="Food FAQ",
        content=FOOD_FAQ,
        project_id=food_id,
    )
    doc = db.query(m.CorpusDocument).filter_by(id=food["document"]["id"]).first()
    assert document_matches_brief(
        doc, {"id": food_id, "domain": "food delivery", "categories": ["late delivery"]}
    )
    assert not document_matches_brief(
        doc,
        {
            "id": hr_id,
            "domain": "HR policy assistant",
            "categories": ["pto", "remote work"],
        },
    )
