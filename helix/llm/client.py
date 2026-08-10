"""OpenRouter-first LLM client (OpenAI-compatible). Local can fall back to xAI."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

from helix.config import Settings, get_settings
from helix.db.models import Tenant


@dataclass
class LLMClient:
    api_key: str
    base_url: str
    model: str
    provider: str
    site_url: str = ""
    site_name: str = "Helix"
    default_headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        headers = dict(self.default_headers)
        if self.provider == "openrouter":
            if self.site_url:
                headers["HTTP-Referer"] = self.site_url
            if self.site_name:
                headers["X-Title"] = self.site_name
        self._client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            default_headers=headers or None,
        )

    def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict | None = "auto",
    ) -> Any:
        """Chat Completions API — works on OpenRouter and xAI."""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"
        return self._client.chat.completions.create(**kwargs)


def build_client_from_settings(settings: Settings | None = None) -> LLMClient:
    s = settings or get_settings()
    if s.llm_provider == "none":
        raise RuntimeError(
            "No LLM key configured. Set OPENROUTER_API_KEY (recommended for VPS) "
            "or XAI_API_KEY for local direct xAI."
        )
    return LLMClient(
        api_key=s.llm_api_key,
        base_url=s.llm_base_url,
        model=s.llm_model,
        provider=s.llm_provider,
        site_url=s.openrouter_site_url or s.helix_base_url,
        site_name=s.openrouter_site_name,
    )


def get_llm_client_for_tenant(tenant: Tenant | None = None) -> LLMClient:
    """Prefer per-tenant OpenRouter credentials, else platform defaults."""
    s = get_settings()
    if tenant and tenant.openrouter_api_key:
        return LLMClient(
            api_key=tenant.openrouter_api_key,
            base_url=s.openrouter_base_url,
            model=tenant.openrouter_model or s.openrouter_model,
            provider="openrouter",
            site_url=s.openrouter_site_url or s.helix_base_url,
            site_name=s.openrouter_site_name,
        )
    return build_client_from_settings(s)


def estimate_cost_usd(prompt_tokens: int, completion_tokens: int) -> float:
    """Rough cost estimate; OpenRouter bills vary by model."""
    # Conservative midpoint for Grok-class models via OpenRouter
    return (prompt_tokens * 2.0 + completion_tokens * 6.0) / 1_000_000


def serialize_tool_calls(message: Any) -> list[dict[str, Any]]:
    calls = []
    for tc in message.tool_calls or []:
        calls.append(
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
        )
    return calls


def parse_tool_args(arguments: str) -> dict[str, Any]:
    try:
        return json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {"_raw": arguments}
