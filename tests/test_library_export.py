"""Library zip packs: gold / synth / corpus as separate files."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from helix.db import models as m
from helix.db.models import Base
from helix.services.library_export import (
    FILE_NAMES,
    classify_gold,
    pack_for_user,
    save_session_pack,
    zip_saved_pack,
)
from helix.services.user_gold_upload import USER_UPLOAD_SOURCE_KIND
from helix.services.user_material_upload import USER_MATERIAL_SOURCE_KIND


def _uid(p: str = "") -> str:
    return f"{p}{uuid.uuid4().hex[:12]}"


def _db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return Session(), engine


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


def _gold(db, tid, uid, *, kind, text, created=None, status="verified"):
    g = m.GoldExample(
        id=_uid("g_"),
        owner_user_id=uid,
        tenant_id=tid,
        topic="support",
        input_text=text,
        output_text=f"A:{text}",
        source_kind=kind,
        verification_status=status,
        created_at=created or datetime.now(timezone.utc),
    )
    db.add(g)
    db.commit()
    return g


def test_classify_buckets():
    db, engine = _db()
    try:
        tid, uid = _seed(db)
        mined = _gold(db, tid, uid, kind="pipeline", text="mined")
        up = _gold(db, tid, uid, kind=USER_UPLOAD_SOURCE_KIND, text="upload")
        mat = _gold(db, tid, uid, kind=USER_MATERIAL_SOURCE_KIND, text="material")
        seed = _gold(db, tid, uid, kind="seed", text="seed")
        bad = _gold(db, tid, uid, kind="pipeline", text="rej", status="rejected")
        assert classify_gold(mined) == "gold"
        assert classify_gold(up) == "structured"
        assert classify_gold(mat) == "unstructured"
        assert classify_gold(seed) is None
        assert classify_gold(bad) is None
    finally:
        db.close()
        engine.dispose()


def test_full_library_zip_splits_files():
    db, engine = _db()
    try:
        tid, uid = _seed(db)
        _gold(db, tid, uid, kind="pipeline", text="mined q")
        _gold(db, tid, uid, kind=USER_UPLOAD_SOURCE_KIND, text="labeled q")
        _gold(db, tid, uid, kind=USER_MATERIAL_SOURCE_KIND, text="notes q")
        db.add(
            m.SyntheticExample(
                id=_uid("s_"),
                owner_user_id=uid,
                tenant_id=tid,
                gold_id="none",
                topic="support",
                input_text="var q",
                output_text="var a",
                variation_index=1,
            )
        )
        db.commit()
        tenant = db.query(m.Tenant).get(tid)
        raw, meta = pack_for_user(
            db, user_id=uid, tenant_id=tid, tenant_slug=tenant.slug, scope="library"
        )
        assert meta["counts"] == {
            "gold": 1,
            "synthetic": 1,
            "structured": 1,
            "unstructured": 1,
        }
        zf = zipfile.ZipFile(io.BytesIO(raw))
        names = set(zf.namelist())
        assert FILE_NAMES["gold"] in names
        assert FILE_NAMES["synthetic"] in names
        assert FILE_NAMES["structured"] in names
        assert FILE_NAMES["unstructured"] in names
        assert "README.txt" in names
        gold_line = zf.read(FILE_NAMES["gold"]).decode().strip().splitlines()[0]
        assert json.loads(gold_line)["input"] == "mined q"
        assert "Apify" not in zf.read("README.txt").decode()
    finally:
        db.close()
        engine.dispose()


def test_session_scope_excludes_older_rows():
    db, engine = _db()
    try:
        tid, uid = _seed(db)
        old = datetime.now(timezone.utc) - timedelta(days=2)
        now = datetime.now(timezone.utc)
        _gold(db, tid, uid, kind="pipeline", text="old gold", created=old)
        db.add(
            m.RiuSession(
                id=_uid("riu_"),
                tenant_id=tid,
                owner_user_id=uid,
                status="active",
                phase="running",
                state_json="{}",
                messages_json="[]",
                created_at=now - timedelta(hours=1),
            )
        )
        db.commit()
        _gold(db, tid, uid, kind="pipeline", text="new gold", created=now)
        _gold(db, tid, uid, kind=USER_UPLOAD_SOURCE_KIND, text="new upload", created=now)
        tenant = db.query(m.Tenant).get(tid)
        raw, meta = pack_for_user(
            db, user_id=uid, tenant_id=tid, tenant_slug=tenant.slug, scope="session"
        )
        assert meta["counts"]["gold"] == 1
        assert meta["counts"]["structured"] == 1
        zf = zipfile.ZipFile(io.BytesIO(raw))
        gold_body = zf.read(FILE_NAMES["gold"]).decode()
        assert "new gold" in gold_body
        assert "old gold" not in gold_body
    finally:
        db.close()
        engine.dispose()


def test_named_session_save_and_redownload():
    db, engine = _db()
    try:
        tid, uid = _seed(db)
        now = datetime.now(timezone.utc)
        db.add(
            m.RiuSession(
                id=_uid("riu_"),
                tenant_id=tid,
                owner_user_id=uid,
                status="active",
                phase="running",
                state_json="{}",
                messages_json="[]",
                created_at=now - timedelta(minutes=5),
            )
        )
        db.commit()
        _gold(db, tid, uid, kind="pipeline", text="session gold", created=now)
        tenant = db.query(m.Tenant).get(tid)
        user = db.query(m.User).get(uid)
        saved = save_session_pack(db, user=user, tenant=tenant, version="support_v1")
        assert saved["version"] == "support_v1"
        assert saved["counts"]["gold"] == 1
        raw, meta = zip_saved_pack(db, user_id=uid, tenant=tenant, version="support_v1")
        zf = zipfile.ZipFile(io.BytesIO(raw))
        assert "session gold" in zf.read(FILE_NAMES["gold"]).decode()
        assert meta["version"] == "support_v1"
    finally:
        db.close()
        engine.dispose()
