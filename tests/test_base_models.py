"""Double Helix catalog: Apache/MIT only, ≤30B, no Llama."""

from helix.services.base_models import (
    ALLOWED_LICENSES,
    MAX_PARAMS_B,
    get_model,
    public_models,
    recommend_model,
    resolve_user_model_choice,
)


def test_catalog_is_apache_or_mit_and_at_most_30b():
    models = public_models()
    assert len(models) >= 8
    for m in models:
        assert m["license"] in ALLOWED_LICENSES
        assert float(m["params_b"]) <= MAX_PARAMS_B
        assert "llama" not in m["id"].lower()
        assert "llama" not in m["name"].lower()


def test_recommend_hiring_is_not_tiny():
    rec = recommend_model(role_type="hiring", risk_level="high")
    assert rec["params_b"] >= 7
    assert rec["license"] in ALLOWED_LICENSES


def test_user_can_pick_qwen_14_and_phi4():
    q = resolve_user_model_choice("qwen 14")
    assert q and "14" in q["name"]
    p = resolve_user_model_choice("phi-4")
    assert p and p["id"].endswith("phi-4")


def test_llama_not_resolvable():
    assert resolve_user_model_choice("llama 3.1 8b") is None
    assert get_model("meta-llama/Llama-3.1-8B-Instruct") is None
