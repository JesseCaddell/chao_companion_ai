import asyncio

from chao.brain.backend import Message
from chao.brain.cloud import AnthropicBackend


class FakeRawStream:
    """Fakes the shape of anthropic's `client.messages.stream(...)` context
    manager: an async CM whose `__aenter__` yields an object exposing
    `.text_stream`.
    """

    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    async def _gen(self):
        for c in self.chunks:
            yield c

    @property
    def text_stream(self):
        return self._gen()


class FakeMessages:
    def __init__(self, raw_stream):
        self.raw_stream = raw_stream
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return self.raw_stream


class FakeAnthropicClient:
    def __init__(self, raw_stream):
        self.messages = FakeMessages(raw_stream)


async def test_streams_text_and_maps_call_args():
    raw = FakeRawStream(["Hello", " world"])
    client = FakeAnthropicClient(raw)
    backend = AnthropicBackend(model="test-model", client=client)

    result = [
        chunk
        async for chunk in backend.stream(
            "sys prompt", [Message(role="user", content="hi")], asyncio.Event()
        )
    ]

    assert result == ["Hello", " world"]
    assert raw.closed
    call = client.messages.calls[0]
    assert call["model"] == "test-model"
    assert call["system"] == "sys prompt"
    assert call["messages"] == [{"role": "user", "content": "hi"}]


async def test_stops_early_when_cancelled():
    raw = FakeRawStream(["a", "b", "c"])
    client = FakeAnthropicClient(raw)
    backend = AnthropicBackend(client=client)
    cancel = asyncio.Event()

    result = []
    async for chunk in backend.stream("sys", [], cancel):
        result.append(chunk)
        if chunk == "a":
            cancel.set()

    assert result == ["a"]
    assert raw.closed
