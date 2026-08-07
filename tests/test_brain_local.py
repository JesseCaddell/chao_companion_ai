import asyncio
import json

from chao.brain.backend import Message
from chao.brain.local import OllamaBackend


class FakeResponse:
    """Fakes an httpx streaming response: the object returned by
    `client.stream(...)` is itself the async context manager.
    """

    def __init__(self, lines):
        self.lines = lines
        self.closed = False

    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        for line in self.lines:
            yield line

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True


class FakeHTTPClient:
    def __init__(self, response):
        self.response = response
        self.calls: list[tuple] = []

    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


def ndjson_line(content: str, *, done: bool = False) -> str:
    return json.dumps({"message": {"content": content}, "done": done})


async def test_streams_content_and_maps_call_args():
    lines = [ndjson_line("Hel"), ndjson_line("lo"), ndjson_line("", done=True)]
    response = FakeResponse(lines)
    client = FakeHTTPClient(response)
    backend = OllamaBackend("test-model", client=client)

    result = [
        chunk
        async for chunk in backend.stream(
            "sys", [Message(role="user", content="hi")], asyncio.Event()
        )
    ]

    assert result == ["Hel", "lo"]
    assert response.closed
    method, url, kwargs = client.calls[0]
    assert method == "POST"
    assert url.endswith("/api/chat")
    assert kwargs["json"]["model"] == "test-model"
    assert kwargs["json"]["messages"][0] == {"role": "system", "content": "sys"}
    assert kwargs["json"]["messages"][1] == {"role": "user", "content": "hi"}


async def test_stops_early_when_cancelled():
    lines = [ndjson_line("a"), ndjson_line("b")]
    response = FakeResponse(lines)
    client = FakeHTTPClient(response)
    backend = OllamaBackend("test-model", client=client)
    cancel = asyncio.Event()

    result = []
    async for chunk in backend.stream("sys", [], cancel):
        result.append(chunk)
        if chunk == "a":
            cancel.set()

    assert result == ["a"]
    assert response.closed


async def test_skips_blank_lines():
    lines = ["", ndjson_line("only"), "", ndjson_line("", done=True)]
    response = FakeResponse(lines)
    client = FakeHTTPClient(response)
    backend = OllamaBackend("test-model", client=client)

    result = [chunk async for chunk in backend.stream("sys", [], asyncio.Event())]

    assert result == ["only"]
