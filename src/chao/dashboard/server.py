"""FastAPI dashboard backend. See design doc §11.

A pure subscriber over the bus's event stream (§11.1) — it never publishes,
only relays. Phase 1 ships only the event feed panel (§11.2 item 3), the
direct replacement for `__main__.py`'s `_print_events`; the other eight
panels (prompt inspector, mood plot, latency waterfall, ...) render data
that doesn't exist yet (retrieval, mood.py, parameter injection) and would
be UI over nothing.

`create_app` takes the `Bus` as a dependency so it's testable without a
real process; `main.py`'s wiring runs it in-process via `uvicorn.Server`
(not `uvicorn.run`, which would call `asyncio.run` and fight the existing
event loop) since `bus.subscribe()` is an in-process `asyncio.Queue`, not a
network call.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from chao.bus import Bus


def create_app(bus: Bus) -> FastAPI:
    app = FastAPI()
    # The frontend runs under Vite's own dev server on a different port
    # (`npm run dev`, per CLAUDE.md), so the websocket is cross-origin in
    # dev. Wide open is fine — this never leaves localhost.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.websocket("/ws/events")
    async def events_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        sub = bus.subscribe()
        try:
            while True:
                event = await sub.get()
                await websocket.send_json(asdict(event))
        except WebSocketDisconnect:
            pass
        finally:
            bus.unsubscribe(sub)

    return app
