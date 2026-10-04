"""Static configuration: Waveshare register map and mocked device identity.

Everything that is "protocol knowledge" lives here so that the hardware model
(:mod:`relay_board`) and the Modbus front-end (:mod:`modbus_server`) never
hard-code magic numbers.

Register map (Waveshare "Modbus RTU/POE ETH Relay" documentation)
-----------------------------------------------------------------
=====  ==================  ================  =====================================
FC     Address             Value             Meaning
=====  ==================  ================  =====================================
0x01   0x0000..0x0007      -                 Read relay status (CH1..CH8)
0x05   0x0000..0x0007      0xFF00            Relay ON
0x05   0x0000..0x0007      0x0000            Relay OFF
0x05   0x0000..0x0007      0x5500            Relay TOGGLE (flip)
0x05   0x00FF              FF00/0000/5500    All relays ON / OFF / TOGGLE
0x05   0x0200..0x0207      delay (x100 ms)   Flash ON  : ON now, OFF after delay
0x05   0x0400..0x0407      delay (x100 ms)   Flash OFF : OFF now, ON after delay
0x0F   0x0000..0x0007      bitfield          Write several relays at once
0x03   0x4000              -                 Read device (slave) address
0x06   0x4000              1..247            Set device (slave) address
0x03   0x8000              -                 Read firmware version (0x00C8 = V2.00)
=====  ==================  ================  =====================================

Simulator extensions (NOT on the real hardware, clearly opt-in)
---------------------------------------------------------------
* FC 0x06 on 0x0200+ch / 0x0400+ch: same Flash ON / Flash OFF semantics as
  FC 0x05. Useful for SCADA/HA tools that can only write registers.
* FC 0x04 "system info" block (MAC, IP, mask, gateway, port, firmware...),
  mocked so that network supervisors have plausible values to poll.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Hardware characteristics
# --------------------------------------------------------------------------- #
RELAY_COUNT = 8

#: Flash delay unit used by the Waveshare firmware (value 0x0007 => 700 ms).
FLASH_UNIT_S = 0.1
#: Highest delay value accepted by the firmware (0x7FFF * 100 ms ~= 54 min).
FLASH_MAX_UNITS = 0x7FFF

# --------------------------------------------------------------------------- #
# Coil space (FC 0x01 / 0x05 / 0x0F)
# --------------------------------------------------------------------------- #
COIL_BASE = 0x0000          # CH1 .. CH8 => 0x0000 .. 0x0007
COIL_ALL = 0x00FF           # "all relays" pseudo-address (FC 0x05 only)
FLASH_ON_BASE = 0x0200      # Flash ON  CH1..CH8 => 0x0200 .. 0x0207
FLASH_OFF_BASE = 0x0400     # Flash OFF CH1..CH8 => 0x0400 .. 0x0407

# FC 0x05 command values
VALUE_ON = 0xFF00
VALUE_OFF = 0x0000
VALUE_TOGGLE = 0x5500

# --------------------------------------------------------------------------- #
# Holding registers (FC 0x03 / 0x06)
# --------------------------------------------------------------------------- #
HR_DEVICE_ADDRESS = 0x4000
HR_FIRMWARE_VERSION = 0x8000

# --------------------------------------------------------------------------- #
# Input registers (FC 0x04) — mocked system information block
# --------------------------------------------------------------------------- #
IR_MAC = 0x0000             # 3 registers, 6 bytes, big-endian
IR_IP = 0x0003              # 2 registers, 4 bytes (192.168.1.200 => C0A8, 01C8)
IR_NETMASK = 0x0005         # 2 registers
IR_GATEWAY = 0x0007         # 2 registers
IR_MODBUS_PORT = 0x0009     # 1 register
IR_FIRMWARE = 0x000A        # 1 register (same encoding as HR 0x8000)
IR_HARDWARE = 0x000B        # 1 register (0x0100 => HW V1.00)
IR_RELAY_COUNT = 0x000C     # 1 register (8)
IR_DEVICE_ADDRESS = 0x000D  # 1 register (mirror of HR 0x4000)
IR_UPTIME = 0x000E          # 2 registers, seconds since start, uint32 big-endian
IR_RELAY_BITMAP = 0x0010    # 1 register, bit n = relay n+1 (handy for HA sensors)
IR_BLOCK_SIZE = 0x0011      # number of registers in the block (0x0000..0x0010)


def _detect_lan_ip(fallback: str = "192.168.1.200") -> str:
    """Return the host's primary LAN IPv4 address.

    The UDP ``connect`` trick sends no packet; it only asks the kernel which
    interface would be used. When offline we fall back to the Waveshare
    factory default IP (192.168.1.200).
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
    except OSError:
        return fallback


@dataclass
class DeviceInfo:
    """Identity of the simulated module (what supervisors will "see")."""

    #: Locally-administered MAC (bit 1 of the first byte set) => never clashes
    #: with a real vendor OUI on the LAN.
    mac: str = "02:57:53:52:4C:08"
    ip: str = field(default_factory=_detect_lan_ip)
    netmask: str = "255.255.255.0"
    gateway: str = ""                      # derived from ip when left empty
    modbus_port: int = 5020
    #: Encoded like the real firmware: 0x00C8 = 200 => "V2.00".
    firmware_version: int = 0x00C8
    hardware_version: int = 0x0100
    #: Modbus device/slave address (Waveshare factory default: 1).
    device_address: int = 1
    model: str = "Modbus POE ETH Relay (8-ch)"

    def __post_init__(self) -> None:
        # Validate early so that a bad CLI argument fails at startup.
        ipaddress.IPv4Address(self.ip)
        ipaddress.IPv4Address(self.netmask)
        if not self.gateway:
            net = ipaddress.IPv4Network(f"{self.ip}/{self.netmask}", strict=False)
            self.gateway = str(net.network_address + 1)
        ipaddress.IPv4Address(self.gateway)
        if len(self.mac_bytes) != 6:
            raise ValueError(f"Invalid MAC address: {self.mac!r}")

    # ---- encoding helpers ------------------------------------------------- #
    @property
    def mac_bytes(self) -> bytes:
        return bytes(int(part, 16) for part in self.mac.replace("-", ":").split(":"))

    @staticmethod
    def ip_to_registers(ip: str) -> list[int]:
        """Pack a dotted IPv4 address into two big-endian 16-bit registers."""
        raw = ipaddress.IPv4Address(ip).packed
        return [(raw[0] << 8) | raw[1], (raw[2] << 8) | raw[3]]

    def mac_registers(self) -> list[int]:
        raw = self.mac_bytes
        return [(raw[i] << 8) | raw[i + 1] for i in range(0, 6, 2)]

    @staticmethod
    def version_str(code: int) -> str:
        """0x00C8 -> 'V2.00' (Waveshare convention: version * 100)."""
        return f"V{code // 100}.{code % 100:02d}"

    def as_dict(self) -> dict:
        """JSON-friendly representation for the web UI."""
        return {
            "model": self.model,
            "mac": self.mac.upper(),
            "ip": self.ip,
            "netmask": self.netmask,
            "gateway": self.gateway,
            "modbus_port": self.modbus_port,
            "device_address": self.device_address,
            "firmware": self.version_str(self.firmware_version),
            # Hardware version is BCD-like: 0x0100 => "V1.00".
            "hardware": f"V{self.hardware_version >> 8}.{self.hardware_version & 0xFF:02d}",
        }
