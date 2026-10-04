"""End-to-end tests: real TCP sockets, hand-crafted Modbus frames.

Hand-crafted frames (rather than a pymodbus client) are used on purpose: a
generic client cannot send the Waveshare-specific FC05 values (0x5500, flash
delays), and byte-level checks prove wire-level fidelity.

Run with:  python -m unittest -v
"""

from __future__ import annotations

import asyncio
import json
import struct
import unittest

from aiohttp import ClientSession

from waveshare_sim.config import DeviceInfo
from waveshare_sim.modbus_server import create_modbus_server
from waveshare_sim.relay_board import RelayBoard
from waveshare_sim.web_server import WebServer

MODBUS_PORT = 15020
WEB_PORT = 18080


class ModbusRaw:
    """Minimal Modbus TCP client speaking raw MBAP frames."""

    def __init__(self) -> None:
        self.tid = 0

    async def connect(self) -> None:
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", MODBUS_PORT)

    async def close(self) -> None:
        self.writer.close()
        await self.writer.wait_closed()

    async def request(self, pdu: bytes, unit: int = 1) -> bytes:
        """Send a PDU, return the response PDU (function code + data)."""
        self.tid = (self.tid + 1) & 0xFFFF
        self.writer.write(struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, unit) + pdu)
        await self.writer.drain()
        header = await asyncio.wait_for(self.reader.readexactly(7), 2)
        tid, proto, length, _unit = struct.unpack(">HHHB", header)
        assert tid == self.tid and proto == 0
        return await self.reader.readexactly(length - 1)


class SimulatorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.board = RelayBoard(DeviceInfo(ip="192.168.1.200", mac="02:57:53:52:4C:08"))
        self.modbus = create_modbus_server(self.board, "127.0.0.1", MODBUS_PORT)
        await self.modbus.serve_forever(background=True)
        self.web = WebServer(self.board, "127.0.0.1", WEB_PORT)
        await self.web.start()
        self.cli = ModbusRaw()
        await self.cli.connect()

    async def asyncTearDown(self) -> None:
        await self.cli.close()
        await self.modbus.shutdown()
        await self.board.shutdown()
        await self.web.stop()

    # ------------------------------------------------------------------ #
    async def test_write_and_read_single_relay(self):
        # Waveshare doc example: 01 05 00 00 FF 00 -> relay 0 ON, echoed back.
        self.assertEqual(await self.cli.request(bytes.fromhex("050000FF00")), bytes.fromhex("050000FF00"))
        self.assertEqual(await self.cli.request(bytes.fromhex("0500030000") ), bytes.fromhex("0500030000"))
        self.assertTrue(self.board.states[0])
        resp = await self.cli.request(bytes.fromhex("0100000008"))
        self.assertEqual(resp, bytes.fromhex("010101"))

    async def test_toggle_and_all(self):
        await self.cli.request(bytes.fromhex("0500025500"))   # toggle CH3
        self.assertEqual(self.board.bitmap(), 0b100)
        await self.cli.request(bytes.fromhex("0500FFFF00"))   # all ON
        self.assertEqual(self.board.bitmap(), 0xFF)
        await self.cli.request(bytes.fromhex("0500FF5500"))   # toggle all
        self.assertEqual(self.board.bitmap(), 0x00)

    async def test_write_multiple(self):
        # FC0F: 8 coils from 0, pattern 0xA5
        resp = await self.cli.request(bytes.fromhex("0F0000000801A5"))
        self.assertEqual(resp, bytes.fromhex("0F00000008"))
        self.assertEqual(self.board.bitmap(), 0xA5)

    async def test_flash_on(self):
        # Flash ON CH2 for 3 x 100 ms
        resp = await self.cli.request(bytes.fromhex("0502010003"))
        self.assertEqual(resp, bytes.fromhex("0502010003"))
        self.assertTrue(self.board.states[1])
        await asyncio.sleep(0.2)
        self.assertTrue(self.board.states[1])
        await asyncio.sleep(0.25)
        self.assertFalse(self.board.states[1])

    async def test_flash_off_and_cancel(self):
        await self.cli.request(bytes.fromhex("050004FF00"))   # CH5 ON
        await self.cli.request(bytes.fromhex("0504040002"))   # Flash OFF 200 ms
        self.assertFalse(self.board.states[4])
        await asyncio.sleep(0.3)
        self.assertTrue(self.board.states[4])
        # A plain write cancels a pending flash: latest command wins.
        await self.cli.request(bytes.fromhex("0502040005"))   # Flash ON 500 ms
        await self.cli.request(bytes.fromhex("050004FF00"))   # force ON
        await asyncio.sleep(0.6)
        self.assertTrue(self.board.states[4])

    async def test_flash_via_fc06_alias(self):
        await self.cli.request(bytes.fromhex("0602000002"))   # FC06 flash ON CH1
        self.assertTrue(self.board.states[0])
        await asyncio.sleep(0.3)
        self.assertFalse(self.board.states[0])

    async def test_exceptions(self):
        self.assertEqual(await self.cli.request(bytes.fromhex("050008FF00")), bytes.fromhex("8502"))
        self.assertEqual(await self.cli.request(bytes.fromhex("0500001234")), bytes.fromhex("8503"))
        self.assertEqual(await self.cli.request(bytes.fromhex("0502000000")), bytes.fromhex("8503"))
        self.assertEqual(await self.cli.request(bytes.fromhex("0100000009")), bytes.fromhex("8102"))
        self.assertEqual(await self.cli.request(bytes.fromhex("0200000008")), bytes.fromhex("8201"))
        self.assertEqual(await self.cli.request(bytes.fromhex("100000000102ABCD")), bytes.fromhex("9001"))

    async def test_holding_registers(self):
        self.assertEqual(await self.cli.request(bytes.fromhex("0380000001")), bytes.fromhex("030200C8"))
        self.assertEqual(await self.cli.request(bytes.fromhex("0340000001")), bytes.fromhex("03020001"))
        await self.cli.request(bytes.fromhex("0640000005"))
        self.assertEqual(self.board.info.device_address, 5)

    async def test_input_registers(self):
        resp = await self.cli.request(bytes.fromhex("040000000D"))
        regs = struct.unpack(">B13H", resp[1:])[1:]
        self.assertEqual(regs[0:3], (0x0257, 0x5352, 0x4C08))      # MAC
        self.assertEqual(regs[3:5], (0xC0A8, 0x01C8))              # 192.168.1.200
        self.assertEqual(regs[5:7], (0xFFFF, 0xFF00))              # 255.255.255.0
        self.assertEqual(regs[7:9], (0xC0A8, 0x0101))              # 192.168.1.1
        self.assertEqual(regs[9], 5020)
        self.assertEqual(regs[10], 0x00C8)
        self.assertEqual(regs[12], 8)

    async def test_websocket_push(self):
        async with ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{WEB_PORT}/") as r:
                self.assertEqual(r.status, 200)
                self.assertIn("Waveshare", await r.text())
            async with session.ws_connect(f"http://127.0.0.1:{WEB_PORT}/ws") as ws:
                first = json.loads((await ws.receive(timeout=2)).data)
                self.assertFalse(first["relays"][6]["on"])
                await self.cli.request(bytes.fromhex("050006FF00"))
                pushed = json.loads((await ws.receive(timeout=2)).data)
                self.assertTrue(pushed["relays"][6]["on"])


if __name__ == "__main__":
    unittest.main()
