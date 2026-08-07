"""Cloud LLM backend. See design doc §5.1.

Default for live responses while streaming: best latency and tag-protocol
instruction-following of the two backends. `client` is injectable so tests
can fake it without hitting the network or spending API credits.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Protocol, Self

from chao.brain.backend import Message

DEFAULT_MODEL = "claude-haiku-4-5-20251001"


class _TextStream(Protocol):
    text_stream: AsyncIterator[str]

    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, *exc: object) -> None: ...


class _Messages(Protocol):
    def stream(self, **kwargs: object) -> _TextStream: ...


class _AnthropicClient(Protocol):
    messages: _Messages


class AnthropicBackend:
    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 300,
        client: _AnthropicClient | None = None,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        # Real client constructed lazily so importing this module never
        # requires ANTHROPIC_API_KEY to be set (tests inject a fake).
        self._client = client

    def _resolve_client(self) -> _AnthropicClient:
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic()
        return self._client

    async def stream(
        self, system: str, messages: list[Message], cancel: asyncio.Event
    ) -> AsyncIterator[str]:
        client = self._resolve_client()
        async with client.messages.stream(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=[{"role": m.role, "content": m.content} for m in messages],
        ) as raw:
            async for text in raw.text_stream:
                yield text
                # Checked between chunks, not mid-await: good enough for the
                # common case where chunks arrive steadily. A backend that
                # never yields again is caught by bus.py's hard-cancel
                # escalation instead (CLAUDE.md invariant 5).
                if cancel.is_set():
                    return
