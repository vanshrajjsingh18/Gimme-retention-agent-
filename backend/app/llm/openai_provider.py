"""OpenAI-compatible chat-completions provider.

Works against any endpoint exposing ``POST /chat/completions`` with the OpenAI
request shape (OpenAI, Azure OpenAI gateways, vLLM, Ollama's compat layer,
OpenRouter). Configured through LLM_BASE_URL / LLM_API_KEY / LLM_MODEL.
"""
from __future__ import annotations

import logging

import httpx

from app.core.config import settings
from app.llm.base import LLMError, LLMProvider, LLMResponse

logger = logging.getLogger(__name__)


class OpenAICompatibleProvider(LLMProvider):
    name = "openai"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
    ) -> None:
        self.api_key = api_key or settings.LLM_API_KEY
        self.base_url = (base_url or settings.LLM_BASE_URL).rstrip("/")
        self.model = model or settings.LLM_MODEL
        self.timeout = timeout or settings.LLM_TIMEOUT_SECONDS

    def complete(self, system_prompt: str, user_prompt: str, *, max_tokens: int = 900) -> LLMResponse:
        if not self.api_key:
            raise LLMError(
                "No LLM API key is configured. Set LLM_API_KEY, or set LLM_PROVIDER=mock "
                "to generate messages locally."
            )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.7,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            raise LLMError(f"Could not reach the LLM provider: {exc}") from exc

        if response.status_code >= 400:
            # Never surface the response body verbatim; it can echo the request.
            raise LLMError(
                f"LLM provider returned HTTP {response.status_code}. "
                "Check LLM_BASE_URL, LLM_MODEL and LLM_API_KEY."
            )

        try:
            data = response.json()
            choice = data["choices"][0]
            text = choice["message"]["content"]
            finish = choice.get("finish_reason", "stop")
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"Unexpected response shape from the LLM provider: {exc}") from exc

        return LLMResponse(text=text, provider=self.name, model=self.model, finish_reason=finish)

    def health(self) -> dict:
        if not self.api_key:
            return {
                "provider": self.name,
                "model": self.model,
                "status": "NOT_CONFIGURED",
                "mode": "live",
                "message": "LLM_API_KEY is not set.",
            }
        try:
            with httpx.Client(timeout=10) as client:
                response = client.get(
                    f"{self.base_url}/models",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
            ok = response.status_code < 400
            return {
                "provider": self.name,
                "model": self.model,
                "status": "OK" if ok else "ERROR",
                "mode": "live",
                "message": (
                    f"Connected to {self.base_url}."
                    if ok
                    else f"Provider returned HTTP {response.status_code}."
                ),
            }
        except httpx.HTTPError as exc:
            return {
                "provider": self.name,
                "model": self.model,
                "status": "ERROR",
                "mode": "live",
                "message": f"Could not reach {self.base_url}: {exc.__class__.__name__}",
            }
