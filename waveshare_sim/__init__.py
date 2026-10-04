"""High-fidelity software simulator of the Waveshare 8-ch Ethernet (Modbus TCP) relay.

Package layout
--------------
- :mod:`waveshare_sim.config`        Register map constants + :class:`DeviceInfo`.
- :mod:`waveshare_sim.relay_board`   Hardware model (relay states, flash timers, pub/sub).
- :mod:`waveshare_sim.modbus_server` Modbus TCP front-end (custom pymodbus PDUs).
- :mod:`waveshare_sim.web_server`    Read-only real-time web UI (aiohttp + WebSocket).
"""

__version__ = "1.0.0"
