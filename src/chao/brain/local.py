"""Local CPU LLM backend, via Ollama. See design doc §5.2.

Used for dev/testing and the offline reflection job — never for GPU
inference (CLAUDE.md invariant 1: zero VRAM). `client` is injectable so
tests can fake it without a running Ollama instance.

**`num_thread`/`think` defaults, from a live investigation (see
SESSION_STATE.md, "big problems" / local backend session):** on this
machine (5900X, 12C/24T), Ollama's unthrottled default thread count
oversubscribes the CPU badly enough to thrash itself *and* stall VTS/OBS
when they're running alongside it — a real turn went from ~5s to ~84s to
first content token with VTS+OBS open, confirmed via `/api/chat`'s own
`prompt_eval_duration`/`eval_duration` both slowing by ~20x, not just one
of them (rules out a prompt-eval-specific artifact; it's raw CPU
contention). Capping `num_thread` to 8 (out of 24 logical) restored
baseline speed with VTS+OBS still open — plenty of headroom left over,
and Ollama's own throughput wasn't hurt by the cap. Separately, qwen3's
default "thinking" mode burns most of the eval budget on tokens that
never reach `content` at all (confirmed: `eval_count` dropped from
~170-250 to ~24 once disabled, with real content arriving almost
immediately after prompt eval instead of only in the final fraction of a
second). Both are per-request `options`/`think` fields, not env vars —
`OLLAMA_NUM_THREAD` only takes effect at Ollama *server* start, so a
shell/machine env var does nothing without restarting the server
(session 5 already hit this same trap for `OLLAMA_NUM_GPU`).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Self

import httpx
import yaml

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
        num_thread: int | None = None,
        think: bool | None = None,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._num_thread = num_thread
        self._think = think

    async def stream(
        self, system: str, messages: list[Message], cancel: asyncio.Event
    ) -> AsyncIterator[str]:
        payload: dict[str, object] = {
            "model": self._model,
            "messages": [{"role": "system", "content": system}]
            + [{"role": m.role, "content": m.content} for m in messages],
            "stream": True,
        }
        if self._num_thread is not None:
            payload["options"] = {"num_thread": self._num_thread}
        if self._think is not None:
            payload["think"] = self._think

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


@dataclass(frozen=True, slots=True)
class OllamaConfig:
    num_thread: int | None = 8
    think: bool = False


def load_ollama_config(path: Path) -> OllamaConfig:
    """Same load/assemble split as every other `*_config` in this project.
    Reads the `ollama:` block out of `config/chao.yaml`. Defaults match the
    live-verified values from the contention investigation above, not
    untested placeholders.
    """
    data = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
    raw = data.get("ollama") or {}
    defaults = OllamaConfig()
    num_thread = raw.get("num_thread", defaults.num_thread)
    return OllamaConfig(
        num_thread=int(num_thread) if num_thread is not None else None,
        think=bool(raw.get("think", defaults.think)),
    )
