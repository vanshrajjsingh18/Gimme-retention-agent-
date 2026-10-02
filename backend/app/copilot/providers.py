"""Tool-calling model providers behind one interface.

The engine speaks a provider-neutral transcript:

* ``{"role": "user", "content": str}``
* ``{"role": "assistant", "content": str, "tool_calls": [{"id", "name", "arguments"}], "raw": ...}``
* ``{"role": "tool", "tool_call_id": str, "name": str, "content": str}``

Each provider translates that to its own wire format. All calls are made
server-side; keys never leave the backend and are never logged.
"""
from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger("app.copilot")

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


class AIProviderError(RuntimeError):
    """The model could not be reached or returned something unusable."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class AIResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    usage: dict = field(default_factory=dict)
    stop_reason: str = ""
    #: Provider-native assistant content, replayed verbatim on the next call
    #: (Anthropic thinking blocks must come back unchanged).
    raw: Any = None
    refused: bool = False
    latency_ms: float = 0.0


class AIProvider(ABC):
    name = "base"
    model = "unknown"
    mode = "live"

    @abstractmethod
    def chat(self, *, system: str, messages: list[dict], tools: list[dict], context: dict) -> AIResponse:
        """One model turn: either text, or tool calls to run."""


# --------------------------------------------------------------------------
# OpenAI-compatible (OpenAI, Azure gateways, vLLM, OpenRouter, Ollama compat)
# --------------------------------------------------------------------------
class OpenAICompatibleProvider(AIProvider):
    name = "openai"

    def __init__(self, *, api_key: str, base_url: str, model: str, timeout: int) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def chat(self, *, system: str, messages: list[dict], tools: list[dict], context: dict) -> AIResponse:
        wire: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "assistant":
                entry: dict = {"role": "assistant", "content": m.get("content") or None}
                if m.get("tool_calls"):
                    entry["tool_calls"] = [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])},
                        }
                        for c in m["tool_calls"]
                    ]
                wire.append(entry)
            elif m["role"] == "tool":
                wire.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
            else:
                wire.append({"role": "user", "content": m["content"]})
        payload = {
            "model": self.model,
            "messages": wire,
            "tools": [{"type": "function", "function": t} for t in tools],
            "tool_choice": "auto",
            "temperature": 0.2,
        }
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
        except httpx.HTTPError as exc:
            raise AIProviderError(f"Could not reach the AI provider ({exc.__class__.__name__}).") from exc
        if response.status_code >= 400:
            # The body can echo the request; never surface it.
            raise AIProviderError(f"The AI provider returned HTTP {response.status_code}.")
        try:
            data = response.json()
            choice = data["choices"][0]
            message = choice["message"]
            calls = [
                ToolCall(
                    id=c["id"],
                    name=c["function"]["name"],
                    arguments=json.loads(c["function"].get("arguments") or "{}"),
                )
                for c in message.get("tool_calls") or []
            ]
        except (KeyError, IndexError, ValueError) as exc:
            raise AIProviderError("The AI provider returned an unexpected response.") from exc
        usage = data.get("usage") or {}
        return AIResponse(
            text=message.get("content") or "",
            tool_calls=calls,
            provider=self.name,
            model=self.model,
            usage={"input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens")},
            stop_reason=choice.get("finish_reason", ""),
            latency_ms=(time.perf_counter() - started) * 1000,
        )


# --------------------------------------------------------------------------
# Anthropic (official SDK)
# --------------------------------------------------------------------------
class AnthropicProvider(AIProvider):
    name = "anthropic"

    def __init__(self, *, api_key: str | None, base_url: str | None, model: str, timeout: int,
                 effort: str, fallbacks: bool) -> None:
        import anthropic

        kwargs: dict = {"timeout": float(timeout), "max_retries": 2}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(**kwargs)
        self.model = model
        self.effort = effort
        self.fallbacks = fallbacks

    def _wire_messages(self, messages: list[dict]) -> list[dict]:
        wire: list[dict] = []
        pending_results: list[dict] = []

        def flush() -> None:
            if pending_results:
                wire.append({"role": "user", "content": list(pending_results)})
                pending_results.clear()

        for m in messages:
            if m["role"] == "tool":
                pending_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": m["tool_call_id"],
                        "content": m["content"],
                        **({"is_error": True} if m.get("is_error") else {}),
                    }
                )
                continue
            flush()
            if m["role"] == "assistant":
                raw = m.get("raw")
                if raw and m.get("raw_provider") == self.name and m.get("raw_model") == self.model:
                    # Replayed exactly as received, thinking blocks included.
                    wire.append({"role": "assistant", "content": raw})
                    continue
                blocks: list[dict] = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for c in m.get("tool_calls") or []:
                    blocks.append({"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]})
                if blocks:
                    wire.append({"role": "assistant", "content": blocks})
            else:
                wire.append({"role": "user", "content": m["content"]})
        flush()
        return wire

    def chat(self, *, system: str, messages: list[dict], tools: list[dict], context: dict) -> AIResponse:
        anthropic = self._anthropic
        request = {
            "model": self.model,
            "max_tokens": 16000,
            "system": system,
            "messages": self._wire_messages(messages),
            "tools": [
                {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                for t in tools
            ],
            "output_config": {"effort": self.effort},
            "cache_control": {"type": "ephemeral"},
        }
        started = time.perf_counter()
        try:
            if self.fallbacks:
                response = self.client.beta.messages.create(
                    **request, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
                )
            else:
                response = self.client.messages.create(**request)
        except anthropic.AuthenticationError as exc:
            raise AIProviderError("The Anthropic API key was rejected. Check AI_API_KEY.") from exc
        except anthropic.RateLimitError as exc:
            raise AIProviderError("The AI provider is rate limiting requests; try again shortly.") from exc
        except anthropic.APIStatusError as exc:
            raise AIProviderError(f"The AI provider returned HTTP {exc.status_code}.") from exc
        except anthropic.APIConnectionError as exc:
            raise AIProviderError("Could not reach the AI provider.") from exc

        latency = (time.perf_counter() - started) * 1000
        usage = getattr(response, "usage", None)
        usage_dict = {
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
        }
        if response.stop_reason == "refusal":
            return AIResponse(
                text="The AI model declined this request. Nothing was changed.",
                provider=self.name,
                model=getattr(response, "model", self.model),
                usage=usage_dict,
                stop_reason="refusal",
                refused=True,
                latency_ms=latency,
            )
        texts, calls = [], []
        for block in response.content:
            if block.type == "text":
                texts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {})))
        return AIResponse(
            text="\n".join(t for t in texts if t).strip(),
            tool_calls=calls,
            provider=self.name,
            model=self.model,
            usage=usage_dict,
            stop_reason=response.stop_reason or "",
            raw=[block.to_dict() for block in response.content],
            latency_ms=latency,
        )


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------
def _resolved() -> tuple[str, str | None, str, str]:
    """(provider, api_key, model, base_url) after falling back to the LLM_* settings."""
    name = (settings.AI_PROVIDER or "").strip().lower()
    if not name:
        legacy = (settings.LLM_PROVIDER or "").lower()
        name = "openai" if legacy in ("openai", "openai-compatible", "live") and settings.LLM_API_KEY else "mock"
    if name in ("openai", "openai-compatible"):
        key = settings.AI_API_KEY or settings.LLM_API_KEY
        model = settings.AI_MODEL or settings.LLM_MODEL or DEFAULT_OPENAI_MODEL
        base = settings.AI_BASE_URL or settings.LLM_BASE_URL
        return ("openai" if key else "mock"), key, model, base
    if name in ("anthropic", "claude"):
        return "anthropic", settings.AI_API_KEY or None, settings.AI_MODEL or DEFAULT_ANTHROPIC_MODEL, settings.AI_BASE_URL
    return "mock", None, "", ""


def get_ai_provider() -> AIProvider:
    name, key, model, base = _resolved()
    if name == "openai":
        return OpenAICompatibleProvider(api_key=key, base_url=base, model=model, timeout=settings.AI_TIMEOUT_SECONDS)
    if name == "anthropic":
        return AnthropicProvider(
            api_key=key,
            base_url=base or None,
            model=model,
            timeout=settings.AI_TIMEOUT_SECONDS,
            effort=settings.AI_EFFORT,
            fallbacks=settings.AI_ANTHROPIC_FALLBACKS,
        )
    from app.copilot.mock import MockAIProvider

    return MockAIProvider()


def provider_info() -> dict:
    """What the Copilot runs on, without making a network call."""
    name, _, model, _ = _resolved()
    if name == "mock":
        from app.copilot.mock import MockAIProvider

        return {
            "provider": "mock",
            "model": MockAIProvider.model,
            "mode": "mock",
            "note": "Deterministic offline planner. Set AI_PROVIDER and AI_API_KEY for a live model.",
        }
    return {"provider": name, "model": model, "mode": "live"}
