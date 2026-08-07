"""Local CPU LLM backend, via Ollama. See design doc §5.2.

Used for dev/testing and the offline reflection job — never for GPU
inference (CLAUDE.md invariant 1: zero VRAM). `client` is injectable so
tests can fake it without a running Ollama instance.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Protocol, Self

import httpx

from chao.brain.backend import Message


class _StreamResponse(Protocol):
    def raise_for_status(self) -> None: ...
    def aiter_lines(self) -> AsyncIterator[str]: ...

    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, *exc: object) -> None: ...


class _HTTPClient(Protocol):
    def stream(self, method: str, url: str, **kwargs: object) -> _StreamResponse: ...


class OllamaBackend:
    def __init__(
        self,
        model: str,
        *,
        base_url: str = "http://localhost:11434",
        client: _HTTPClient | None = None,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._client = client

    async def stream(
        self, system: str, messages: list[Message], cancel: asyncio.Event
    ) -> AsyncIterator[str]:
        payload = {
            "model": self._model,
            "messages": [{"role": "system", "content": system}]
            + [{"role": m.role, "content": m.content} for m in messages],
            "stream": True,
        }

        client = self._client
        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient()

        try:
            async with client.stream(
                "POST", f"{self._base_url}/api/chat", json=payload, timeout=None
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    content = chunk.get("message", {}).get("content", "")
                    if content:
                        yield content
                    # Same cooperative-checkpoint tradeoff as cloud.py: good
                    # enough between chunks, backstopped by bus.py's
                    # hard-cancel if the connection stalls entirely.
                    if cancel.is_set() or chunk.get("done"):
                        return
        finally:
            if owns_client:
                await client.aclose()
