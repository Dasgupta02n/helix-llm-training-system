"""OpenAI-compatible tool schemas for all Helix agent tools."""

from __future__ import annotations

from typing import Any


def _t(
    name: str,
    description: str,
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    props = properties or {}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": required or [],
            },
        },
    }


def _s(desc: str = "") -> dict[str, Any]:
    return {"type": "string", "description": desc}


def _n(desc: str = "") -> dict[str, Any]:
    return {"type": "number", "description": desc}


def _b(desc: str = "") -> dict[str, Any]:
    return {"type": "boolean", "description": desc}


def _i(desc: str = "") -> dict[str, Any]:
    return {"type": "integer", "description": desc}


TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    # Shared research brief / schema
    "get_research_brief": _t(
        "get_research_brief",
        "Get the active Research Brief (domain, mission, targets, topics) for this tenant.",
    ),
    "list_topic_schemas": _t(
        "list_topic_schemas",
        "List all active gold-example topic schemas for this tenant.",
    ),
    # Research Director
    "score_all_active": _t("score_all_active", "Score all active category/source combinations by priority."),
    "write_work_queue": _t(
        "write_work_queue",
        "Write discovery work-queue assignments from scored priorities.",
        {"assignments": {"type": "array", "items": {"type": "object"}, "description": "List of {category, source, priority_score}"}},
        ["assignments"],
    ),
    "get_open_contradictions": _t("get_open_contradictions", "List open knowledge-graph contradictions."),
    "apply_auto_resolution": _t(
        "apply_auto_resolution",
        "Apply an automated resolution rule to a contradiction.",
        {"contradiction_id": _s(), "rule": _s("recency | source_reliability"), "note": _s()},
        ["contradiction_id", "rule"],
    ),
    "create_escalation": _t(
        "create_escalation",
        "Create a human escalation.",
        {"kind": _s(), "payload": {"type": "object"}, "message": _s()},
        ["kind", "message"],
    ),
    "write_research_journal": _t(
        "write_research_journal",
        "Write a research journal entry for this cycle.",
        {"entry": _s()},
        ["entry"],
    ),
    # Discovery
    "get_current_assignment": _t("get_current_assignment", "Get this discovery agent's current work-queue assignment."),
    "check_recent_searches": _t(
        "check_recent_searches",
        "Check recent searches for a source within the dedup window.",
        {"source": _s(), "query": _s()},
        ["source"],
    ),
    "trigger_discovery": _t(
        "trigger_discovery",
        "GATHER via Apify only (batched, cached, deduped). Never invent results. "
        "Returns needs_judgment items already code-filtered.",
        {"category": _s(), "source": _s(), "query": _s()},
        ["category", "source", "query"],
    ),
    "get_discovery_results": _t(
        "get_discovery_results",
        "Fetch Apify-gathered, code-filtered results for a gather job. Do not invent items.",
        {"job_id": _s()},
        ["job_id"],
    ),
    "score_relevance": _t(
        "score_relevance",
        "Code-only relevance score (not LLM). Prefer scores already on gather results.",
        {"title": _s(), "category": _s(), "snippet": _s()},
        ["title", "category"],
    ),
    "write_discovery_candidate": _t(
        "write_discovery_candidate",
        "Write a candidate from Apify gather output. Prefer gather_item_id. Never invent URLs.",
        {
            "gather_item_id": _s("ID from trigger_discovery / get_discovery_results"),
            "category": _s(),
            "source": _s(),
            "title": _s(),
            "url": _s(),
            "brand": _s(),
            "creator": _s(),
            "relevance_score": _n(),
        },
        [],
    ),
    "record_search": _t(
        "record_search",
        "Record a search in recent-search memory.",
        {"source": _s(), "query": _s(), "category": _s()},
        ["source", "query"],
    ),
    # Evidence
    "claim_candidate": _t("claim_candidate", "Claim a pending discovery candidate.", {"candidate_id": _s()}, ["candidate_id"]),
    "collect_full_evidence": _t(
        "collect_full_evidence",
        "GATHER full page/post via Apify or cache only. Never invent post text.",
        {"candidate_id": _s()},
        ["candidate_id"],
    ),
    "assess_completeness": _t(
        "assess_completeness",
        "Assess whether collected evidence is complete.",
        {"candidate_id": _s(), "content": _s()},
        ["candidate_id"],
    ),
    "write_to_raw_lake": _t(
        "write_to_raw_lake",
        "Write raw evidence to the data lake.",
        {"candidate_id": _s(), "content": {"type": "object"}},
        ["candidate_id", "content"],
    ),
    "compute_preliminary_confidence": _t(
        "compute_preliminary_confidence",
        "Compute cheap preliminary confidence heuristic.",
        {"candidate_id": _s(), "content_length": _i()},
        ["candidate_id"],
    ),
    "extract_lightweight_signals": _t(
        "extract_lightweight_signals",
        "Extract brand/creator/date identity signals.",
        {"candidate_id": _s(), "brand": _s(), "creator": _s(), "content_date": _s()},
        ["candidate_id"],
    ),
    "write_evidence_staging": _t(
        "write_evidence_staging",
        "Stage evidence for dedup.",
        {
            "candidate_id": _s(),
            "brand": _s(),
            "creator": _s(),
            "content_date": _s(),
            "content_text": _s(),
            "preliminary_confidence": _n(),
            "identity_signals": {"type": "object"},
        },
        ["candidate_id", "content_text"],
    ),
    "discard_candidate": _t(
        "discard_candidate",
        "Discard a thin/broken candidate.",
        {"candidate_id": _s(), "reason": _s()},
        ["candidate_id", "reason"],
    ),
    # Duplicate resolver
    "get_pending_dedup_batch": _t("get_pending_dedup_batch", "Get staged evidence pending dedup."),
    "compute_content_similarity": _t(
        "compute_content_similarity",
        "Compute content similarity against existing campaigns.",
        {"staging_id": _s(), "content_text": _s()},
        ["staging_id"],
    ),
    "get_campaign_identity_signals": _t(
        "get_campaign_identity_signals",
        "Get identity signals for campaigns.",
        {"brand": _s(), "creator": _s()},
    ),
    "compute_match_score": _t(
        "compute_match_score",
        "Combine identity + similarity into a match score.",
        {
            "staging_id": _s(),
            "content_similarity": _n(),
            "brand_match": _b(),
            "creator_match": _b(),
            "date_overlap": _b(),
        },
        ["staging_id", "content_similarity"],
    ),
    "attach_evidence_to_campaign": _t(
        "attach_evidence_to_campaign",
        "Attach staged evidence to an existing campaign.",
        {"staging_id": _s(), "campaign_id": _s()},
        ["staging_id", "campaign_id"],
    ),
    "create_campaign_stub": _t(
        "create_campaign_stub",
        "Create a new pending campaign stub from staged evidence.",
        {"staging_id": _s(), "category": _s(), "title": _s()},
        ["staging_id"],
    ),
    "flag_ambiguous_match": _t(
        "flag_ambiguous_match",
        "Flag an ambiguous match for verification.",
        {"staging_id": _s(), "match_score": _n(), "candidate_campaign_id": _s(), "reasoning": _s()},
        ["staging_id", "match_score", "reasoning"],
    ),
    # Fact verification
    "get_pending_verification_batch": _t("get_pending_verification_batch", "Get campaigns pending verification."),
    "get_source_reliability": _t("get_source_reliability", "Get reliability for a source type.", {"source": _s()}, ["source"]),
    "check_phyllo_profile_consistency": _t(
        "check_phyllo_profile_consistency",
        "Check creator profile consistency (simulated Phyllo).",
        {"creator": _s(), "brand": _s()},
        ["creator"],
    ),
    "get_ambiguous_match_flag": _t("get_ambiguous_match_flag", "Get open ambiguous match for a staging/campaign.", {"staging_id": _s()}),
    "resolve_ambiguous_match": _t(
        "resolve_ambiguous_match",
        "Resolve an ambiguous match.",
        {"match_id": _s(), "resolution": _s("new | existing"), "campaign_id": _s(), "reasoning": _s()},
        ["match_id", "resolution", "reasoning"],
    ),
    "escalate_ambiguous_match": _t(
        "escalate_ambiguous_match",
        "Escalate an ambiguous match to humans.",
        {"match_id": _s(), "reason": _s()},
        ["match_id", "reason"],
    ),
    "update_verification_status": _t(
        "update_verification_status",
        "Update campaign verification status.",
        {
            "campaign_id": _s(),
            "status": _s("verified | rejected | request_more_evidence | pending"),
            "confidence": _n(),
            "reasoning": _s(),
        },
        ["campaign_id", "status", "confidence", "reasoning"],
    ),
    # Knowledge extraction
    "get_unextracted_verified_campaigns": _t("get_unextracted_verified_campaigns", "List verified campaigns needing extraction."),
    "get_campaign_evidence_content": _t("get_campaign_evidence_content", "Get evidence text for a campaign.", {"campaign_id": _s()}, ["campaign_id"]),
    "get_ontology": _t("get_ontology", "Get the extraction ontology."),
    "extract_entities": _t(
        "extract_entities",
        "Extract ontology-constrained entities from text (records proposal only).",
        {"campaign_id": _s(), "entities": {"type": "array", "items": {"type": "object"}}},
        ["campaign_id", "entities"],
    ),
    "extract_relationships": _t(
        "extract_relationships",
        "Extract relationships from text.",
        {"campaign_id": _s(), "relationships": {"type": "array", "items": {"type": "object"}}},
        ["campaign_id", "relationships"],
    ),
    "score_extraction_confidence": _t(
        "score_extraction_confidence",
        "Score extraction confidence for a fact.",
        {"text_support": _s(), "is_inferred": _b()},
        ["text_support"],
    ),
    "write_candidate_fact": _t(
        "write_candidate_fact",
        "Write a candidate fact for graph review.",
        {
            "campaign_id": _s(),
            "entity": _s(),
            "fact_type": _s(),
            "value": _s(),
            "relationship": _s(),
            "citation": _s(),
            "is_inferred": _b(),
            "extraction_confidence": _n(),
        },
        ["campaign_id", "entity", "fact_type", "value", "citation", "extraction_confidence"],
    ),
    "flag_ontology_gap": _t(
        "flag_ontology_gap",
        "Flag content that does not fit the ontology.",
        {"campaign_id": _s(), "description": _s()},
        ["campaign_id", "description"],
    ),
    # Knowledge graph
    "get_pending_graph_writes": _t("get_pending_graph_writes", "Get pending candidate facts."),
    "query_existing_facts": _t(
        "query_existing_facts",
        "Query graph facts for an entity/type.",
        {"entity": _s(), "fact_type": _s()},
        ["entity"],
    ),
    "check_conflict": _t(
        "check_conflict",
        "Check if a new value conflicts with existing facts.",
        {"entity": _s(), "fact_type": _s(), "value": _s()},
        ["entity", "fact_type", "value"],
    ),
    "write_graph_fact": _t(
        "write_graph_fact",
        "Commit a fact to the graph.",
        {
            "candidate_fact_id": _s(),
            "entity": _s(),
            "fact_type": _s(),
            "value": _s(),
            "relationship": _s(),
            "citation": _s(),
            "campaign_id": _s(),
        },
        ["entity", "fact_type", "value", "citation"],
    ),
    "write_contradiction": _t(
        "write_contradiction",
        "Record a CONTRADICTS relationship between two facts.",
        {"fact_a_id": _s(), "fact_b_id": _s()},
        ["fact_a_id", "fact_b_id"],
    ),
    "reject_candidate_fact": _t(
        "reject_candidate_fact",
        "Reject a candidate fact.",
        {"candidate_fact_id": _s(), "reason": _s()},
        ["candidate_fact_id", "reason"],
    ),
    # Strategist
    "get_analysis_candidates": _t("get_analysis_candidates", "Get category clusters ready for analysis."),
    "get_cluster_campaigns": _t("get_cluster_campaigns", "Get verified campaigns in a cluster.", {"cluster_key": _s()}, ["cluster_key"]),
    "get_research_memory": _t("get_research_memory", "Get research memory for a topic.", {"topic": _s()}, ["topic"]),
    "write_strategic_analysis": _t(
        "write_strategic_analysis",
        "Write a strategic analysis record.",
        {"cluster_key": _s(), "analysis": {"type": "object"}, "confidence": _n()},
        ["cluster_key", "analysis"],
    ),
    "write_research_memory_summary": _t(
        "write_research_memory_summary",
        "Update research memory summary.",
        {"topic": _s(), "summary": _s()},
        ["topic", "summary"],
    ),
    # Synthetic generator
    "get_seed_material": _t("get_seed_material", "Get seed material for synthetic expansion."),
    "get_topic_schema": _t("get_topic_schema", "Get training schema for a topic.", {"topic": _s()}, ["topic"]),
    "generate_variation": _t(
        "generate_variation",
        "Register a generated variation (logic held in agent reasoning).",
        {
            "seed_id": _s(),
            "input_text": _s(),
            "output_text": _s(),
            "difficulty": _s(),
            "is_negative": _b(),
            "rationale": _s(),
        },
        ["seed_id", "input_text", "output_text", "difficulty"],
    ),
    "validate_business_logic_invariant": _t(
        "validate_business_logic_invariant",
        "Validate that a variation preserves seed business logic.",
        {"seed_id": _s(), "rationale": _s()},
        ["seed_id", "rationale"],
    ),
    "write_draft_training_example": _t(
        "write_draft_training_example",
        "Write a draft training example for review.",
        {
            "seed_id": _s(),
            "topic": _s(),
            "input_text": _s(),
            "output_text": _s(),
            "difficulty": _s(),
            "is_negative": _b(),
            "rationale": _s(),
        },
        ["topic", "input_text", "output_text", "difficulty"],
    ),
    # Adversarial reviewer
    "get_pending_review_batch": _t("get_pending_review_batch", "Get draft examples pending adversarial review."),
    "construct_counter_argument": _t(
        "construct_counter_argument",
        "Record a counter-argument attempt against a draft.",
        {"example_id": _s(), "counter": _s(), "defeats_original": _b()},
        ["example_id", "counter", "defeats_original"],
    ),
    "reassess_difficulty": _t(
        "reassess_difficulty",
        "Independently reassess difficulty tag.",
        {"example_id": _s(), "difficulty": _s()},
        ["example_id", "difficulty"],
    ),
    "check_negative_example_validity": _t(
        "check_negative_example_validity",
        "Check whether a negative example tests its claimed failure mode.",
        {"example_id": _s(), "valid": _b(), "note": _s()},
        ["example_id", "valid"],
    ),
    "fact_check_against_graph": _t(
        "fact_check_against_graph",
        "Fact-check example claims against the knowledge graph.",
        {"example_id": _s(), "query": _s()},
        ["example_id"],
    ),
    "update_review_status": _t(
        "update_review_status",
        "Approve, reject, or flag a draft example.",
        {"example_id": _s(), "status": _s("approved | rejected | revise"), "reasoning": _s()},
        ["example_id", "status", "reasoning"],
    ),
    # Benchmark builder
    "get_approved_examples_pool": _t("get_approved_examples_pool", "Get approved examples available for benchmark/training."),
    "get_current_benchmark_composition": _t("get_current_benchmark_composition", "Get current benchmark composition stats."),
    "claim_for_benchmark": _t(
        "claim_for_benchmark",
        "Claim an example exclusively for the benchmark suite.",
        {"example_id": _s()},
        ["example_id"],
    ),
    "get_prior_benchmark_versions": _t("get_prior_benchmark_versions", "List prior benchmark versions."),
    "create_benchmark_version": _t(
        "create_benchmark_version",
        "Create a new immutable benchmark version.",
        {"version": _s(), "notes": _s(), "example_ids": {"type": "array", "items": {"type": "string"}}},
        ["version"],
    ),
    "flag_thin_coverage": _t(
        "flag_thin_coverage",
        "Flag thin hard-case coverage for a topic.",
        {"topic": _s(), "note": _s()},
        ["topic", "note"],
    ),
    # Dataset curator
    "get_approved_non_benchmark_examples": _t("get_approved_non_benchmark_examples", "Get approved examples not reserved for benchmark."),
    "check_semantic_duplicates": _t("check_semantic_duplicates", "Find near-duplicate training examples."),
    "assign_splits": _t(
        "assign_splits",
        "Assign train/val/test splits at seed-group level.",
        {"train_ratio": _n(), "val_ratio": _n(), "test_ratio": _n()},
    ),
    "write_training_examples_batch": _t(
        "write_training_examples_batch",
        "Mark examples as exported under a dataset version.",
        {"dataset_version": _s(), "example_ids": {"type": "array", "items": {"type": "string"}}},
        ["dataset_version"],
    ),
    "create_dataset_version": _t(
        "create_dataset_version",
        "Create a new immutable dataset version.",
        {"version": _s(), "manifest": {"type": "object"}},
        ["version", "manifest"],
    ),
    "write_manifest": _t(
        "write_manifest",
        "Write/update manifest content for a dataset version.",
        {"version": _s(), "manifest": {"type": "object"}},
        ["version", "manifest"],
    ),
    "flag_undersized_split": _t(
        "flag_undersized_split",
        "Flag an undersized split rather than exporting it.",
        {"topic": _s(), "split": _s(), "count": _i()},
        ["topic", "split", "count"],
    ),
    # Trainer
    "get_training_run_config": _t("get_training_run_config", "Get training run configuration.", {"dataset_version": _s()}),
    "provision_training_pod": _t("provision_training_pod", "Provision a training pod (simulated)."),
    "run_training_job": _t(
        "run_training_job",
        "Run training job (simulated).",
        {"pod_id": _s(), "dataset_version": _s(), "config": {"type": "object"}},
        ["pod_id"],
    ),
    "run_benchmark_eval": _t(
        "run_benchmark_eval",
        "Evaluate model against benchmark (simulated).",
        {"pod_id": _s(), "model_id": _s()},
        ["pod_id"],
    ),
    "teardown_pod": _t("teardown_pod", "Tear down training pod.", {"pod_id": _s()}, ["pod_id"]),
    "get_baseline_scores": _t("get_baseline_scores", "Get production baseline topic scores."),
    "compare_scores": _t(
        "compare_scores",
        "Compare new scores against baseline.",
        {"new_scores": {"type": "object"}},
        ["new_scores"],
    ),
    "register_model": _t(
        "register_model",
        "Register a trained model; optionally promote to production.",
        {
            "model_id": _s(),
            "dataset_version": _s(),
            "config": {"type": "object"},
            "scores": {"type": "object"},
            "git_commit": _s(),
            "promote": _b(),
            "decision_reasoning": _s(),
        },
        ["model_id", "scores", "decision_reasoning"],
    ),
    # Operations
    "get_success_metrics": _t("get_success_metrics", "Get tenant success metrics snapshot."),
    "get_agent_health": _t("get_agent_health", "Get per-agent operational health."),
    "get_unified_escalation_queue": _t("get_unified_escalation_queue", "Get unified open escalations."),
    "route_human_decision": _t(
        "route_human_decision",
        "Route a human decision back to the originating store.",
        {"escalation_id": _s(), "decision": _s()},
        ["escalation_id", "decision"],
    ),
    "generate_digest": _t("generate_digest", "Generate a weekly activity digest."),
    "write_digest_history": _t(
        "write_digest_history",
        "Persist a digest to history.",
        {"content": _s()},
        ["content"],
    ),
    # Scope guardian
    "get_active_contract": _t("get_active_contract", "Get the active client engagement contract."),
    "validate_action": _t(
        "validate_action",
        "Validate a proposed action against the active contract.",
        {
            "category": _s(),
            "source": _s(),
            "brand": _s(),
            "competitor": _s(),
            "query": _s(),
        },
    ),
    "flag_ambiguous_action": _t(
        "flag_ambiguous_action",
        "Escalate an ambiguous scope action.",
        {"action": {"type": "object"}, "reason": _s()},
        ["reason"],
    ),
    "flag_repeated_violation": _t(
        "flag_repeated_violation",
        "Flag a repeated out-of-scope pattern.",
        {"pattern": _s(), "count": _i()},
        ["pattern", "count"],
    ),
    # Shared
    "log_event": _t(
        "log_event",
        "Write an event to the tenant event log.",
        {"event_type": _s(), "message": _s(), "payload": {"type": "object"}},
        ["event_type", "message"],
    ),
}
