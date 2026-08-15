"""C7X-IO catalog: short Apache/MIT list, deliberate default, no Llama."""

from helix.services.base_models import (
    ALLOWED_LICENSES,
    DEFAULT_MODEL_ID,
    MAX_PARAMS_B,
    catalog_payload,
    default_model,
    get_model,
    public_models,
    recommend_model,
    resolve_user_model_choice,
)


def test_catalog_is_short_apache_or_mit():
    models = public_models()
    assert 3 <= len(models) <= 6
    ids = [m["id"] for m in models]
    assert DEFAULT_MODEL_ID in ids
    assert "HuggingFaceTB/SmolLM2-1.7B-Instruct" in ids
    assert "Qwen/Qwen2.5-14B-Instruct" in ids
    for m in models:
        assert m["license"] in ALLOWED_LICENSES
        assert float(m["params_b"]) <= MAX_PARAMS_B
        assert "llama" not in m["id"].lower()
        assert "llama" not in m["name"].lower()
        assert m["best_for"]
        assert m["train_usd_min"] >= 5
        assert m["train_usd_max"] >= m["train_usd_min"]


def test_default_is_qwen_7b_not_smallest():
    rec = default_model()
    assert rec["id"] == DEFAULT_MODEL_ID
    assert rec["params_b"] >= 7
    assert rec["recommended"] is True
    smol = get_model("HuggingFaceTB/SmolLM2-1.7B-Instruct")
    assert smol is not None
    assert smol["recommended"] is False
    assert rec["params_b"] > smol["params_b"]
    assert recommend_model(role_type="hiring", risk_level="high")["id"] == DEFAULT_MODEL_ID


def test_catalog_payload_marks_default():
    payload = catalog_payload()
    assert payload["default_id"] == DEFAULT_MODEL_ID
    assert "role-specific" in payload["default_reason"].lower() or "7B" in payload["default_reason"]
    rec = next(m for m in payload["models"] if m["id"] == DEFAULT_MODEL_ID)
    assert rec["recommended"] is True
    # Recommended is first, but UI must not rely on order alone.
    assert payload["models"][0]["id"] == DEFAULT_MODEL_ID


def test_user_can_pick_qwen_14_and_mistral():
    q = resolve_user_model_choice("qwen 14")
    assert q and "14" in q["name"]
    m = resolve_user_model_choice("mistral 7b")
    assert m and m["id"].startswith("mistralai/")


def test_llama_not_resolvable():
    assert resolve_user_model_choice("llama 3.1 8b") is None
    assert get_model("meta-llama/Llama-3.1-8B-Instruct") is None


def test_seven_b_band_stays_near_prior_15_50():
    rec = default_model()
    assert rec["train_usd_min"] >= 10
    assert rec["train_usd_max"] <= 50
    fourteen = get_model("Qwen/Qwen2.5-14B-Instruct")
    assert fourteen["train_usd_max"] >= rec["train_usd_max"]
    smol = get_model("HuggingFaceTB/SmolLM2-1.7B-Instruct")
    assert smol["train_usd_max"] < rec["train_usd_max"]


def test_train_backend_is_serverless_never_pod():
    from helix.services.runpod_train import (
        COMPUTE_BACKEND,
        assert_serverless_only,
        compute_policy,
    )

    assert COMPUTE_BACKEND == "runpod_serverless"
    assert compute_policy()["idle_charge"] is False
    assert "pod" in compute_policy()["forbidden"]
    try:
        assert_serverless_only("pod")
        assert False, "pods must be rejected"
    except ValueError as e:
        assert "pay-per-run" in str(e).lower() or "always-on" in str(e).lower()
