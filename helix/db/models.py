"""SQLAlchemy models — multi-tenant Helix state."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    plan: Mapped[str] = mapped_column(String(40), default="starter")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    monthly_budget_usd: Mapped[float] = mapped_column(Float, default=50.0)
    spent_usd: Mapped[float] = mapped_column(Float, default=0.0)
    # Optional per-tenant OpenRouter key override
    openrouter_api_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    openrouter_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    memberships: Mapped[list[Membership]] = relationship(back_populates="tenant")
    contracts: Mapped[list[Contract]] = relationship(back_populates="tenant")


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), default="")
    full_name: Mapped[str] = mapped_column(String(200), default="")
    is_superadmin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    password_set: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    memberships: Mapped[list[Membership]] = relationship(back_populates="user")


class AuthToken(Base):
    """One-time tokens for email verify, password set/reset, invites."""

    __tablename__ = "auth_tokens"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    purpose: Mapped[str] = mapped_column(String(40), index=True)
    # verify_email | reset_password | set_password | invite
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    meta_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class EmailLog(Base):
    """Outbound email audit trail (Resend)."""

    __tablename__ = "email_log"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    to_email: Mapped[str] = mapped_column(String(255), index=True)
    subject: Mapped[str] = mapped_column(String(500))
    template: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(40), default="queued")
    provider_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "tenant_id", name="uq_user_tenant"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    role: Mapped[str] = mapped_column(String(40), default="member")  # owner|admin|member
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="memberships")
    tenant: Mapped[Tenant] = relationship(back_populates="memberships")


class CategoryState(Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_tenant_category"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    phase_target: Mapped[int] = mapped_column(Integer, default=50)
    verified_count: Mapped[int] = mapped_column(Integer, default=0)
    verification_rate_14d: Mapped[float] = mapped_column(Float, default=0.0)
    cost_per_verified_14d: Mapped[float] = mapped_column(Float, default=0.0)
    weeks_missed_target: Mapped[int] = mapped_column(Integer, default=0)


class WorkQueueItem(Base):
    __tablename__ = "work_queue"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    category: Mapped[str] = mapped_column(String(80))
    source: Mapped[str] = mapped_column(String(80))
    priority_score: Mapped[float] = mapped_column(Float, default=0.0)
    assigned_agent: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DiscoveryCandidate(Base):
    __tablename__ = "discovery_candidates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    category: Mapped[str] = mapped_column(String(80))
    source: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    brand: Mapped[str | None] = mapped_column(String(200), nullable=True)
    creator: Mapped[str | None] = mapped_column(String(200), nullable=True)
    content_date: Mapped[str | None] = mapped_column(String(40), nullable=True)
    relevance_score: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(40), default="pending")
    claimed_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RecentSearch(Base):
    __tablename__ = "recent_searches"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    source: Mapped[str] = mapped_column(String(80))
    query: Mapped[str] = mapped_column(String(500))
    category: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RawLakeItem(Base):
    __tablename__ = "raw_lake"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    candidate_id: Mapped[str] = mapped_column(String(32), index=True)
    content_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvidenceStaging(Base):
    __tablename__ = "evidence_staging"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    candidate_id: Mapped[str] = mapped_column(String(32), index=True)
    brand: Mapped[str | None] = mapped_column(String(200), nullable=True)
    creator: Mapped[str | None] = mapped_column(String(200), nullable=True)
    content_date: Mapped[str | None] = mapped_column(String(40), nullable=True)
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    preliminary_confidence: Mapped[float] = mapped_column(Float, default=0.5)
    identity_signals_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="pending_dedup")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    brand: Mapped[str | None] = mapped_column(String(200), nullable=True)
    creator: Mapped[str | None] = mapped_column(String(200), nullable=True)
    category: Mapped[str | None] = mapped_column(String(80), nullable=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    verification_status: Mapped[str] = mapped_column(String(40), default="pending")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    verification_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    more_evidence_cycles: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CampaignEvidence(Base):
    __tablename__ = "campaign_evidence"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    campaign_id: Mapped[str] = mapped_column(String(32), index=True)
    staging_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source: Mapped[str | None] = mapped_column(String(80), nullable=True)
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AmbiguousMatch(Base):
    __tablename__ = "ambiguous_matches"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    staging_id: Mapped[str] = mapped_column(String(32))
    match_score: Mapped[float] = mapped_column(Float)
    candidate_campaign_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourceReliability(Base):
    __tablename__ = "source_reliability"
    __table_args__ = (UniqueConstraint("tenant_id", "source", name="uq_tenant_source"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    source: Mapped[str] = mapped_column(String(80))
    reliability: Mapped[float] = mapped_column(Float, default=0.7)


class OntologyType(Base):
    __tablename__ = "ontology"
    __table_args__ = (UniqueConstraint("tenant_id", "type_name", name="uq_tenant_ontology"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    type_name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(40))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class CandidateFact(Base):
    __tablename__ = "candidate_facts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    campaign_id: Mapped[str] = mapped_column(String(32), index=True)
    entity: Mapped[str] = mapped_column(String(300))
    fact_type: Mapped[str] = mapped_column(String(120))
    value: Mapped[str] = mapped_column(Text)
    relationship: Mapped[str | None] = mapped_column(String(120), nullable=True)
    citation: Mapped[str] = mapped_column(Text)
    is_inferred: Mapped[bool] = mapped_column(Boolean, default=False)
    extraction_confidence: Mapped[float] = mapped_column(Float, default=0.5)
    status: Mapped[str] = mapped_column(String(40), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GraphFact(Base):
    __tablename__ = "graph_facts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    entity: Mapped[str] = mapped_column(String(300), index=True)
    fact_type: Mapped[str] = mapped_column(String(120))
    value: Mapped[str] = mapped_column(Text)
    relationship: Mapped[str | None] = mapped_column(String(120), nullable=True)
    citation: Mapped[str] = mapped_column(Text)
    campaign_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Contradiction(Base):
    __tablename__ = "contradictions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    fact_a_id: Mapped[str] = mapped_column(String(32))
    fact_b_id: Mapped[str] = mapped_column(String(32))
    resolution_status: Mapped[str] = mapped_column(String(40), default="open")
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StrategicAnalysis(Base):
    __tablename__ = "strategic_analyses"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    cluster_key: Mapped[str] = mapped_column(String(200))
    analysis_json: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ResearchMemory(Base):
    __tablename__ = "research_memory"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    topic: Mapped[str] = mapped_column(String(200), index=True)
    summary: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ResearchJournal(Base):
    __tablename__ = "research_journal"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    entry: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SeedMaterial(Base):
    __tablename__ = "seed_material"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    topic: Mapped[str] = mapped_column(String(120))
    input_text: Mapped[str] = mapped_column(Text)
    output_text: Mapped[str] = mapped_column(Text)
    reasoning_pattern: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TrainingExample(Base):
    __tablename__ = "training_examples"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    seed_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    topic: Mapped[str] = mapped_column(String(120))
    input_text: Mapped[str] = mapped_column(Text)
    output_text: Mapped[str] = mapped_column(Text)
    difficulty: Mapped[str] = mapped_column(String(40), default="moderate")
    is_negative: Mapped[bool] = mapped_column(Boolean, default=False)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_status: Mapped[str] = mapped_column(String(40), default="draft")
    review_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    reserved_for_benchmark: Mapped[bool] = mapped_column(Boolean, default=False)
    split: Mapped[str | None] = mapped_column(String(20), nullable=True)
    dataset_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserDataScope(Base):
    """Per-user generation scope: gold targets + synthesis multipliers. Stored forever."""

    __tablename__ = "user_data_scopes"
    __table_args__ = (
        UniqueConstraint("user_id", "tenant_id", name="uq_user_tenant_scope"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    gold_target_count: Mapped[int] = mapped_column(Integer, default=5000)
    variations_per_gold: Mapped[int] = mapped_column(Integer, default=4)
    # JSON list of parameter keys the user selected to vary during synthesis
    vary_parameters_json: Mapped[str] = mapped_column(
        Text,
        default='["tone","difficulty","persona","context","locale"]',
    )
    auto_promote_approved: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GoldExample(Base):
    """Vetted real-world gold training rows — owned by a user account indefinitely."""

    __tablename__ = "gold_examples"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    topic: Mapped[str] = mapped_column(String(120), index=True)
    input_text: Mapped[str] = mapped_column(Text)
    output_text: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    difficulty: Mapped[str] = mapped_column(String(40), default="moderate")
    is_negative: Mapped[bool] = mapped_column(Boolean, default=False)
    source_kind: Mapped[str] = mapped_column(String(40), default="mined")
    # mined | curated | imported | pipeline
    source_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verification_status: Mapped[str] = mapped_column(String(40), default="verified")
    tags_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Indefinite retention — never auto-expires
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SyntheticExample(Base):
    """Synthesized variations of gold — owned by the same user account indefinitely."""

    __tablename__ = "synthetic_examples"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    gold_id: Mapped[str] = mapped_column(ForeignKey("gold_examples.id"), index=True)
    topic: Mapped[str] = mapped_column(String(120), index=True)
    input_text: Mapped[str] = mapped_column(Text)
    output_text: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    difficulty: Mapped[str] = mapped_column(String(40), default="moderate")
    is_negative: Mapped[bool] = mapped_column(Boolean, default=False)
    variation_index: Mapped[int] = mapped_column(Integer, default=1)
    varied_parameters_json: Mapped[str] = mapped_column(Text, default="{}")
    synthesis_run_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SynthesisRun(Base):
    """A user-triggered synthesis job over their gold library."""

    __tablename__ = "synthesis_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    status: Mapped[str] = mapped_column(String(40), default="pending")
    # pending | running | completed | error
    gold_requested: Mapped[int] = mapped_column(Integer, default=0)
    gold_processed: Mapped[int] = mapped_column(Integer, default=0)
    variations_per_gold: Mapped[int] = mapped_column(Integer, default=4)
    synthesized_count: Mapped[int] = mapped_column(Integer, default=0)
    parameters_json: Mapped[str] = mapped_column(Text, default="[]")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BatchJob(Base):
    """Persistent multi-batch jobs (pipeline or synthesis). Survive user logout."""

    __tablename__ = "batch_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    job_type: Mapped[str] = mapped_column(String(40), index=True)
    # pipeline | synthesis
    # 1 = best quality (all 15 agents), 4 = ultra lean (lowest cost)
    quality_mode: Mapped[int] = mapped_column(Integer, default=2)
    batch_size: Mapped[int] = mapped_column(Integer, default=5)
    total_batches: Mapped[int] = mapped_column(Integer, default=1)
    completed_batches: Mapped[int] = mapped_column(Integer, default=0)
    auto_continue: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    # pending | running | completed | failed | cancelled
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    progress_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    items_processed: Mapped[int] = mapped_column(Integer, default=0)
    last_batch_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    avg_batch_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    eta_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    result_summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BatchJobEvent(Base):
    """Per-batch log lines for UI."""

    __tablename__ = "batch_job_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("batch_jobs.id"), index=True)
    batch_index: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text)
    level: Mapped[str] = mapped_column(String(20), default="info")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GatherJob(Base):
    """Apify gather run — never performed by the LLM."""

    __tablename__ = "gather_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    source: Mapped[str] = mapped_column(String(80))
    category: Mapped[str] = mapped_column(String(80), default="")
    query: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(40), default="pending")
    # pending | running | completed | cached | error
    apify_run_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    apify_dataset_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    needs_judgment_count: Mapped[int] = mapped_column(Integer, default=0)
    from_cache: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GatherItem(Base):
    """Normalized item from Apify — source of truth for evidence (not LLM)."""

    __tablename__ = "gather_items"
    __table_args__ = (
        UniqueConstraint("tenant_id", "content_hash", name="uq_tenant_gather_hash"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    gather_job_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    source: Mapped[str] = mapped_column(String(80), index=True)
    category: Mapped[str] = mapped_column(String(80), default="")
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(500), default="")
    brand: Mapped[str | None] = mapped_column(String(200), nullable=True)
    creator: Mapped[str | None] = mapped_column(String(200), nullable=True)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_json: Mapped[str] = mapped_column(Text, default="{}")
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    relevance_score: Mapped[float] = mapped_column(Float, default=0.0)
    needs_judgment: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    status: Mapped[str] = mapped_column(String(40), default="gathered", index=True)
    # gathered | staged | judged | discarded
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GatherCache(Base):
    """Query-level cache so Apify is not re-hit within the dedupe window."""

    __tablename__ = "gather_cache"
    __table_args__ = (
        UniqueConstraint("tenant_id", "cache_key", name="uq_tenant_gather_cache"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    cache_key: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(80))
    query: Mapped[str] = mapped_column(String(500))
    category: Mapped[str] = mapped_column(String(80), default="")
    payload_json: Mapped[str] = mapped_column(Text)
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BenchmarkVersion(Base):
    __tablename__ = "benchmark_versions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    version: Mapped[str] = mapped_column(String(40))
    composition_json: Mapped[str] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DatasetVersion(Base):
    __tablename__ = "dataset_versions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    version: Mapped[str] = mapped_column(String(40))
    manifest_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TrainingRun(Base):
    __tablename__ = "training_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    dataset_version: Mapped[str] = mapped_column(String(40))
    config_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default="pending")
    scores_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision: Mapped[str | None] = mapped_column(String(40), nullable=True)
    decision_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelRecord(Base):
    __tablename__ = "models"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    dataset_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    training_config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    eval_scores_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    git_commit: Mapped[str | None] = mapped_column(String(80), nullable=True)
    is_production: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Escalation(Base):
    __tablename__ = "escalations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    source_agent: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(80))
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default="open")
    human_decision: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Contract(Base):
    __tablename__ = "contracts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    client_name: Mapped[str] = mapped_column(String(200))
    allowed_categories_json: Mapped[str] = mapped_column(Text)
    excluded_competitors_json: Mapped[str] = mapped_column(Text)
    allowed_sources_json: Mapped[str] = mapped_column(Text)
    brand_voice: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    tenant: Mapped[Tenant] = relationship(back_populates="contracts")


class ScopeFlag(Base):
    __tablename__ = "scope_flags"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    action_json: Mapped[str] = mapped_column(Text)
    result: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Digest(Base):
    __tablename__ = "digests"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EventLog(Base):
    __tablename__ = "event_log"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    agent: Mapped[str] = mapped_column(String(80))
    event_type: Mapped[str] = mapped_column(String(80))
    message: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentHealth(Base):
    __tablename__ = "agent_health"
    __table_args__ = (UniqueConstraint("tenant_id", "agent", name="uq_tenant_agent"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    agent: Mapped[str] = mapped_column(String(80))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str] = mapped_column(String(40), default="never")
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    run_count: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)


class ReallocationLog(Base):
    __tablename__ = "reallocation_log"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TopicSchema(Base):
    __tablename__ = "topic_schemas"
    __table_args__ = (UniqueConstraint("tenant_id", "topic", name="uq_tenant_topic"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    topic: Mapped[str] = mapped_column(String(120))
    display_name: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    schema_json: Mapped[str] = mapped_column(Text)
    sample_row_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    export_format: Mapped[str] = mapped_column(String(40), default="jsonl")  # jsonl | chat | preference
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ResearchProject(Base):
    """Research brief that defines what gold data this mining line is for."""

    __tablename__ = "research_projects"
    __table_args__ = (UniqueConstraint("tenant_id", "slug", name="uq_tenant_project_slug"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    slug: Mapped[str] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(200))
    # What domain / product this gold data trains
    domain: Mapped[str] = mapped_column(Text, default="")
    # Free-text mission for Research Director and all agents
    mission: Mapped[str] = mapped_column(Text, default="")
    # JSON list of research questions
    research_questions_json: Mapped[str] = mapped_column(Text, default="[]")
    # JSON list of allowed sources / channels
    sources_json: Mapped[str] = mapped_column(Text, default="[]")
    # JSON list of categories or topic buckets
    categories_json: Mapped[str] = mapped_column(Text, default="[]")
    # JSON map category -> phase target counts
    phase_targets_json: Mapped[str] = mapped_column(Text, default="{}")
    # JSON list of success metrics definitions
    success_metrics_json: Mapped[str] = mapped_column(Text, default="[]")
    # JSON list of topic keys that define gold example formats
    topic_keys_json: Mapped[str] = mapped_column(Text, default="[]")
    # Extra instructions injected into every agent
    agent_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Output notes (how gold data should look at export)
    output_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentRun(Base):
    """Audit trail of agent executions."""

    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    agent: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(40), default="running")
    trigger: Mapped[str] = mapped_column(String(40), default="manual")
    input_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_trace_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
