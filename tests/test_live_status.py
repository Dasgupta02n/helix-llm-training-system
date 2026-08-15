"""Live process heartbeat + activity payload."""

from datetime import datetime, timedelta, timezone

from helix.services.live_status import heartbeat_fields


def test_heartbeat_live_quiet_stale():
    now = datetime.now(timezone.utc)
    live = heartbeat_fields(now, running=True, started_at=now)
    assert live["live_state"] == "live"
    quiet = heartbeat_fields(now - timedelta(seconds=20), running=True, started_at=now)
    assert quiet["live_state"] == "quiet"
    stale = heartbeat_fields(now - timedelta(seconds=80), running=True, started_at=now)
    assert stale["live_state"] == "stale"
    idle = heartbeat_fields(now, running=False)
    assert idle["live_state"] == "idle"


def test_job_to_dict_includes_heartbeat_and_events():
    import uuid

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from helix.db import models as m
    from helix.db.models import Base
    from helix.services.batch_jobs import job_to_dict, list_user_jobs

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    tid, uid, jid = f"t{uuid.uuid4().hex[:8]}", f"u{uuid.uuid4().hex[:8]}", f"job_{uuid.uuid4().hex[:8]}"
    db.add(m.Tenant(id=tid, slug=tid, name="t", is_active=True))
    db.add(
        m.User(
            id=uid,
            email=f"{uid}@ex.com",
            hashed_password="x",
            is_active=True,
            email_verified=True,
            admin_approved=True,
        )
    )
    job = m.BatchJob(
        id=jid,
        owner_user_id=uid,
        tenant_id=tid,
        job_type="pipeline",
        status="running",
        progress_message="Gathering sources…",
        total_batches=2,
        completed_batches=0,
    )
    db.add(job)
    db.add(
        m.BatchJobEvent(
            id=f"ev_{uuid.uuid4().hex[:8]}",
            job_id=jid,
            batch_index=1,
            message="Gathering sources (Apify/code)…",
            level="info",
        )
    )
    db.commit()
    d = job_to_dict(job, [])
    assert "live_state" in d
    assert "live_label" in d
    listed = list_user_jobs(db, uid, tid)
    assert listed[0]["events"]
    assert "Gathering" in listed[0]["events"][0]["message"]
    assert "Apify" not in listed[0]["events"][0]["message"]
    db.close()
    engine.dispose()


def test_public_activity_strips_vendor_names():
    from helix.services.live_status import public_activity_text

    assert "Apify" not in public_activity_text("Gathering sources (Apify/code)…")
    assert public_activity_text("Gathering sources (Apify/code)…") == "Gathering sources …"
    assert public_activity_text("RunPod IN_PROGRESS") == "training IN_PROGRESS"
    assert public_activity_text("Heartbeat — RunPod still In Progress.") == (
        "Heartbeat — training still In Progress."
    )
    assert public_activity_text("Cost: OpenRouter $1.00 + Apify $0.20") == (
        "Cost: the model $1.00 + gather $0.20"
    )
    assert "Hugging" not in public_activity_text("Uploading to Hugging Face…")
    assert "Hostinger" not in public_activity_text("Deploying on Hostinger")


def test_train_activity_appended():
    from helix.db import models as m
    from helix.services.double_helix_train import append_train_activity, job_to_dict

    job = m.DoubleHelixTrainJob(
        id="dht_live1",
        owner_user_id="u",
        tenant_id="t",
        status="running",
        base_model_id="Qwen/Qwen2.5-7B-Instruct",
        gold_count=3,
        meta_json="{}",
    )
    append_train_activity(job, "Uploading gold…")
    append_train_activity(job, "RunPod IN_PROGRESS")
    d = job_to_dict(job)
    assert d["live_state"] in {"live", "quiet", "stale"}
    assert len(d["events"]) == 2
    assert d["events"][-1]["message"] == "training IN_PROGRESS"
    assert "RunPod" not in d["progress"]
