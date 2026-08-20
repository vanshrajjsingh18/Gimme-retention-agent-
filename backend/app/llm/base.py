"""LLM provider abstraction.

Providers receive a fully-formed prompt and return raw text. All grounding,
context assembly and output validation happens outside the provider so that
swapping providers cannot change the safety properties of the system.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class LLMError(RuntimeError):
    """Raised when a provider cannot produce a completion."""


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    finish_reason: str = "stop"
    is_mock: bool = False


class LLMProvider(ABC):
    name: str = "base"
    model: str = "unknown"

    @abstractmethod
    def complete(self, system_prompt: str, user_prompt: str, *, max_tokens: int = 900) -> LLMResponse:
        """Return a completion for the given prompts."""

    @abstractmethod
    def health(self) -> dict:
        """Return provider status for the integrations screen."""
