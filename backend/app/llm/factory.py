"""Provider selection."""
from __future__ import annotations

from app.core.config import settings
from app.llm.base import LLMProvider
from app.llm.mock_provider import MockLLMProvider
from app.llm.openai_provider import OpenAICompatibleProvider


def get_llm_provider(name: str | None = None) -> LLMProvider:
    """Return the configured provider, falling back to MOCK.

    MOCK is the default whenever no API key is present so the product remains
    fully demonstrable without credentials.
    """
    provider_name = (name or settings.LLM_PROVIDER or "mock").lower()
    if provider_name in ("openai", "openai-compatible", "live") and settings.LLM_API_KEY:
        return OpenAICompatibleProvider()
    return MockLLMProvider()


def provider_mode() -> str:
    return "mock" if isinstance(get_llm_provider(), MockLLMProvider) else "live"
