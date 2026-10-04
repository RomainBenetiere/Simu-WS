"""Hardware model of the relay board (the "single source of truth").

The model is completely protocol-agnostic: it knows nothing about Modbus
frames nor about HTTP. The Modbus front-end *drives* it, the web UI only
*observes* it through a tiny publish/subscribe mechanism.

Concurrency model
-----------------
Everything runs on a single asyncio event loop, so no locks are required:
each public method executes atomically between two ``await`` points.
Flash timers are plain asyncio tasks (one per channel at most).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime

from .config import FLASH_UNIT_S, RELAY_COUNT, DeviceInfo

log = logging.getLogger(__name__)


@dataclass
class FlashTimer:
    """A pending Flash ON / Flash OFF operation on one channel."""

    #: State applied immediately (True for Flash ON, False for Flash OFF).
    active_state: bool
    #: Total duration in seconds.
    duration: float
    #: ``loop.time()`` at which the relay will revert.
    deadline: float
    #: Background task that performs the revert.
    task: asyncio.Task

    def remaining(self, now: float) -> float:
        return max(0.0, self.deadline - now)


class RelayBoard:
    """Simulated Waveshare relay board.

    Channels are **0-based** internally (0..7) and displayed as CH1..CH8.
    """

    #: Max number of pending snapshots per web client before we drop the oldest.
    _QUEUE_SIZE = 32

    def __init__(
        self,
        info: DeviceInfo,
        relay_count: int = RELAY_COUNT,
        event_log_size: int = 40,
    ) -> None:
        self.info = info
        self.relay_count = relay_count
        self._states: list[bool] = [False] * relay_count
        self._timers: dict[int, FlashTimer] = {}
        self._subscribers: set[asyncio.Queue] = set()
        self._events: deque[dict] = deque(maxlen=event_log_size)
        self._started_monotonic = time.monotonic()
        self.stats = {"modbus_requests": 0, "modbus_errors": 0, "relay_switches": 0}

    # ------------------------------------------------------------------ #
    # Read accessors
    # ------------------------------------------------------------------ #
    @property
    def states(self) -> list[bool]:
        """Copy of the relay states (index 0 = CH1)."""
        return list(self._states)

    def read(self, start: int, count: int) -> list[bool]:
        """Return ``count`` relay states starting at channel ``start``."""
        self._check_range(start, count)
        return self._states[start : start + count]

    def bitmap(self) -> int:
        """Relay states packed in an int (bit n = CHn+1)."""
        return sum(1 << i for i, on in enumerate(self._states) if on)

    @property
    def uptime_s(self) -> int:
        return int(time.monotonic() - self._started_monotonic)

    # ------------------------------------------------------------------ #
    # Write operations (called by the Modbus front-end)
    # ------------------------------------------------------------------ #
    def set_relay(self, channel: int, on: bool, source: str = "") -> None:
        """Force one relay. Cancels any pending flash on that channel."""
        self._check_range(channel, 1)
        self._cancel_timer(channel)
        self._apply(channel, on)
        self.log_event(f"CH{channel + 1} {'ON' if on else 'OFF'}", source)
        self._notify()

    def toggle_relay(self, channel: int, source: str = "") -> None:
        """Flip one relay (Waveshare value 0x5500)."""
        self._check_range(channel, 1)
        self._cancel_timer(channel)
        self._apply(channel, not self._states[channel])
        self.log_event(
            f"CH{channel + 1} TOGGLE -> {'ON' if self._states[channel] else 'OFF'}", source
        )
        self._notify()

    def set_all(self, on: bool, source: str = "") -> None:
        """All relays ON/OFF (Waveshare address 0x00FF)."""
        for ch in range(self.relay_count):
            self._cancel_timer(ch)
            self._apply(ch, on)
        self.log_event(f"ALL {'ON' if on else 'OFF'}", source)
        self._notify()

    def toggle_all(self, source: str = "") -> None:
        """Flip every relay (address 0x00FF, value 0x5500)."""
        for ch in range(self.relay_count):
            self._cancel_timer(ch)
            self._apply(ch, not self._states[ch])
        self.log_event("ALL TOGGLE", source)
        self._notify()

    def write_many(self, start: int, values: list[bool], source: str = "") -> None:
        """Write consecutive relays in one shot (FC 0x0F)."""
        self._check_range(start, len(values))
        for offset, on in enumerate(values):
            self._cancel_timer(start + offset)
            self._apply(start + offset, bool(on))
        bits = "".join("1" if v else "0" for v in values)
        self.log_event(f"CH{start + 1}..CH{start + len(values)} <= {bits}", source)
        self._notify()

    def flash(self, channel: int, active_state: bool, units: int, source: str = "") -> None:
        """Start a Flash ON (``active_state=True``) or Flash OFF operation.

        The relay is switched to ``active_state`` immediately, then an asyncio
        timer reverts it after ``units * 100 ms``. A new command on the same
        channel (flash or plain write) cancels the previous timer — this is
        how the real firmware behaves: the latest command always wins.
        """
        self._check_range(channel, 1)
        if units <= 0:
            raise ValueError("flash delay must be > 0")
        self._cancel_timer(channel)
        self._apply(channel, active_state)

        loop = asyncio.get_running_loop()
        duration = units * FLASH_UNIT_S
        task = loop.create_task(
            self._flash_revert(channel, duration), name=f"flash-ch{channel + 1}"
        )
        self._timers[channel] = FlashTimer(active_state, duration, loop.time() + duration, task)

        kind = "FLASH ON" if active_state else "FLASH OFF"
        self.log_event(f"CH{channel + 1} {kind} {duration:.1f}s", source)
        self._notify()

    def set_device_address(self, address: int, source: str = "") -> None:
        """Change the Modbus device address (HR 0x4000)."""
        if not 1 <= address <= 247:
            raise ValueError("device address must be in 1..247")
        self.info.device_address = address
        self.log_event(f"Device address set to {address}", source)
        self._notify()

    # ------------------------------------------------------------------ #
    # Observability helpers
    # ------------------------------------------------------------------ #
    def log_event(self, message: str, source: str = "") -> None:
        """Append a line to the in-memory activity journal (shown in the UI)."""
        entry = {
            "time": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "source": source,
            "message": message,
        }
        self._events.append(entry)
        log.info("[%s] %s", source or "-", message)

    def count_request(self, error: bool = False) -> None:
        self.stats["modbus_requests"] += 1
        if error:
            self.stats["modbus_errors"] += 1

    def snapshot(self) -> dict:
        """Full JSON-serialisable state, pushed to every web client."""
        now = asyncio.get_running_loop().time()
        relays = []
        for ch, on in enumerate(self._states):
            timer = self._timers.get(ch)
            relays.append(
                {
                    "channel": ch + 1,
                    "on": on,
                    "flash": None
                    if timer is None
                    else {
                        "mode": "on" if timer.active_state else "off",
                        "duration_ms": round(timer.duration * 1000),
                        "remaining_ms": round(timer.remaining(now) * 1000),
                    },
                }
            )
        return {
            "type": "state",
            "relays": relays,
            "device": self.info.as_dict(),
            "uptime_s": self.uptime_s,
            "stats": dict(self.stats),
            "events": list(self._events),
        }

    # ------------------------------------------------------------------ #
    # Publish / subscribe (used by the web layer)
    # ------------------------------------------------------------------ #
    def subscribe(self) -> asyncio.Queue:
        """Register an observer; it receives a snapshot after every change."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._QUEUE_SIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def _notify(self) -> None:
        """Push the new snapshot to every observer without ever blocking.

        A slow browser must never slow down the Modbus side: if its queue is
        full we drop the *oldest* snapshot (only the latest state matters).
        """
        if not self._subscribers:
            return
        snap = self.snapshot()
        for queue in self._subscribers:
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(snap)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def shutdown(self) -> None:
        """Cancel every pending flash timer (called on Ctrl+C)."""
        tasks = [t.task for t in self._timers.values()]
        self._timers.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    async def _flash_revert(self, channel: int, delay: float) -> None:
        """Body of a flash timer: sleep, then restore the opposite state."""
        await asyncio.sleep(delay)  # CancelledError simply ends the task
        timer = self._timers.get(channel)
        # Guard: only revert if *this* task is still the registered timer.
        if timer is None or timer.task is not asyncio.current_task():
            return
        del self._timers[channel]
        self._apply(channel, not timer.active_state)
        self.log_event(
            f"CH{channel + 1} flash elapsed -> {'ON' if self._states[channel] else 'OFF'}",
            "timer",
        )
        self._notify()

    def _cancel_timer(self, channel: int) -> None:
        timer = self._timers.pop(channel, None)
        if timer is not None:
            timer.task.cancel()

    def _apply(self, channel: int, on: bool) -> None:
        if self._states[channel] != on:
            self._states[channel] = on
            self.stats["relay_switches"] += 1

    def _check_range(self, start: int, count: int) -> None:
        if count < 1 or start < 0 or start + count > self.relay_count:
            raise IndexError(
                f"relay range {start}..{start + count - 1} outside 0..{self.relay_count - 1}"
            )
