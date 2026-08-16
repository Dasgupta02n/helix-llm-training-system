"""Quick accuracy checks for Riu."""

from __future__ import annotations

from helix.db import models as m
from helix.db.session import SessionLocal, init_db
from helix.services import riu as R
from helix.services.riu import _heuristic_turn, _refuses_run, _wants_run


def main() -> None:
    assert _wants_run("start")
    assert _wants_run("yes, start")
    assert _wants_run("run it")
    assert _wants_run("start 10")
    assert not _wants_run("restart")
    assert not _wants_run("please restart later")
    assert not _wants_run("start over")
    assert _refuses_run("don't start")

    r = _heuristic_turn(
        "please restart later",
        {"project_name": "X", "mission": "m", "categories": ["a"]},
        "confirm",
    )
    assert r["phase"] == "confirm" and not r["actions"], r

    r = _heuristic_turn(
        "start",
        {
            "project_name": "X",
            "mission": "m",
            "categories": ["a"],
            "sample_input": "q",
            "sample_output": "a",
        },
        "confirm",
    )
    assert any(a["type"] == "start_pipeline" for a in r["actions"]), r

    init_db()
    db = SessionLocal()
    user = db.query(m.User).filter(m.User.is_superadmin.is_(True)).first()
    tenant = db.query(m.Tenant).filter_by(slug="demo").first()
    assert user and tenant

    R._llm_turn = lambda **_kw: (_ for _ in ()).throw(RuntimeError("no llm"))
    s = R.create_session(db, user_id=user.id, tenant_id=tenant.id)
    out = None
    for text in [
        "Sales coach AI",
        "Help managers give better feedback",
        "coaching, feedback, 1:1s",
        "How do I give tough feedback?\nBe direct, kind, and specific with one next step.",
        "ok",
        "start",
    ]:
        s = db.query(m.RiuSession).filter_by(id=s.id).first()
        out = R.handle_user_message(db, tenant=tenant, user=user, session=s, text=text)
        print(text[:40], "->", out["phase"], "job", out.get("last_job_id"))

    assert out is not None
    assert out["phase"] == "running" and out.get("last_job_id")
    plan = (
        db.query(m.ResearchProject)
        .filter_by(tenant_id=tenant.id, slug="default")
        .first()
    )
    assert plan and "Sales" in plan.name
    print("ALL CHECKS PASSED")
    db.close()


if __name__ == "__main__":
    main()
