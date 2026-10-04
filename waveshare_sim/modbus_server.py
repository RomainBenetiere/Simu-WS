"""Modbus TCP front-end of the simulator, built on pymodbus (asyncio).

Why custom PDU classes instead of a pymodbus datastore?
-------------------------------------------------------
The Waveshare firmware abuses FC 0x05 ("Write Single Coil"): on addresses
0x0200+ch / 0x0400+ch the 16-bit *value* field carries a delay, and 0x5500
means "toggle". The stock pymodbus decoder converts that field to a boolean
(``bits = [bool(value)]``) *before* any datastore sees it, so the delay is
irremediably lost. Writing our own request classes is therefore the only
high-fidelity option. It brings extra benefits:

* byte-exact responses (FC 0x05 echoes the raw request, like the hardware);
* precise exception codes (ILLEGAL ADDRESS / VALUE / FUNCTION);
* the pymodbus datastore is bypassed entirely: :class:`RelayBoard` remains the
  single source of truth, no state duplication / synchronisation is needed.

pymodbus is still in charge of everything "boring": TCP listener, MBAP
framing, transaction ids, connection handling.
"""

from __future__ import annotations

import logging
import struct

from pymodbus.constants import ExcCodes
from pymodbus.exceptions import NoSuchIdException
from pymodbus.pdu import DecodePDU, ExceptionResponse, ModbusPDU
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

from . import config as cfg
from .relay_board import RelayBoard

log = logging.getLogger(__name__)


class ModbusError(Exception):
    """Raised by request handlers to produce a Modbus exception response."""

    def __init__(self, code: ExcCodes, reason: str = "") -> None:
        super().__init__(reason or code.name)
        self.code = code


# =========================================================================== #
# Generic response
# =========================================================================== #
class RawResponse(ModbusPDU):
    """Response whose PDU payload has already been encoded by the handler.

    pymodbus builds the frame as ``function_code byte + encode()``, then wraps
    it in the MBAP header, so all we need is to hand back the payload bytes.
    """

    def __init__(self, function_code: int, payload: bytes) -> None:
        super().__init__()
        self.function_code = function_code  # instance attribute shadows class attr
        self._payload = payload

    def encode(self) -> bytes:
        return self._payload

    def decode(self, data: bytes) -> None:  # pragma: no cover - server side only
        self._payload = data


# =========================================================================== #
# Base request
# =========================================================================== #
class WaveshareRequest(ModbusPDU):
    """Common plumbing for every simulated function code.

    Concrete subclasses implement :meth:`decode` (parse the request PDU) and
    :meth:`handle` (act on the board, return the response payload).

    The ``board`` and ``strict_unit_id`` class attributes are injected by
    :func:`build_request_classes`, which creates subclasses bound to one
    :class:`RelayBoard` instance (no global state).
    """

    board: RelayBoard
    strict_unit_id: bool = False

    def decode(self, data: bytes) -> None:
        self.raw = data

    def encode(self) -> bytes:  # pragma: no cover - only used by clients
        return getattr(self, "raw", b"")

    def handle(self) -> bytes:
        raise NotImplementedError

    @property
    def source(self) -> str:
        """Label used in the activity journal, e.g. ``FC05 uid=1``."""
        return f"FC{self.function_code:02X} uid={self.dev_id}"

    async def datastore_update(self, context, device_id: int) -> ModbusPDU:
        """Entry point called by pymodbus for every decoded request.

        ``context`` (the pymodbus datastore) is deliberately ignored.
        """
        _ = context
        board = self.board
        if self.strict_unit_id and device_id not in (0, board.info.device_address):
            # Like a real slave behind a gateway: wrong address => no answer.
            # pymodbus turns this exception into "silently ignore" because the
            # server is created with ignore_missing_devices=True.
            raise NoSuchIdException(f"unit id {device_id} not handled")
        try:
            payload = self.handle()
        except ModbusError as exc:
            board.count_request(error=True)
            board.log_event(f"EXCEPTION {exc.code.name} ({exc})", self.source)
            return ExceptionResponse(self.function_code, exc.code)
        board.count_request()
        return RawResponse(self.function_code, payload)

    # ---- helpers ---------------------------------------------------------- #
    def unpack(self, fmt: str) -> tuple:
        """Strictly unpack the request; malformed frame => ILLEGAL VALUE."""
        size = struct.calcsize(fmt)
        if len(self.raw) < size:
            raise ModbusError(ExcCodes.ILLEGAL_VALUE, "truncated request")
        return struct.unpack(fmt, self.raw[:size])

    @staticmethod
    def pack_bits(bits: list[bool]) -> bytes:
        """Modbus bit packing: LSB of first byte = first coil."""
        out = bytearray((len(bits) + 7) // 8)
        for i, bit in enumerate(bits):
            if bit:
                out[i // 8] |= 1 << (i % 8)
        return bytes(out)

    @staticmethod
    def unpack_bits(data: bytes, count: int) -> list[bool]:
        return [bool(data[i // 8] & (1 << (i % 8))) for i in range(count)]


# =========================================================================== #
# FC 0x01 — Read Coils (relay status)
# =========================================================================== #
class ReadRelaysRequest(WaveshareRequest):
    function_code = 0x01
    rtu_frame_size = 8

    def handle(self) -> bytes:
        address, count = self.unpack(">HH")
        if not 1 <= count <= 2000:
            raise ModbusError(ExcCodes.ILLEGAL_VALUE, f"count={count}")
        start = address - cfg.COIL_BASE
        try:
            bits = self.board.read(start, count)
        except IndexError as exc:
            raise ModbusError(ExcCodes.ILLEGAL_ADDRESS, str(exc)) from None
        data = self.pack_bits(bits)
        return struct.pack(">B", len(data)) + data


# =========================================================================== #
# FC 0x05 — Write Single Coil (+ Waveshare toggle / all / flash extensions)
# =========================================================================== #
class WriteRelayRequest(WaveshareRequest):
    function_code = 0x05
    rtu_frame_size = 8

    def handle(self) -> bytes:
        address, value = self.unpack(">HH")
        board, src = self.board, self.source

        if cfg.COIL_BASE <= address < cfg.COIL_BASE + board.relay_count:
            ch = address - cfg.COIL_BASE
            if value == cfg.VALUE_ON:
                board.set_relay(ch, True, src)
            elif value == cfg.VALUE_OFF:
                board.set_relay(ch, False, src)
            elif value == cfg.VALUE_TOGGLE:
                board.toggle_relay(ch, src)
            else:
                raise ModbusError(ExcCodes.ILLEGAL_VALUE, f"value=0x{value:04X}")

        elif address == cfg.COIL_ALL:
            if value == cfg.VALUE_ON:
                board.set_all(True, src)
            elif value == cfg.VALUE_OFF:
                board.set_all(False, src)
            elif value == cfg.VALUE_TOGGLE:
                board.toggle_all(src)
            else:
                raise ModbusError(ExcCodes.ILLEGAL_VALUE, f"value=0x{value:04X}")

        elif _is_flash_address(address, board.relay_count):
            _do_flash(board, address, value, src)

        else:
            raise ModbusError(ExcCodes.ILLEGAL_ADDRESS, f"address=0x{address:04X}")

        # The hardware echoes the request PDU verbatim.
        return struct.pack(">HH", address, value)


# =========================================================================== #
# FC 0x0F — Write Multiple Coils
# =========================================================================== #
class WriteRelaysRequest(WaveshareRequest):
    function_code = 0x0F
    rtu_byte_count_pos = 6

    def handle(self) -> bytes:
        address, count, byte_count = self.unpack(">HHB")
        data = self.raw[5:]
        if not 1 <= count <= 0x07B0 or byte_count != (count + 7) // 8 or len(data) < byte_count:
            raise ModbusError(ExcCodes.ILLEGAL_VALUE, f"count={count} bytes={byte_count}")
        values = self.unpack_bits(data, count)
        try:
            self.board.write_many(address - cfg.COIL_BASE, values, self.source)
        except IndexError as exc:
            raise ModbusError(ExcCodes.ILLEGAL_ADDRESS, str(exc)) from None
        return struct.pack(">HH", address, count)


# =========================================================================== #
# FC 0x03 — Read Holding Registers (device address, firmware version)
# =========================================================================== #
class ReadHoldingRequest(WaveshareRequest):
    function_code = 0x03
    rtu_frame_size = 8

    def _registers(self) -> dict[int, int]:
        info = self.board.info
        return {
            cfg.HR_DEVICE_ADDRESS: info.device_address,
            cfg.HR_FIRMWARE_VERSION: info.firmware_version,
        }

    def handle(self) -> bytes:
        address, count = self.unpack(">HH")
        if not 1 <= count <= 125:
            raise ModbusError(ExcCodes.ILLEGAL_VALUE, f"count={count}")
        regs = self._registers()
        try:
            values = [regs[a] for a in range(address, address + count)]
        except KeyError as exc:
            raise ModbusError(ExcCodes.ILLEGAL_ADDRESS, f"address=0x{exc.args[0]:04X}") from None
        return struct.pack(f">B{count}H", 2 * count, *values)


# =========================================================================== #
# FC 0x04 — Read Input Registers (mocked system information)
# =========================================================================== #
class ReadInputRequest(WaveshareRequest):
    function_code = 0x04
    rtu_frame_size = 8

    def _block(self) -> list[int]:
        """Build the system-info block (re-computed on each read: live uptime)."""
        board, info = self.board, self.board.info
        uptime = board.uptime_s & 0xFFFFFFFF
        block = (
            info.mac_registers()                          # 0x0000..0x0002
            + info.ip_to_registers(info.ip)               # 0x0003..0x0004
            + info.ip_to_registers(info.netmask)          # 0x0005..0x0006
            + info.ip_to_registers(info.gateway)          # 0x0007..0x0008
            + [info.modbus_port & 0xFFFF]                 # 0x0009
            + [info.firmware_version]                     # 0x000A
            + [info.hardware_version]                     # 0x000B
            + [board.relay_count]                         # 0x000C
            + [info.device_address]                       # 0x000D
            + [uptime >> 16, uptime & 0xFFFF]             # 0x000E..0x000F
            + [board.bitmap()]                            # 0x0010
        )
        assert len(block) == cfg.IR_BLOCK_SIZE
        return block

    def handle(self) -> bytes:
        address, count = self.unpack(">HH")
        if not 1 <= count <= 125:
            raise ModbusError(ExcCodes.ILLEGAL_VALUE, f"count={count}")
        if address + count > cfg.IR_BLOCK_SIZE:
            raise ModbusError(ExcCodes.ILLEGAL_ADDRESS, f"address=0x{address:04X}+{count}")
        values = self._block()[address : address + count]
        return struct.pack(f">B{count}H", 2 * count, *values)


# =========================================================================== #
# FC 0x06 — Write Single Register (device address + flash alias)
# =========================================================================== #
class WriteRegisterRequest(WaveshareRequest):
    function_code = 0x06
    rtu_frame_size = 8

    def handle(self) -> bytes:
        address, value = self.unpack(">HH")
        board = self.board
        if address == cfg.HR_DEVICE_ADDRESS:
            try:
                board.set_device_address(value, self.source)
            except ValueError as exc:
                raise ModbusError(ExcCodes.ILLEGAL_VALUE, str(exc)) from None
        elif _is_flash_address(address, board.relay_count):
            # Simulator extension: same semantics as FC 0x05 flash commands.
            _do_flash(board, address, value, self.source)
        else:
            raise ModbusError(ExcCodes.ILLEGAL_ADDRESS, f"address=0x{address:04X}")
        return struct.pack(">HH", address, value)


# =========================================================================== #
# Every other standard function code — not implemented by the hardware
# =========================================================================== #
class UnsupportedRequest(WaveshareRequest):
    """Answer ILLEGAL FUNCTION, as the real firmware does for unknown FCs."""

    def handle(self) -> bytes:
        raise ModbusError(ExcCodes.ILLEGAL_FUNCTION, "function not supported by device")


# =========================================================================== #
# Shared flash helpers
# =========================================================================== #
def _is_flash_address(address: int, relay_count: int) -> bool:
    return (
        cfg.FLASH_ON_BASE <= address < cfg.FLASH_ON_BASE + relay_count
        or cfg.FLASH_OFF_BASE <= address < cfg.FLASH_OFF_BASE + relay_count
    )


def _do_flash(board: RelayBoard, address: int, units: int, source: str) -> None:
    """Decode a flash address and start the timer on the board."""
    if not 1 <= units <= cfg.FLASH_MAX_UNITS:
        raise ModbusError(ExcCodes.ILLEGAL_VALUE, f"flash delay=0x{units:04X}")
    if address >= cfg.FLASH_OFF_BASE:
        board.flash(address - cfg.FLASH_OFF_BASE, False, units, source)
    else:
        board.flash(address - cfg.FLASH_ON_BASE, True, units, source)


# =========================================================================== #
# Factory
# =========================================================================== #
_IMPLEMENTED = (
    ReadRelaysRequest,
    WriteRelayRequest,
    WriteRelaysRequest,
    ReadHoldingRequest,
    ReadInputRequest,
    WriteRegisterRequest,
)


def build_request_classes(board: RelayBoard, strict_unit_id: bool = False) -> list[type]:
    """Create PDU classes bound to ``board`` (one subclass per function code).

    Binding via dynamic subclasses (instead of a module-level global) lets the
    test-suite run several independent simulators in the same process.
    """
    bound = {"board": board, "strict_unit_id": strict_unit_id}
    classes: list[type] = [type(cls.__name__, (cls,), dict(bound)) for cls in _IMPLEMENTED]

    # Override every other FC known to pymodbus so that it answers
    # ILLEGAL FUNCTION instead of hitting the (empty) pymodbus datastore.
    implemented = {cls.function_code for cls in _IMPLEMENTED}
    for fc in DecodePDU(True).list_function_codes():
        if fc not in implemented:
            classes.append(
                type(f"Unsupported{fc:02X}", (UnsupportedRequest,), {**bound, "function_code": fc})
            )
    return classes


def create_modbus_server(
    board: RelayBoard,
    host: str = "0.0.0.0",
    port: int = 5020,
    strict_unit_id: bool = False,
) -> ModbusTcpServer:
    """Instantiate (but do not start) the asyncio Modbus TCP server.

    Must be called from within a running event loop (pymodbus requirement).
    """
    # pymodbus insists on a datastore; our PDUs never touch it, so a minimal
    # one-register device is enough. id=0 => answers on every unit id.
    dummy_context = SimDevice(0, simdata=[SimData(0, values=0, datatype=DataType.REGISTERS)])

    def on_connect(connected: bool) -> None:
        board.log_event("Modbus client connected" if connected else "Modbus client disconnected", "tcp")

    return ModbusTcpServer(
        dummy_context,
        address=(host, port),
        custom_pdu=build_request_classes(board, strict_unit_id),
        ignore_missing_devices=True,   # needed for strict_unit_id "no answer"
        trace_connect=on_connect,
    )
