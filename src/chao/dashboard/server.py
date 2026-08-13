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

Also serves the built frontend (`web/dist/`, via `npm run build`) as static
files, so `chao.dashboard.window`'s native window (design doc §11.4) has a
single self-contained URL to point at — no separate `npm run dev` process
needed for normal/streaming use, only for frontend iteration.

`/subtitles` is a second, unrelated static page also served from here
(design doc §17's phase-2 subtitle overlay row) — an OBS Browser Source
URL, stream-facing rather than streamer-only. It's a separate concern from
the dashboard proper, just colocated because both are static pages riding
the same already-running uvicorn server and the same `/ws/events` stream.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from chao.bus import Bus

WEB_DIST = Path(__file__).parent / "web" / "dist"
SUBTITLES_HTML = Path(__file__).parent / "subtitles.html"

DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 8765


def create_app(bus: Bus) -> FastAPI:
    app = FastAPI()
    # CORS matters only for the `npm run dev` workflow (Vite's dev server
    # runs on a different port); the built frontend served below is
    # same-origin. Wide open is fine either way — this never leaves
    # localhost.
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

    @app.get("/subtitles")
    async def subtitles() -> HTMLResponse:
        # Stream-facing overlay (design doc §17's phase-2 row) — an OBS
        # Browser Source URL, separate from the streamer-only dashboard
        # mounted below. Plain static HTML/JS, no build step, so there's
        # nothing to run alongside `chao` for it to work; reads the file
        # fresh each request rather than caching it in memory, since it's
        # tiny and this way editing it during setup doesn't need a restart.
        return HTMLResponse(SUBTITLES_HTML.read_text())

    # Mounted last and at "/" so it doesn't shadow the websocket/subtitles
    # routes above; Starlette matches explicit routes before catch-all
    # mounts regardless of declaration order, but keeping them first
    # documents the intent. Skipped gracefully if `npm run build` hasn't
    # been run yet — dev workflow (`npm run dev` on its own port) doesn't
    # need this at all.
    if WEB_DIST.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="frontend")

    return app
