import asyncio

import pytest

from chao.brain.backend import CircuitBreakerBackend


class FakeBackend:
    """Fake LLMBackend: yields `chunks` after an optional `delay`, or raises
    `error` before yielding anything if set.
    """

    def __init__(self, chunks=(), *, delay: float = 0.0, error: Exception | None = None):
        self.chunks = list(chunks)
        self.delay = delay
        self.error = error
        self.calls = 0

    async def stream(self, system, messages, cancel):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        for c in self.chunks:
            yield c


async def collect(aiter):
    return [c async for c in aiter]


async def test_uses_primary_when_it_responds_in_time():
    primary = FakeBackend(["hi", " there"])
    fallback = FakeBackend(["should not see this"])
    breaker = CircuitBreakerBackend(primary, fallback, first_token_timeout_s=1.0)

    result = await collect(breaker.stream("sys", [], asyncio.Event()))

    assert result == ["hi", " there"]
    assert fallback.calls == 0


async def test_falls_back_when_primary_is_slow():
    primary = FakeBackend(["too late"], delay=0.2)
    fallback = FakeBackend(["fallback text"])
    breaker = CircuitBreakerBackend(primary, fallback, first_token_timeout_s=0.05)

    result = await collect(breaker.stream("sys", [], asyncio.Event()))

    assert result == ["fallback text"]


async def test_falls_back_when_primary_raises_before_first_token():
    primary = FakeBackend(error=RuntimeError("api down"))
    fallback = FakeBackend(["fallback text"])
    breaker = CircuitBreakerBackend(primary, fallback, first_token_timeout_s=1.0)

    result = await collect(breaker.stream("sys", [], asyncio.Event()))

    assert result == ["fallback text"]


async def test_no_fallback_once_primary_has_yielded():
    """Once text has already been emitted, switching backends would
    duplicate it — any later error must propagate, not trigger a fallback.
    """
    primary = FakeBackend(["first chunk"])

    async def broken_after_first(system, messages, cancel):
        yield "first chunk"
        raise RuntimeError("dies mid-stream")

    primary.stream = broken_after_first
    fallback = FakeBackend(["should not see this"])
    breaker = CircuitBreakerBackend(primary, fallback, first_token_timeout_s=1.0)

    with pytest.raises(RuntimeError, match="dies mid-stream"):
        await collect(breaker.stream("sys", [], asyncio.Event()))

    assert fallback.calls == 0


async def test_cancellation_propagates_without_falling_back():
    primary = FakeBackend(["too late"], delay=10.0)
    fallback = FakeBackend(["should not see this"])
    breaker = CircuitBreakerBackend(primary, fallback, first_token_timeout_s=10.0)

    async def run():
        return await collect(breaker.stream("sys", [], asyncio.Event()))

    task = asyncio.create_task(run())
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert fallback.calls == 0
