#!/usr/bin/env python3
"""Waveshare 8-ch Ethernet Relay — high-fidelity software simulator.

Runs two servers concurrently on ONE asyncio event loop:

* Modbus TCP slave (pymodbus)  -> default port 5020
* Read-only web UI (aiohttp)   -> default port 8080 (HTTP + WebSocket)

Usage::

    python main.py                       # defaults
    python main.py --modbus-port 502     # real Modbus port (needs root on Unix)
    python main.py --ip 192.168.1.200 --mac 02:00:00:12:34:56 -v

Stop with Ctrl+C: both servers and every pending flash timer are shut down
cleanly.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys

from waveshare_sim import __version__
from waveshare_sim.config import DeviceInfo
from waveshare_sim.modbus_server import create_modbus_server
from waveshare_sim.relay_board import RelayBoard
from waveshare_sim.web_server import WebServer

log = logging.getLogger("waveshare_sim")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _env(name: str, default):
    """Default value taken from ``WAVESHARE_<NAME>`` if set (Docker-friendly).

    Precedence: command-line flag > environment variable > built-in default.
    """
    raw = os.environ.get(f"WAVESHARE_{name}")
    if raw is None or raw == "":
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    return raw


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Waveshare 8-ch Ethernet Relay simulator",
        epilog="Every option can also be set with a WAVESHARE_<OPTION> environment "
        "variable, e.g. WAVESHARE_MODBUS_PORT=502, WAVESHARE_IP=192.168.1.200.",
    )
    p.add_argument("--host", default=_env("HOST", "0.0.0.0"), help="bind address for both servers (default: 0.0.0.0)")
    p.add_argument("--modbus-port", type=int, default=_env("MODBUS_PORT", 5020), help="Modbus TCP port (default: 5020)")
    p.add_argument("--web-port", type=int, default=_env("WEB_PORT", 8080), help="web UI port (default: 8080)")
    p.add_argument("--ip", default=_env("IP", None), help="IP reported in input registers (default: auto-detect)")
    p.add_argument("--advertised-port", type=int, default=_env("ADVERTISED_PORT", 0),
                   help="Modbus port reported in input registers, if different from "
                   "--modbus-port (e.g. Docker mapping 502:5020)")
    p.add_argument("--mac", default=_env("MAC", DeviceInfo.mac), help=f"MAC reported (default: {DeviceInfo.mac})")
    p.add_argument("--netmask", default=_env("NETMASK", "255.255.255.0"), help="netmask reported")
    p.add_argument("--gateway", default=_env("GATEWAY", ""), help="gateway reported (default: first host of the subnet)")
    p.add_argument("--device-address", type=int, default=_env("DEVICE_ADDRESS", 1), help="Modbus slave address (default: 1)")
    p.add_argument(
        "--strict-unit-id",
        action="store_true",
        default=_env("STRICT_UNIT_ID", False),
        help="only answer to unit id == device address (or 0); others time out",
    )
    p.add_argument("-v", "--verbose", action="store_true", default=_env("VERBOSE", False),
                   help="debug logging (incl. pymodbus)")
    return p.parse_args(argv)


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Third-party libraries are noisy at INFO; keep them quiet unless -v.
    for noisy in ("pymodbus", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.DEBUG if verbose else logging.WARNING)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
async def run(args: argparse.Namespace) -> None:
    """Start both servers, wait for a stop signal, then shut down in order."""
    info_kwargs = dict(
        mac=args.mac,
        netmask=args.netmask,
        gateway=args.gateway,
        modbus_port=args.advertised_port or args.modbus_port,
        device_address=args.device_address,
    )
    if args.ip:
        info_kwargs["ip"] = args.ip
    board = RelayBoard(DeviceInfo(**info_kwargs))

    # --- Stop signal -------------------------------------------------------
    # On Unix we turn SIGINT/SIGTERM into an asyncio.Event, which lets the
    # code below run a deterministic, ordered shutdown. On Windows
    # add_signal_handler is unavailable: Ctrl+C then raises KeyboardInterrupt,
    # asyncio.run() cancels this coroutine and the `finally` block still runs.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)

    modbus = create_modbus_server(board, args.host, args.modbus_port, args.strict_unit_id)
    web = WebServer(board, args.host, args.web_port)
    web_started = False
    try:
        # background=True: bind the socket and return instead of blocking.
        await modbus.serve_forever(background=True)
        await web.start()
        web_started = True

        shown_host = "localhost" if args.host in ("0.0.0.0", "::") else args.host
        log.info("=" * 64)
        log.info("Waveshare 8-ch ETH Relay simulator v%s", __version__)
        log.info("  Modbus TCP : %s:%d  (slave address %d)", args.host, args.modbus_port, args.device_address)
        log.info("  Web UI     : http://%s:%d/", shown_host, args.web_port)
        log.info("  Identity   : MAC %s  IP %s  FW %s", board.info.mac, board.info.ip,
                 DeviceInfo.version_str(board.info.firmware_version))
        log.info("Press Ctrl+C to stop.")
        log.info("=" * 64)
        board.log_event("Simulator started", "system")

        await stop.wait()
        log.info("Stop signal received, shutting down...")
    finally:
        # Reverse start order; every step is isolated so that one failure
        # never prevents the others from releasing their resources.
        steps = [("Modbus server", modbus.shutdown), ("flash timers", board.shutdown)]
        if web_started:
            steps.append(("web server", web.stop))
        for name, step in steps:
            try:
                await asyncio.wait_for(step(), timeout=5)
                log.info("Stopped %s", name)
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                log.warning("Error while stopping %s: %s", name, exc)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        # Windows path (or Ctrl+C during startup): cleanup already ran.
        pass
    except (OSError, RuntimeError, ValueError) as exc:
        # Typical causes: port already in use, permission denied (<1024),
        # malformed --ip/--mac.
        log.error("Fatal: %s", exc)
        return 1
    log.info("Bye.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
