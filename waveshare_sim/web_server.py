"""Read-only real-time web UI (aiohttp: HTTP + WebSocket on the same port).

Endpoints
---------
``GET /``           The front-end (``static/index.html``, self-contained).
``GET /api/state``  JSON snapshot of the board (handy for curl / debugging).
``GET /ws``         WebSocket: a full snapshot is pushed on connect and after
                    every relay change. Messages *from* the browser are
                    ignored: the UI is strictly read-only, control happens
                    exclusively over Modbus, like on real hardware.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import weakref
from pathlib import Path

from aiohttp import WSCloseCode, WSMsgType, web

from .relay_board import RelayBoard

log = logging.getLogger(__name__)

#: Resolved relative to this file => works whatever the current directory is.
STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_FILE = STATIC_DIR / "index.html"

#: When nothing happens, re-send a snapshot periodically (resync uptime/stats
#: and keep proxies from closing an idle connection).
RESYNC_INTERVAL_S = 5.0

BOARD_KEY = web.AppKey("board", RelayBoard)
SOCKETS_KEY = web.AppKey("sockets", weakref.WeakSet)


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
async def index(request: web.Request) -> web.StreamResponse:
    """Serve the single-page UI. ``no-cache`` so edits show up on reload."""
    return web.FileResponse(INDEX_FILE, headers={"Cache-Control": "no-cache"})


async def api_state(request: web.Request) -> web.Response:
    return web.json_response(request.app[BOARD_KEY].snapshot())


async def websocket(request: web.Request) -> web.WebSocketResponse:
    """Push board snapshots to one browser until it disconnects."""
    board = request.app[BOARD_KEY]
    ws = web.WebSocketResponse(heartbeat=20.0)
    await ws.prepare(request)
    request.app[SOCKETS_KEY].add(ws)
    queue = board.subscribe()
    peer = request.remote
    log.info("WebSocket client connected: %s", peer)

    async def drain_incoming() -> None:
        """Consume (and discard) client frames so close/ping are processed."""
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                log.debug("Ignoring client message (UI is read-only): %.80s", msg.data)
            elif msg.type == WSMsgType.ERROR:
                log.warning("WebSocket error: %s", ws.exception())

    reader = asyncio.create_task(drain_incoming())
    try:
        await ws.send_json(board.snapshot())
        while not ws.closed and not reader.done():
            try:
                snap = await asyncio.wait_for(queue.get(), timeout=RESYNC_INTERVAL_S)
            except asyncio.TimeoutError:
                snap = board.snapshot()
            await ws.send_json(snap)
    except (ConnectionResetError, RuntimeError):
        pass  # browser vanished mid-send
    finally:
        board.unsubscribe(queue)
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader
        await ws.close()
        log.info("WebSocket client disconnected: %s", peer)
    return ws


async def _close_websockets(app: web.Application) -> None:
    """on_shutdown hook: tell every browser we are going away."""
    for ws in set(app[SOCKETS_KEY]):
        await ws.close(code=WSCloseCode.GOING_AWAY, message=b"Simulator shutdown")


# --------------------------------------------------------------------------- #
# Server wrapper
# --------------------------------------------------------------------------- #
def create_app(board: RelayBoard) -> web.Application:
    app = web.Application()
    app[BOARD_KEY] = board
    app[SOCKETS_KEY] = weakref.WeakSet()
    app.router.add_get("/", index)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/ws", websocket)
    app.on_shutdown.append(_close_websockets)
    return app


class WebServer:
    """Small start/stop wrapper around aiohttp's low-level runner API.

    ``web.run_app`` would own the event loop and install its own signal
    handlers; ``AppRunner`` + ``TCPSite`` lets us share the loop with pymodbus
    and orchestrate the shutdown ourselves.
    """

    def __init__(self, board: RelayBoard, host: str = "0.0.0.0", port: int = 8080) -> None:
        self.host, self.port = host, port
        self._runner = web.AppRunner(create_app(board), access_log=None)

    async def start(self) -> None:
        if not INDEX_FILE.is_file():
            raise FileNotFoundError(f"UI file missing: {INDEX_FILE}")
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()

    async def stop(self) -> None:
        # cleanup() runs on_shutdown (closes websockets) then stops the sites.
        await self._runner.cleanup()
