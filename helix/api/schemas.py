"""Pydantic request/response models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    email: str
    is_superadmin: bool


class TenantCreate(BaseModel):
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9-]+$")
    name: str
    plan: str = "starter"
    monthly_budget_usd: float = 50.0
    openrouter_api_key: str | None = None
    openrouter_model: str | None = None
    owner_email: str | None = None
    owner_password: str | None = None


class TenantOut(BaseModel):
    id: str
    slug: str
    name: str
    plan: str
    is_active: bool
    monthly_budget_usd: float
    spent_usd: float
    openrouter_spent_usd: float | None = None
    apify_spent_usd: float | None = None
    openrouter_model: str | None = None

    class Config:
        from_attributes = True


class MemberAdd(BaseModel):
    email: str
    password: str | None = None
    full_name: str = ""
    role: str = "member"


class AgentRunRequest(BaseModel):
    message: str | None = None


class PipelineRunRequest(BaseModel):
    agents: list[str] | None = None
    message: str | None = None


class EscalationDecision(BaseModel):
    decision: str
