"""Somfy SDN and Modbus TCP gateway protocol checks against vendor captures."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path

PACKAGE = Path(__file__).parents[1] / "custom_components/rexlite"
package = types.ModuleType("rexlite_rs485_tests")
package.__path__ = [str(PACKAGE)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(
    f"{package.__name__}.rs485_protocol", PACKAGE / "rs485_protocol.py"
)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)

# 璞真 site, 窗簾編號ID以及指令.txt: motor -> OPEN, CLOSE, STOP, GET_POSITION.
VENDOR = {
    "203F95": (
        "FC7009FFFFFE6AC0DFFEFFFFFF0A75",
        "FC7009FFFFFE6AC0DFFFFFFFFF0A76",
        "FD7309FFFFFE6AC0DFFE077C",
        "F37409FFFFFE6AC0DF0675",
    ),
    "203FAB": (
        "FC7009FFFFFE54C0DFFEFFFFFF0A5F",
        "FC7009FFFFFE54C0DFFFFFFFFF0A60",
        "FD7309FFFFFE54C0DFFE0766",
        "F37409FFFFFE54C0DF065F",
    ),
    "203FB5": (
        "FC7009FFFFFE4AC0DFFEFFFFFF0A55",
        "FC7009FFFFFE4AC0DFFFFFFFFF0A56",
        "FD7309FFFFFE4AC0DFFE075C",
        "F37409FFFFFE4AC0DF0655",
    ),
    "203F58": (
        "FC7009FFFFFEA7C0DFFEFFFFFF0AB2",
        "FC7009FFFFFEA7C0DFFFFFFFFF0AB3",
        "FD7309FFFFFEA7C0DFFE07B9",
        "F37409FFFFFEA7C0DF06B2",
    ),
    "203FA1": (
        "FC7009FFFFFE5EC0DFFEFFFFFF0A69",
        "FC7009FFFFFE5EC0DFFFFFFFFF0A6A",
        "FD7309FFFFFE5EC0DFFE0770",
        "F37409FFFFFE5EC0DF0669",
    ),
    "203F81": (
        "FC7009FFFFFE7EC0DFFEFFFFFF0A89",
        "FC7009FFFFFE7EC0DFFFFFFFFF0A8A",
        "FD7309FFFFFE7EC0DFFE0790",
        "F37409FFFFFE7EC0DF0689",
    ),
    "203F8B": (
        "FC7009FFFFFE74C0DFFEFFFFFF0A7F",
        "FC7009FFFFFE74C0DFFFFFFFFF0A80",
        "FD7309FFFFFE74C0DFFE0786",
        "F37409FFFFFE74C0DF067F",
    ),
}
# The capture tool dropped one byte of each repeated FF run; restored here,
# every frame then matches its LEN field and checksum (203FAB motor).
REPLY_OPEN = "F2E99054C0DFFFFFFEFFFFFF00010000FF7F00000BD6"
REPLY_HALF = "F2E99054C0DFFFFFFE0EFACD00010000FF7F00000AAE"
REPLY_CLOSED = "F2E99054C0DFFFFFFE1DF49B00010000FF7F00000A85"


def reply(msg: int, motor: str, data: bytes = b"") -> bytes:
    """A frame as a Glydea motor sends it to the master."""

    decoded = (
        bytes((msg, 11 + len(data), 0x6F))
        + bytes.fromhex(motor)[::-1]
        + bytes((0x00, 0x00, 0x01))
        + data
    )
    raw = bytes(b ^ 0xFF for b in decoded)
    return raw + (sum(raw) & 0xFFFF).to_bytes(2, "big")


class SomfyFrameTests(unittest.TestCase):
    def test_commands_match_every_vendor_frame(self):
        for motor, frames in VENDOR.items():
            for action, expected in zip(
                ("open", "close", "stop", "get_position"), frames, strict=True
            ):
                self.assertEqual(
                    m.somfy_command(motor, action).hex().upper(), expected, motor
                )

    def test_fifty_percent_matches_vendor_and_ha_100_is_open(self):
        self.assertEqual(
            m.somfy_command("203fab", "position", 50).hex().upper(),
            "FC7009FFFFFE54C0DFFBCDFFFF0A2A",
        )
        frame = m.somfy_command("203FAB", "position", 100)
        self.assertEqual(frame[10] ^ 0xFF, 0)
        with self.assertRaises(m.GatewayError):
            m.somfy_command("203FAB", "position", 101)

    def test_discovery_is_a_valid_broadcast_frame(self):
        for ack in (False, True):
            frames = m.parse_somfy_frames(m.somfy_discovery(ack=ack))
            self.assertEqual(len(frames), 1)
            self.assertEqual(frames[0].msg, m.GET_NODE_ADDR)
        raw = m.somfy_discovery(ack=False)
        self.assertEqual(raw[6:9], b"\x00\x00\x00")  # FFFFFF inverted
        self.assertEqual(raw[1] ^ 0xFF, 11)

    def test_vendor_position_replies(self):
        for value, expected in (
            (REPLY_OPEN, 100),
            (REPLY_HALF, 50),
            (REPLY_CLOSED, 0),
        ):
            frames = m.parse_somfy_frames(bytes.fromhex(value))
            self.assertEqual(len(frames), 1, value)
            self.assertEqual(frames[0].source, "203FAB")
            self.assertEqual(frames[0].node_type, 6)
            self.assertEqual(m.somfy_position(frames[0]), expected)

    def test_stream_resyncs_after_noise_and_bad_checksum(self):
        bad = bytearray(bytes.fromhex(REPLY_HALF))
        bad[-1] ^= 0x01
        stream = b"\x00\x13" + bytes(bad) + bytes.fromhex(REPLY_CLOSED) * 2
        frames = m.parse_somfy_frames(stream)
        self.assertEqual([m.somfy_position(f) for f in frames], [0, 0])

    def test_unknown_position_and_labels(self):
        unknown = m.parse_somfy_frames(
            reply(m.POST_MOTOR_POSITION, "203F95", bytes((0, 0, 0xFF, 0, 0xFF)))
        )[0]
        self.assertIsNone(m.somfy_position(unknown))
        label = m.parse_somfy_frames(
            reply(m.POST_NODE_LABEL, "203F95", b"LIVING SHEER\x00   ")
        )[0]
        self.assertEqual(m.somfy_label(label), "LIVING SHEER")

    def test_gateway_must_be_private_lan(self):
        self.assertEqual(m.gateway_host(" 192.168.77.95 "), "192.168.77.95")
        for bad in ("8.8.8.8", "127.0.0.1", "172.17.0.1", "169.254.1.1", "x"):
            with self.assertRaises(m.GatewayError):
                m.gateway_host(bad)
        for bad in (0, 70000, "abc", True):
            with self.assertRaises(m.GatewayError):
                m.gateway_port(bad)


class GatewayEmulatorTests(unittest.IsolatedAsyncioTestCase):
    async def serve(self, handler):
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        return server.sockets[0].getsockname()[1]

    async def test_somfy_scan_finds_motors_with_label_and_position(self):
        motors = {"203F95": (b"SHEER", 30), "203FAB": (b"DRAPE", 100)}
        received: list[bytes] = []

        async def handle(reader, writer):
            while data := await reader.read(256):
                received.append(data)
                for frame in m.parse_somfy_frames(data):
                    decoded = bytes(b ^ 0xFF for b in data[:9])
                    target = decoded[6:9][::-1].hex().upper()
                    if frame.msg == m.GET_NODE_ADDR:
                        for motor in motors:
                            writer.write(reply(m.POST_NODE_ADDR, motor))
                    elif frame.msg == m.GET_NODE_LABEL:
                        name = motors[target][0].ljust(16)
                        writer.write(reply(m.POST_NODE_LABEL, target, name))
                    elif frame.msg == m.GET_MOTOR_POSITION:
                        percent = motors[target][1]
                        writer.write(
                            reply(
                                m.POST_MOTOR_POSITION,
                                target,
                                bytes((0, 0, percent, 0, 0xFF)),
                            )
                        )
                    await writer.drain()
            writer.close()

        port = await self.serve(handle)
        devices = await m.somfy_scan(
            "127.0.0.1", port, rounds=2, window=0.2, reply_window=0.2
        )
        self.assertEqual(
            devices,
            [
                {"address": "203F95", "nodeType": 6, "label": "SHEER", "position": 70},
                {"address": "203FAB", "nodeType": 6, "label": "DRAPE", "position": 0},
            ],
        )
        sent = b"".join(received)
        self.assertIn(m.somfy_discovery(ack=False), sent)
        self.assertIn(m.somfy_discovery(ack=True), sent)

    async def test_somfy_send_writes_the_vendor_frame(self):
        received = bytearray()

        async def handle(reader, writer):
            received.extend(await reader.read(64))
            writer.close()

        port = await self.serve(handle)
        await m.somfy_send("127.0.0.1", port, "203F95", "open")
        self.assertEqual(received.hex().upper(), VENDOR["203F95"][0])
        with self.assertRaises(m.GatewayError):
            await m.somfy_send("127.0.0.1", port, "203F95", "position")

    async def test_modbus_scan_reads_units_and_ignores_late_replies(self):
        units = {2: [1, 4, 3, 26], 5: [0, 0, 5, 24]}

        async def handle(reader, writer):
            try:
                await serve_modbus(reader, writer)
            except asyncio.IncompleteReadError:
                writer.close()

        async def serve_modbus(reader, writer):
            while header := await reader.readexactly(7):
                pdu = await reader.readexactly(int.from_bytes(header[4:6], "big") - 1)
                unit = header[6]
                if unit == 3:  # answers after the master gave up
                    await asyncio.sleep(0.3)
                    registers = [1, 0, 1, 99]
                elif unit == 4:  # gateway exception: target failed
                    body = bytes((unit, 0x83, 0x0B))
                    writer.write(header[:4] + len(body).to_bytes(2, "big") + body)
                    await writer.drain()
                    continue
                elif unit in units:
                    registers = units[unit]
                else:
                    continue
                count = int.from_bytes(pdu[3:5], "big")
                data = b"".join(r.to_bytes(2, "big") for r in registers[:count])
                body = bytes((unit, 0x03, len(data))) + data
                writer.write(header[:4] + len(body).to_bytes(2, "big") + body)
                await writer.drain()

        port = await self.serve(handle)
        found = await m.modbus_scan("127.0.0.1", port, max_unit=6, reply_seconds=0.2)
        self.assertEqual(
            found,
            [
                {
                    "address": "2",
                    "state": {
                        "power": "ON",
                        "mode": "heat",
                        "fan": "medium",
                        "roomTemperature": 26,
                    },
                },
                {
                    "address": "5",
                    "state": {
                        "power": "OFF",
                        "mode": "off",
                        "fan": "auto",
                        "roomTemperature": 24,
                    },
                },
            ],
        )

    async def test_unreachable_gateway_has_a_clear_error(self):
        server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()
        with self.assertRaises(m.GatewayError) as caught:
            await m.modbus_scan("127.0.0.1", port, max_unit=1)
        self.assertEqual(caught.exception.code, "node_red_gateway_unreachable")


class YaleFrameTests(unittest.TestCase):
    """YA071 v1.6 examples (2.1-2.3, 3.4, 3.7) and the 2026-09-09 site capture."""

    def test_commands_match_the_vendor_frames(self):
        self.assertEqual(m.yale_query().hex().upper(), "059101118 10F".replace(" ", ""))
        self.assertEqual(m.yale_frame(0x02, b"\x11").hex().upper(), "05910211820F")
        self.assertEqual(m.yale_frame(0x02, b"\x12").hex().upper(), "05910212810F")
        # The ACK for a card unlock repeats the event with ID 91h and a new CRC.
        self.assertEqual(
            m.yale_frame(0x81, bytes.fromhex("3400FF")).hex().upper(),
            "0591813400FFDB0F",
        )

    def test_status_nibbles_are_lock_then_door(self):
        expected = {
            0x11: ("UNLOCKED", "OPEN"),
            0x12: ("UNLOCKED", "CLOSE"),
            0x21: ("LOCKED", "OPEN"),
            0x22: ("LOCKED", "CLOSE"),
        }
        for status, (lock, door) in expected.items():
            frame = m.yale_frame(0x01, bytes((0x21, status)), m.YALE_FROM_MODULE)
            (parsed,) = m.parse_yale_frames(frame)
            self.assertEqual(m.yale_status(parsed), {"lock": lock, "door": door})
        (site,) = m.parse_yale_frames(bytes.fromhex("0519012111280f"))
        self.assertEqual(m.yale_status(site), {"lock": "UNLOCKED", "door": "OPEN"})

    def test_stream_resyncs_and_rejects_bad_crc(self):
        stream = bytes.fromhex(
            "aa050519012111290f0519813400ff530f051982118a0f0519fe11f60f"
        )
        frames = m.parse_yale_frames(stream)
        self.assertEqual(
            [(f.cmd, f.data.hex()) for f in frames],
            [(0x81, "3400ff"), (0x82, "11"), (0xFE, "11")],
        )


class YaleGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def serve(self, handler):
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        return server.sockets[0].getsockname()[1]

    async def test_scan_reports_lock_and_door(self):
        received = bytearray()

        async def handle(reader, writer):
            while data := await reader.read(64):
                received.extend(data)
                if data == m.yale_query():
                    # Noise from the serial line, then the reply split in two.
                    writer.write(bytes.fromhex("ff0519012122"))
                    await writer.drain()
                    writer.write(bytes.fromhex("1b0f"))
                    await writer.drain()
            writer.close()

        port = await self.serve(handle)
        devices = await m.yale_scan("127.0.0.1", port, window=0.3)
        self.assertEqual(
            devices, [{"address": "1", "lock": {"lock": "LOCKED", "door": "CLOSE"}}]
        )
        self.assertEqual(bytes(received[:6]), m.yale_query())

    async def test_rf_error_and_silence_are_distinguished(self):
        async def rf_error(reader, writer):
            while await reader.read(64):
                writer.write(bytes.fromhex("0519fe11f60f"))
                await writer.drain()
            writer.close()

        port = await self.serve(rf_error)
        with self.assertRaises(m.GatewayError) as caught:
            await m.yale_scan("127.0.0.1", port, window=0.2)
        self.assertEqual(caught.exception.code, "node_red_device_unreachable")

        async def silent(reader, writer):
            while await reader.read(64):
                pass
            writer.close()

        port = await self.serve(silent)
        self.assertEqual(await m.yale_scan("127.0.0.1", port, window=0.2), [])


if __name__ == "__main__":
    unittest.main()
