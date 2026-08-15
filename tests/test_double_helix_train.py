"""Double Helix: download gold stays; train pulls account gold after confirm."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from helix.db import models as m
from helix.db.models import Base
from helix.services.double_helix import build_package_zip
from helix.services.double_helix_train import (
    build_dataset_texts,
    build_trained_zip,
    create_train_job,
    gold_to_alpaca_line,
    job_to_dict,
    load_trainable_gold,
)
from helix.services.runpod_train import official_qlora_input


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


def _seed(db, gold_n: int = 3):
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
    for i in range(gold_n):
        db.add(
            m.GoldExample(
                id=_uid("gld_"),
                owner_user_id=uid,
                tenant_id=tid,
                topic="support",
                input_text=f"Where is order {i}?",
                output_text=f"Share the tracking for {i}.",
                verification_status="verified",
                source_kind="mined",
            )
        )
    db.add(
        m.GoldExample(
            id=_uid("gld_"),
            owner_user_id=uid,
            tenant_id=tid,
            topic="support",
            input_text="Rejected row",
            output_text="Nope",
            verification_status="rejected",
            source_kind="mined",
        )
    )
    db.commit()
    return tid, uid


def test_gold_download_zip_still_works():
    blob = build_package_zip([{"input": "Hello?", "output": "Hi — how can I help?"}])
    zf = zipfile.ZipFile(io.BytesIO(blob))
    assert "data/train_chat.jsonl" in zf.namelist()


def test_load_trainable_gold_skips_rejected(db):
    tid, uid = _seed(db, 2)
    rows = load_trainable_gold(db, owner_user_id=uid, tenant_id=tid)
    assert len(rows) == 2
    assert all(r["input"].startswith("Where is order") for r in rows)


def test_alpaca_and_chat_dataset_texts(db):
    tid, uid = _seed(db, 1)
    rows = load_trainable_gold(db, owner_user_id=uid, tenant_id=tid)
    chat, alpaca = build_dataset_texts(rows)
    c = json.loads(chat.strip())
    a = json.loads(alpaca.strip())
    assert c["messages"][0]["role"] == "user"
    assert a["instruction"]
    assert a["output"]
    assert gold_to_alpaca_line(rows[0])["input"] == ""


def test_create_requires_confirm_and_gold(db, monkeypatch):
    tid, uid = _seed(db, 0)
    monkeypatch.setattr(
        "helix.services.double_helix_train.train_ready", lambda: True
    )
    with pytest.raises(ValueError, match="confirm"):
        create_train_job(
            db, owner_user_id=uid, tenant_id=tid, model_id=None, confirm=False
        )
    with pytest.raises(ValueError, match="No trainable gold"):
        create_train_job(
            db, owner_user_id=uid, tenant_id=tid, model_id=None, confirm=True
        )


def test_create_fetches_account_gold(db, monkeypatch):
    tid, uid = _seed(db, 4)
    monkeypatch.setattr(
        "helix.services.double_helix_train.train_ready", lambda: True
    )
    job = create_train_job(
        db,
        owner_user_id=uid,
        tenant_id=tid,
        model_id="Qwen/Qwen2.5-7B-Instruct",
        confirm=True,
    )
    assert job.status == "queued"
    assert job.gold_count == 4
    assert job.base_model_id == "Qwen/Qwen2.5-7B-Instruct"
    d = job_to_dict(job)
    assert d["download_ready"] is False
    assert d["estimated_usd_min"] == 15
    assert d["include_synthetics"] is False


def test_create_can_include_synthetics(db, monkeypatch):
    tid, uid = _seed(db, 2)
    gold = (
        db.query(m.GoldExample)
        .filter_by(owner_user_id=uid, tenant_id=tid)
        .first()
    )
    db.add(
        m.SyntheticExample(
            id=_uid("syn_"),
            owner_user_id=uid,
            tenant_id=tid,
            gold_id=gold.id,
            topic="support",
            input_text="Any update on my box?",
            output_text="Here is the latest tracking.",
            variation_index=1,
        )
    )
    db.commit()
    monkeypatch.setattr(
        "helix.services.double_helix_train.train_ready", lambda: True
    )
    job = create_train_job(
        db,
        owner_user_id=uid,
        tenant_id=tid,
        model_id="Qwen/Qwen2.5-7B-Instruct",
        confirm=True,
        include_synthetics=True,
    )
    d = job_to_dict(job)
    assert d["include_synthetics"] is True
    assert d["synth_count"] == 1
    assert job.gold_count == 2


def test_official_payload_shape():
    body = official_qlora_input(
        run_id="dht_test",
        base_model="Qwen/Qwen2.5-7B-Instruct",
        dataset_repo="user/helix-gold",
        hub_model_id="user/helix-qlora",
        hf_token="secret-token",
        gold_count=80,
    )
    assert body["run_id"] == "dht_test"
    assert "hf_token" in body["credentials"]
    assert body["args"]["adapter"] == "qlora"
    assert body["args"]["datasets"][0]["path"] == "user/helix-gold"
    assert body["args"]["val_set_size"] == 0.05
    small = official_qlora_input(
        run_id="x",
        base_model="m",
        dataset_repo="d",
        hub_model_id="h",
        hf_token="t",
        gold_count=3,
    )
    assert small["args"]["val_set_size"] == 0.0


def test_trained_zip_has_adapter_tokenizer_and_gold(tmp_path: Path):
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    (adapter / "adapter_config.json").write_text("{}", encoding="utf-8")
    (adapter / "adapter_model.safetensors").write_bytes(b"fake-weights")
    tok = tmp_path / "tok"
    tok.mkdir()
    (tok / "tokenizer.json").write_text("{}", encoding="utf-8")
    (tok / "tokenizer_config.json").write_text("{}", encoding="utf-8")
    job = m.DoubleHelixTrainJob(
        id="dht_zip1",
        owner_user_id="u",
        tenant_id="t",
        status="packaging",
        base_model_id="Qwen/Qwen2.5-7B-Instruct",
        gold_count=1,
        hf_dataset_repo="user/ds",
        hf_model_repo="user/md",
    )
    blob = build_trained_zip(
        job=job,
        adapter_dir=adapter,
        tokenizer_dir=tok,
        gold_rows=[{"input": "Q?", "output": "A."}],
    )
    zf = zipfile.ZipFile(io.BytesIO(blob))
    names = set(zf.namelist())
    assert "qlora/adapter_model.safetensors" in names
    assert "qlora/adapter_config.json" in names
    assert "tokenizer/tokenizer.json" in names
    assert "data/train_chat.jsonl" in names
    assert "README.md" in names
    assert "load_adapter.py" in names
    assert "DECLARATION.txt" in names
    readme = zf.read("README.md").decode()
    assert "python load_adapter.py" in readme
    script = zf.read("load_adapter.py").decode()
    assert "PeftModel.from_pretrained" in script
    meta = json.loads(zf.read("meta.json"))
    assert meta["includes_full_merged_weights"] is False
    assert meta["training"] == "qlora"
