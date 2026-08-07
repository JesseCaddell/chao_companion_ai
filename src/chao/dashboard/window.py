"""Native window shell for the dashboard. `uv run python -m chao.dashboard.window`.

Opens the dashboard (served by `chao.__main__`'s FastAPI app — run that
first, separately) in a native OS window via `pywebview`, using the
system's built-in webview runtime (WebView2 on Windows) instead of a full
Chrome tab. See design doc §11.4: this exists to avoid the baseline
memory/process overhead of a whole separate browser instance for what's
fundamentally a single page, which matters here because CLAUDE.md
invariant 1 (zero VRAM) makes background overhead worth avoiding in
general on the machine the user games on.

Deliberately a separate process from `chao.__main__`, not a thread inside
it: `webview.start()` blocks and must own the OS main thread for its GUI
event loop, which conflicts with `__main__.py`'s asyncio loop already
owning that thread in the brain process. This module is a pure client of
the dashboard server's HTTP/websocket endpoint — same relationship a
browser tab would have to it.
"""

from __future__ import annotations

import webview

from chao.dashboard.server import DASHBOARD_HOST, DASHBOARD_PORT


def main() -> None:
    webview.create_window(
        "chao dashboard", f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}/", width=1000, height=750
    )
    webview.start()


if __name__ == "__main__":
    main()
