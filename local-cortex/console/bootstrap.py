"""Packaged-window launcher for the Kaidera OS Console (R6).

This is the NATIVE DESKTOP entry point: it starts the FastAPI app on a uvicorn
daemon thread (on a dynamic free loopback port) and opens a native pywebview
(WKWebView) window pointed at it. PyInstaller freezes THIS module into
`dist/Kaidera OS Console.app` (see console.spec).

NOT used for dev — dev runs `uvicorn app.main:app` directly (see BUILD.md).
`app/main.py` stays shell-agnostic (no pywebview import there) so the exact
same ASGI app runs under plain uvicorn AND inside this packaged window — no
code branches between dev and packaged.

Risk mitigations baked in (see research/2026-06-01-desktop-packaging.md):
  * `multiprocessing.freeze_support()` is the FIRST line of main() — without it
    a frozen app that touches multiprocessing endlessly relaunches itself.
  * uvicorn runs on a daemon background thread; pywebview owns the main (Cocoa)
    thread — all WebKit ops must be main-thread, so uvicorn cannot be there.
  * single-process server (workers=1, in-thread) — no `sys.executable`
    subprocess, no `--workers` (both would re-trigger the spawn loop frozen).
  * the window's `closing` event flips `server.should_exit = True` so uvicorn
    (and its SSE generators) shut down cleanly when the window is closed.
"""

from __future__ import annotations

import multiprocessing
import socket
import threading

import uvicorn
import webview

from app.main import app  # a normal, shell-agnostic FastAPI instance

WINDOW_TITLE = "Kaidera OS Console"
HOST = "127.0.0.1"


def _free_port() -> int:
    """Ask the OS for a free loopback port (bind to port 0, read it back).

    A dynamic port avoids colliding with the Cortex API (8501), the dev console
    port (8765), or anything else already bound — the window is told the exact
    port we got, so there is no fixed-port assumption."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def main() -> None:
    # MUST be first: a frozen (PyInstaller) app that touches multiprocessing
    # without this endlessly relaunches itself (see research, risk #1).
    multiprocessing.freeze_support()

    port = _free_port()

    # In-process uvicorn Server (NOT uvicorn.run) so we hold the instance and can
    # flip should_exit on window close. Pass the app OBJECT (not a "app.main:app"
    # import string) so uvicorn never re-imports by path inside the frozen bundle.
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=HOST,
            port=port,
            log_level="warning",
            workers=1,  # single-process: no spawn, SSE stays on one loop
        )
    )
    threading.Thread(target=server.run, daemon=True).start()  # uvicorn off-main

    window = webview.create_window(
        WINDOW_TITLE,
        f"http://{HOST}:{port}/app/",
        width=1280,
        height=820,
        min_size=(960, 600),
    )
    # Clean quit: closing the window stops uvicorn (and its SSE generators) so the
    # process exits instead of lingering on a still-running server thread.
    window.events.closing += lambda: setattr(server, "should_exit", True)

    webview.start()  # blocks in NSApplication.run() on the main thread


if __name__ == "__main__":
    main()
