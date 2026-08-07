"""LLM backend protocol and circuit breaker. See design doc §5.

Both backends (cloud.py, local.py) implement `LLMBackend` behind this one
interface, so switching backends is a config change, not a code change.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol

logger = logging.getLogger(__name__)

Role = Literal["user", "assistant"]


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str


class LLMBackend(Protocol):
    async def stream(
        self,
        system: str,
        messages: list[Message],
        cancel: asyncio.Event,
    ) -> AsyncIterator[str]: ...


class CircuitBreakerBackend:
    """§5.3: fall back to `fallback` if `primary` fails or takes longer than
    `first_token_timeout_s` to produce its first token. Once the primary has
    yielded at least one token, no fallback is possible — switching backends
    after text has already been emitted would duplicate it, so from that
    point on any error just propagates.
    """

    def __init__(
        self,
        primary: LLMBackend,
        fallback: LLMBackend,
        *,
        first_token_timeout_s: float = 2.0,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._first_token_timeout_s = first_token_timeout_s

    async def stream(
        self, system: str, messages: list[Message], cancel: asyncio.Event
    ) -> AsyncIterator[str]:
        primary_iter = self._primary.stream(system, messages, cancel)
        try:
            first = await asyncio.wait_for(
                primary_iter.__anext__(), timeout=self._first_token_timeout_s
            )
        except StopAsyncIteration:
            return
        except Exception:  # noqa: BLE001 - any primary failure before first token triggers fallback
            logger.warning("primary backend failed before first token; falling back to local")
            async for chunk in self._fallback.stream(system, messages, cancel):
                yield chunk
            return

        yield first
        async for chunk in primary_iter:
            yield chunk
