"""Small mining jobs send several search questions in one Apify run."""

from helix.services.gather.apify_client import _query_list, search_web
from helix.services.pipeline_modes import gather_attempt_plan


def test_query_list_splits_lines_and_lists():
    assert _query_list("one\ntwo\n") == ["one", "two"]
    assert _query_list([" a ", "", "b"]) == ["a", "b"]
    assert _query_list("solo") == ["solo"]


def test_small_job_is_two_attempts_no_deep():
    plan = gather_attempt_plan(5)
    assert plan["max_attempts"] == 2
    assert plan["queries_per_attempt"] == (2, 3)
    assert plan["deep"] is False
    assert plan["force_refresh"] is False


def test_large_job_keeps_three_attempts():
    plan = gather_attempt_plan(40)
    assert plan["max_attempts"] == 3
    assert plan["queries_per_attempt"] == (2, 3, 3)


def test_search_web_sends_queries_in_one_actor_run(monkeypatch):
    seen: dict = {}

    def fake_run(actor_id, run_input, **_kw):
        seen["actor"] = actor_id
        seen["queries"] = run_input["queries"]
        return {"id": "run1", "defaultDatasetId": "ds1", "status": "SUCCEEDED"}

    monkeypatch.setattr(
        "helix.services.gather.apify_client.run_actor", fake_run
    )
    monkeypatch.setattr(
        "helix.services.gather.apify_client.fetch_dataset_items",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "helix.services.cost_tracking.apify_cost_from_run",
        lambda _run: (0.0, "none"),
    )
    items, meta = search_web(
        ["invoice GL coding accounts payable", "new vendor flag review"],
        max_results=10,
    )
    assert items == []
    assert meta["query_count"] == 2
    assert "\n" in seen["queries"]
    assert "invoice GL" in seen["queries"]
    assert seen["actor"] == "apify/google-search-scraper"
