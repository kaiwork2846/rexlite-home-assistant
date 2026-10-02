"""RS-485 / RS-232 gateway protocols used by REXLiTE Node-RED bridges.

Somfy SDN (Glydea curtain motors), Hitachi air conditioners behind a
Modbus TCP gateway, and the Yale / GATEMAN YA071 RF link module (one lock
per RS-232 port of a transparent TCP serial server). Everything here is
plain asyncio so it can be unit tested without Home Assistant; the bridge
manager only calls the async helpers.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import time
from dataclasses import dataclass, field

SOMFY_TEMPLATE = "somfy_curtain_rs485"
HITACHI_TEMPLATE = "hitachi_ac_modbus"
YALE_TEMPLATE = "yale_lock_ya071"
TEMPLATES = (HITACHI_TEMPLATE, SOMFY_TEMPLATE, YALE_TEMPLATE)
# YA071 has no bus address: one lock per serial port, always device "1".
YALE_ADDRESS = "1"

# SDN frames are sent bit-inverted. Header bytes below are the decoded values
# used by the vendor-verified frames (NodeType F6h: master -> Glydea motor).
_SOMFY_NODE_TYPE = 0xF6
_SOMFY_MASTER = bytes((0x00, 0x00, 0x01))
_SOMFY_BROADCAST = bytes((0xFF, 0xFF, 0xFF))
_ACK = 0x80
GET_NODE_ADDR = 0x40
GET_NODE_LABEL = 0x45
GET_MOTOR_POSITION = 0x0C
POST_MOTOR_POSITION = 0x0D
POST_NODE_ADDR = 0x60
POST_NODE_LABEL = 0x65
CTRL_MOVETO = 0x03
CTRL_STOP = 0x02

HITACHI_MODES = {0: "cool", 1: "dry", 2: "fan_only", 3: "auto", 4: "heat"}
HITACHI_FANS = {5: "auto", 7: "quiet", 1: "low", 3: "medium", 4: "high"}


class GatewayError(Exception):
    """A gateway could not be reached or answered unexpectedly."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def gateway_host(value: object) -> str:
    """Accept only RFC1918 IPv4 gateways; never let a command reach the internet."""

    try:
        address = ipaddress.IPv4Address(str(value).strip())
    except ValueError:
        raise GatewayError("node_red_invalid_gateway", "閘道 IP 格式不正確") from None
    if (
        not address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
        or address == ipaddress.IPv4Address("172.17.0.1")
    ):
        raise GatewayError("node_red_invalid_gateway", "閘道必須是區網私有 IP")
    return str(address)


def gateway_port(value: object) -> int:
    if isinstance(value, bool):
        raise GatewayError("node_red_invalid_gateway", "閘道 Port 需為 1–65535")
    try:
        port = int(str(value))
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise GatewayError("node_red_invalid_gateway", "閘道 Port 需為 1–65535")
    return port


def motor_id(value: object) -> str:
    text = str(value).strip().upper()
    if len(text) != 6 or any(c not in "0123456789ABCDEF" for c in text):
        raise GatewayError(
            "node_red_invalid_device", "馬達 ID 需為 6 碼十六進位，例如 203F95"
        )
    return text


def _lsbf(node_id: str) -> bytes:
    return bytes.fromhex(node_id)[::-1]


def somfy_frame(
    msg: int, destination: bytes, data: bytes = b"", *, ack: bool = True
) -> bytes:
    """Build one wire frame: inverted bytes followed by a 16-bit byte sum."""

    length = 11 + len(data)
    decoded = bytes((msg, (_ACK if ack else 0) | length, _SOMFY_NODE_TYPE)) + (
        _SOMFY_MASTER + destination + data
    )
    raw = bytes(b ^ 0xFF for b in decoded)
    total = sum(raw) & 0xFFFF
    return raw + total.to_bytes(2, "big")


def somfy_command(node_id: str, action: str, position: int | None = None) -> bytes:
    """Frames identical to the vendor capture (窗簾編號ID以及指令.txt)."""

    destination = _lsbf(motor_id(node_id))
    if action == "open":
        return somfy_frame(CTRL_MOVETO, destination, bytes((0x01, 0, 0, 0)))
    if action == "close":
        return somfy_frame(CTRL_MOVETO, destination, bytes((0x00, 0, 0, 0)))
    if action == "stop":
        return somfy_frame(CTRL_STOP, destination, bytes((0x01,)))
    if action == "position":
        if position is None or not 0 <= position <= 100:
            raise GatewayError("node_red_invalid_device", "開度需為 0–100")
        # HA 100 = fully open; the motor reports 0% at the UP limit.
        return somfy_frame(
            CTRL_MOVETO, destination, bytes((0x04, 100 - position, 0, 0))
        )
    if action == "get_position":
        return somfy_frame(GET_MOTOR_POSITION, destination)
    if action == "get_label":
        return somfy_frame(GET_NODE_LABEL, destination, ack=False)
    raise GatewayError("node_red_invalid_device", "不支援的窗簾指令")


def somfy_discovery(*, ack: bool) -> bytes:
    return somfy_frame(GET_NODE_ADDR, _SOMFY_BROADCAST, ack=ack)


@dataclass(frozen=True, slots=True)
class SomfyFrame:
    msg: int
    node_type: int
    source: str
    data: bytes


def parse_somfy_frames(buffer: bytes) -> list[SomfyFrame]:
    """Split a gateway stream into checksum-valid SDN frames."""

    frames: list[SomfyFrame] = []
    index = 0
    while index + 11 <= len(buffer):
        length = (buffer[index + 1] ^ 0xFF) & 0x1F
        end = index + length
        if length < 11 or end > len(buffer):
            index += 1
            continue
        raw = buffer[index:end]
        if sum(raw[:-2]) & 0xFFFF != int.from_bytes(raw[-2:], "big"):
            index += 1
            continue
        decoded = bytes(b ^ 0xFF for b in raw[:-2])
        frames.append(
            SomfyFrame(
                msg=decoded[0],
                node_type=decoded[2] >> 4,
                source=decoded[3:6][::-1].hex().upper(),
                data=decoded[9:],
            )
        )
        index = end
    return frames


def somfy_position(frame: SomfyFrame) -> int | None:
    """HA position (100 = open) from POST_MOTOR_POSITION; FFh means unknown."""

    if frame.msg != POST_MOTOR_POSITION or len(frame.data) < 3:
        return None
    percent = frame.data[2]
    if percent > 100:
        return None
    position = 100 - percent
    return 100 if position == 99 else position


def somfy_label(frame: SomfyFrame) -> str:
    if frame.msg != POST_NODE_LABEL:
        return ""
    text = frame.data[:16].decode("ascii", "replace")
    return "".join(c for c in text if c.isprintable()).strip()[:16]


async def _connect(host: str, port: int, seconds: float = 5.0):
    try:
        return await asyncio.wait_for(asyncio.open_connection(host, port), seconds)
    except (TimeoutError, OSError):
        raise GatewayError(
            "node_red_gateway_unreachable",
            f"無法連線到閘道 {host}:{port}，請確認閘道電源、IP 與網路",
        ) from None


async def _close(writer) -> None:
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


async def _collect(reader, window: float) -> bytes:
    """Read whatever the gateway forwards within a fixed listening window."""

    deadline = time.monotonic() + window
    chunks = bytearray()
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            chunk = await asyncio.wait_for(reader.read(512), remaining)
        except TimeoutError:
            break
        if not chunk:
            break
        chunks += chunk
    return bytes(chunks)


async def somfy_scan(
    host: str,
    port: int,
    *,
    rounds: int = 3,
    window: float = 2.0,
    reply_window: float = 0.8,
) -> list[dict]:
    """Broadcast GET_NODE_ADDR, then read each motor's label and position.

    The SDN guide warns that replies can collide on a busy bus, so the
    broadcast is repeated and results are merged. Alternate rounds request
    an acknowledgement because some gateways only forward acknowledged traffic.
    """

    reader, writer = await _connect(host, port)
    found: dict[str, dict] = {}
    try:
        for round_index in range(rounds):
            writer.write(somfy_discovery(ack=round_index % 2 == 1))
            await writer.drain()
            for frame in parse_somfy_frames(await _collect(reader, window)):
                if frame.msg in (POST_NODE_ADDR, POST_MOTOR_POSITION):
                    found.setdefault(
                        frame.source,
                        {
                            "address": frame.source,
                            "nodeType": frame.node_type,
                            "label": "",
                            "position": None,
                        },
                    )
            await asyncio.sleep(0.15)
        for device in found.values():
            for action in ("get_label", "get_position"):
                writer.write(somfy_command(device["address"], action))
                await writer.drain()
                for frame in parse_somfy_frames(await _collect(reader, reply_window)):
                    if frame.source != device["address"]:
                        continue
                    if label := somfy_label(frame):
                        device["label"] = label
                    position = somfy_position(frame)
                    if position is not None:
                        device["position"] = position
                await asyncio.sleep(0.1)
    finally:
        await _close(writer)
    return sorted(found.values(), key=lambda d: d["address"])


async def somfy_send(host: str, port: int, node_id: str, action: str) -> None:
    if action not in ("open", "close", "stop"):
        raise GatewayError("node_red_invalid_device", "只能測試開、關、停")
    reader, writer = await _connect(host, port)
    try:
        writer.write(somfy_command(node_id, action))
        await writer.drain()
        await _collect(reader, 0.3)
    finally:
        await _close(writer)


@dataclass(slots=True)
class ModbusTcp:
    """Minimal Modbus TCP master (function 03) for gateway discovery."""

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    transaction: int = 0
    buffer: bytearray = field(default_factory=bytearray)

    async def read_holding(
        self, unit: int, address: int, count: int, seconds: float
    ) -> list[int] | None:
        """Return register values, or None when the unit does not answer."""

        self.transaction = (self.transaction + 1) & 0xFFFF
        tid = self.transaction
        pdu = bytes((0x03,)) + address.to_bytes(2, "big") + count.to_bytes(2, "big")
        self.writer.write(
            tid.to_bytes(2, "big")
            + b"\x00\x00"
            + (len(pdu) + 1).to_bytes(2, "big")
            + bytes((unit,))
            + pdu
        )
        await self.writer.drain()
        deadline = time.monotonic() + seconds
        while True:
            reply = self._next_frame()
            if reply is not None:
                reply_tid, reply_unit, body = reply
                # Late answers to an earlier, timed-out unit are discarded.
                if reply_tid != tid or reply_unit != unit:
                    continue
                if body[0] == 0x03 and len(body) >= 2 + body[1]:
                    data = body[2 : 2 + body[1]]
                    return [
                        int.from_bytes(data[i : i + 2], "big")
                        for i in range(0, len(data) - 1, 2)
                    ][:count]
                # Exception replies (typically 0Bh, target failed to respond)
                # mean there is no usable unit at this address.
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                chunk = await asyncio.wait_for(self.reader.read(256), remaining)
            except TimeoutError:
                return None
            if not chunk:
                raise GatewayError(
                    "node_red_gateway_unreachable", "閘道中斷連線，請稍後再試"
                )
            self.buffer += chunk

    def _next_frame(self) -> tuple[int, int, bytes] | None:
        if len(self.buffer) < 8:
            return None
        length = int.from_bytes(self.buffer[4:6], "big")
        if length < 2 or length > 254 or self.buffer[2:4] != b"\x00\x00":
            self.buffer.clear()
            return None
        if len(self.buffer) < 6 + length:
            return None
        frame = bytes(self.buffer[: 6 + length])
        del self.buffer[: 6 + length]
        return int.from_bytes(frame[:2], "big"), frame[6], frame[7:]


def hitachi_state(registers: list[int]) -> dict:
    power = registers[0] == 1 if registers else False
    return {
        "power": "ON" if power else "OFF",
        "mode": HITACHI_MODES.get(registers[1], "off")
        if power and len(registers) > 1
        else "off",
        "fan": HITACHI_FANS.get(registers[2], "auto") if len(registers) > 2 else None,
        "roomTemperature": registers[3] if len(registers) > 3 else None,
    }


async def modbus_scan(
    host: str, port: int, *, max_unit: int = 16, reply_seconds: float = 0.8
) -> list[dict]:
    """Ask units 1..max_unit for the status block at 0x0040 (4 registers)."""

    reader, writer = await _connect(host, port)
    client = ModbusTcp(reader, writer)
    found: list[dict] = []
    try:
        for unit in range(1, max_unit + 1):
            registers = await client.read_holding(unit, 0x0040, 4, reply_seconds)
            if registers is not None:
                found.append({"address": str(unit), "state": hitachi_state(registers)})
            await asyncio.sleep(0.05)
    finally:
        await _close(writer)
    return found


# Yale YA071 RF link module (protocol v1.6, 19200 8N1) -------------------------
#
#   05 | ID | CMD | DATA (1-14 bytes) | CRC | 0F
#
# ID 91h = controller -> module, 19h = module -> controller. The high nibble of
# the first DATA byte is the DATA length; CRC = ID ^ CMD ^ every DATA byte.

YALE_START = 0x05
YALE_END = 0x0F
YALE_TO_MODULE = 0x91
YALE_FROM_MODULE = 0x19
YALE_STATUS = 0x01
YALE_CONTROL_ERROR = 0xFE


def yale_frame(cmd: int, data: bytes, ident: int = YALE_TO_MODULE) -> bytes:
    crc = ident ^ cmd
    for byte in data:
        crc ^= byte
    return bytes((YALE_START, ident, cmd)) + data + bytes((crc, YALE_END))


def yale_query() -> bytes:
    """Lock status check (section 2.1): 05 91 01 11 81 0F."""

    return yale_frame(YALE_STATUS, b"\x11")


@dataclass(frozen=True, slots=True)
class YaleFrame:
    ident: int
    cmd: int
    data: bytes


def parse_yale_frames(buffer: bytes) -> list[YaleFrame]:
    """Split a serial stream into CRC-valid YA071 frames, skipping noise."""

    frames: list[YaleFrame] = []
    index = 0
    while index + 6 <= len(buffer):
        if buffer[index] != YALE_START:
            index += 1
            continue
        length = buffer[index + 3] >> 4
        end = index + 5 + length
        if not 1 <= length <= 14 or end > len(buffer) or buffer[end - 1] != YALE_END:
            index += 1
            continue
        ident, cmd = buffer[index + 1], buffer[index + 2]
        data = buffer[index + 3 : index + 3 + length]
        crc = ident ^ cmd
        for byte in data:
            crc ^= byte
        if crc != buffer[end - 2]:
            index += 1
            continue
        frames.append(YaleFrame(ident, cmd, bytes(data)))
        index = end
    return frames


def yale_status(frame: YaleFrame) -> dict | None:
    """Status reply 05 19 01 21 ST.

    High nibble: lock (1 unlocked, 2 locked). Low nibble: door (1 open, 2 closed).
    """

    if frame.ident != YALE_FROM_MODULE or frame.cmd != YALE_STATUS:
        return None
    if len(frame.data) < 2 or frame.data[0] != 0x21:
        return None
    status = frame.data[1]
    return {
        "lock": {1: "UNLOCKED", 2: "LOCKED"}.get(status >> 4),
        "door": {1: "OPEN", 2: "CLOSE"}.get(status & 0x0F),
    }


async def yale_scan(
    host: str, port: int, *, attempts: int = 2, window: float = 3.5
) -> list[dict]:
    """Ask the lock for its status; the reply takes about 2.5 s over RF.

    A control error (FEh) means the serial server and RF module answered but
    the lock itself did not, which is reported separately from "no reply".
    """

    reader, writer = await _connect(host, port)
    rf_error = False
    try:
        for _ in range(attempts):
            writer.write(yale_query())
            await writer.drain()
            frames = parse_yale_frames(await _collect(reader, window))
            for frame in frames:
                if (state := yale_status(frame)) is not None:
                    return [{"address": YALE_ADDRESS, "lock": state}]
                if frame.ident == YALE_FROM_MODULE and frame.cmd == YALE_CONTROL_ERROR:
                    rf_error = True
            await asyncio.sleep(0.3)
    finally:
        await _close(writer)
    if rf_error:
        raise GatewayError(
            "node_red_device_unreachable",
            "閘道與 RF 模組有回應，但電子鎖沒有回應：請確認電子鎖電池與 RF 模組配對",
        )
    return []
