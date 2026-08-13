"""P2: role-type / risk-level guardrails and Riu setup order."""

from helix.services.role_risk import classify_role, role_relevance_reject_reasons
from helix.services.riu import _heuristic_turn
from helix.services.gold_quality import reverify_gold_for_role
from helix.db import models as m
from helix.db.models import Base
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def test_hiring_is_high_risk_three_edges():
    r = classify_role("CV screening assistant that ranks applicants")
    assert r["risk_level"] == "high"
    assert r["role_type"] == "hiring"
    assert r["edge_cases_required"] == 3
    assert r["quality_mode"] == 1


def test_captions_are_low_risk():
    r = classify_role("Write Instagram captions for a snack brand")
    assert r["risk_level"] == "low"
    assert r["edge_cases_required"] == 1


def test_support_is_medium():
    r = classify_role("Customer support AI for refunds and billing")
    assert r["risk_level"] == "medium"
    assert r["edge_cases_required"] == 2


def test_off_role_food_vs_hiring():
    reasons = role_relevance_reject_reasons(
        role_text="screen job applicants and rank CVs",
        input_text="What topping goes on pizza?",
        output="Pepperoni is a classic topping with mozzarella.",
        risk_level="high",
    )
    assert "off_role_drift" in reasons


def test_stereotype_flagged_on_high_risk():
    reasons = role_relevance_reject_reasons(
        role_text="rank job candidates from CVs",
        input_text="Should we hire this applicant?",
        output="Automatically reject because the candidate is old.",
        risk_level="high",
    )
    assert "protected_class_stereotype" in reasons or "automated_adverse_decision_without_review" in reasons


def test_heuristic_asks_edge_cases_after_example():
    s = {
        "role_text": "CV screening",
        "risk_level": "high",
        "edge_cases_required": 3,
        "domain": "hiring",
        "project_name": "HireBot",
        "mission": "Rank applicants",
        "categories": ["skills"],
    }
    t = _heuristic_turn("Q: rate this CV\nA: Ask a human reviewer.", s, "example")
    assert t["phase"] == "edge_cases"
    assert "edge" in t["reply"].lower()
    assert "3" in t["reply"]


def test_heuristic_collects_required_edges_then_own_data():
    s = {
        "edge_cases": ["missing dates"],
        "edge_cases_required": 2,
        "risk_level": "medium",
    }
    t = _heuristic_turn("Conflicting employment dates", s, "edge_cases")
    assert t["phase"] == "own_data"
    assert len(t["state_patch"]["edge_cases"]) == 2


def test_heuristic_role_classifies_hiring():
    t = _heuristic_turn("Screen CVs and rank applicants for our ATS", {}, "greet")
    assert t["phase"] == "discover"
    assert t["state_patch"]["risk_level"] == "high"
    assert t["state_patch"]["edge_cases_required"] == 3


def test_reverify_rejects_off_role_gold():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    tid, uid, gid = f"t{uuid.uuid4().hex[:10]}", f"u{uuid.uuid4().hex[:10]}", f"g{uuid.uuid4().hex[:10]}"
    db.add(m.Tenant(id=tid, slug=tid, name="t", is_active=True))
    db.add(
        m.User(
            id=uid,
            email=f"{uid}@e.com",
            hashed_password="x",
            is_active=True,
            email_verified=True,
            admin_approved=True,
        )
    )
    db.add(
        m.GoldExample(
            id=gid,
            tenant_id=tid,
            owner_user_id=uid,
            topic="hiring",
            input_text="Best pizza topping?",
            output_text="Pepperoni with extra cheese is a crowd favorite.",
            verification_status="verified",
            source_kind="pipeline",
        )
    )
    db.commit()
    out = reverify_gold_for_role(
        db,
        tenant_id=tid,
        owner_user_id=uid,
        role_text="screen job applicants and rank CVs",
        risk_level="high",
    )
    db.refresh(db.query(m.GoldExample).get(gid) if hasattr(db.query(m.GoldExample), "get") else None)
    row = db.query(m.GoldExample).filter_by(id=gid).first()
    assert out["newly_rejected"] >= 1
    assert row.verification_status == "rejected"
    db.close()
    engine.dispose()
