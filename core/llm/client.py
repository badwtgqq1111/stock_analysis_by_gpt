"""DeepSeek LLM client with OpenAI-compatible API."""

from __future__ import annotations

import json
import os
import time
from typing import Any

import requests


DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TEMPERATURE = 0.7
REQUEST_TIMEOUT = 120


class LLMClient:
    """Thin wrapper around DeepSeek's OpenAI-compatible chat completions API."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ):
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL)

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _chat_endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        model: str | None = None,
    ) -> str:
        """Send a chat request and return the assistant's text response."""
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        resp = requests.post(
            self._chat_endpoint(),
            headers=self._headers,
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )

        if resp.status_code != 200:
            raise RuntimeError(
                f"LLM API error {resp.status_code}: {resp.text[:500]}"
            )

        data = resp.json()
        return data["choices"][0]["message"]["content"]

    def chat_with_retry(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        model: str | None = None,
        max_retries: int = 3,
        retry_delay: float = 2.0,
    ) -> str:
        """Chat with automatic retry on failure."""
        last_error: Exception | None = None
        for attempt in range(max_retries):
            try:
                return self.chat(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    model=model,
                )
            except Exception as exc:
                last_error = exc
                if attempt < max_retries - 1:
                    time.sleep(retry_delay * (attempt + 1))
        raise last_error  # type: ignore[misc]
