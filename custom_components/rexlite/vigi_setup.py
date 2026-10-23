"""Activate a factory-fresh TP-Link VIGI camera from the hub.

A new or reset VIGI camera answers ONVIF discovery but refuses every login
until an ``admin`` password is set through its web UI. The hub shares the
camera LAN, so it sends the same anonymous ``set_password`` request the
camera's own page sends: the password RSA-encrypted (PKCS#1 v1.5) with the
fixed public key every VIGI web UI ships, so it never crosses the LAN in
clear text. The password is used for this request only and never logged.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import re
import secrets
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .camera_probe import CameraError, OnvifClient, _timeout, camera_host

USERNAME = "admin"
# Error code the camera's web API answers with until it has been activated.
UNINITIALIZED = -40405
MAX_REPLY_BYTES = 64 * 1024
# The public key from the VIGI web UI (``-----BEGIN PUBLIC KEY-----MIGf…``).
_MODULUS = int(
    "B80FA8B4A03FC66B9A9BFFF45F49EF0CACF548ACC20679C0B191C1961A3F64C7"
    "193BE2A40DABC5D4B97A32D8F733437D01A62447AE591BB4110F7962E9ABCE6D"
    "7EE6D3523841CF534F36F5AB0BCF2E698546BF336983364EA830B7E7DEB5B8F4"
    "EEBDD51AF2FCED714423119CB5E966E67AD46351114889626014299E9B26156D",
    16,
)
_EXPONENT = 65537
_KEY_BYTES = 128
# Recovery questions the web UI preselects; answers stay empty, like skipping
# them on the camera's own page.
_QUESTIONS = ("father_name", "mother_name", "senior_teacher_name")


def encrypt(text: str) -> str:
    """RSA PKCS#1 v1.5 with the VIGI key, base64 then URL-encoded like the UI."""

    message = text.encode()
    if len(message) > _KEY_BYTES - 11:
        raise CameraError("camera_invalid_request", "密碼過長")
    padding = bytearray()
    while len(padding) < _KEY_BYTES - 3 - len(message):
        padding += bytes(b for b in secrets.token_bytes(16) if b)
    filler = bytes(padding[: _KEY_BYTES - 3 - len(message)])
    block = b"\x00\x02" + filler + b"\x00" + message
    cipher = pow(int.from_bytes(block, "big"), _EXPONENT, _MODULUS)
    encoded = base64.b64encode(cipher.to_bytes(_KEY_BYTES, "big")).decode()
    return quote(encoded, safe="")


def password_error(password: str) -> str:
    """The camera's own strong-password rule; empty when it is acceptable."""

    if not re.fullmatch(r"[\x21-\x7e]{9,32}", password):
        return "密碼需 9–32 個字元，不可含空白或中文"
    if not (
        re.search(r"[0-9]", password)
        and re.search(r"[a-z]", password)
        and re.search(r"[A-Z]", password)
        and re.search(r"\W", password)
    ):
        return "密碼需包含大寫、小寫、數字和符號"
    for index in range(len(password) - 2):
        a, b, c = password[index : index + 3]
        if a == b == c:
            return "密碼不可有 3 個相同的連續字元"
        same_kind = all(ch.isdigit() for ch in (a, b, c)) or all(
            ch.isascii() and ch.isalpha() and ch.islower() == a.islower()
            for ch in (a, b, c)
        )
        if same_kind and ord(a) + 1 == ord(b) and ord(b) + 1 == ord(c):
            return "密碼不可有 3 個連續字元（如 123、abc）"
    if USERNAME in password:
        return "密碼不可包含 admin"
    return ""


async def _call(session: Any, host: str, body: dict, seconds: float) -> dict:
    try:
        async with session.request(
            "POST",
            f"https://{host}/",
            data=json.dumps(body),
            headers={"Content-Type": "application/json"},
            ssl=False,
            timeout=_timeout(seconds),
        ) as response:
            data = (await response.read())[:MAX_REPLY_BYTES]
    except CameraError:
        raise
    except Exception as err:  # noqa: BLE001 - every network failure is one code
        raise CameraError("camera_unreachable", f"無法連線到 {host}") from err
    try:
        reply = json.loads(data)
    except ValueError:
        reply = None
    if not isinstance(reply, dict):
        raise CameraError("camera_not_vigi", "這台攝影機不支援開通")
    return reply


async def vigi_state(session: Any, host: str, seconds: float = 3) -> str | None:
    """``uninitialized``, ``ready``, or None when the host is not a VIGI web API.

    The camera's HTTPS server drops the odd connection (and restarts it right
    after activation), so an unreachable camera is asked once more.
    """

    body = {"method": "do", "user_management": {"get_encrypt_info": None}}
    reply: dict | None = None
    for attempt in range(2):
        if attempt:
            await asyncio.sleep(1)
        try:
            reply = await _call(session, camera_host(host), body, seconds)
            break
        except CameraError as err:
            if err.code != "camera_unreachable":
                return None
    if reply is None:
        return None
    data = reply.get("data") if isinstance(reply.get("data"), dict) else {}
    if data.get("code") == UNINITIALIZED:
        return "uninitialized"
    return "ready" if "error_code" in reply else None


async def _language(session: Any, host: str) -> str:
    """Traditional Chinese for the camera's own UI when it offers it."""

    try:
        reply = await _call(
            session, host, {"method": "do", "get_language_capability": {}}, 5
        )
    except CameraError:
        return "English"
    offered = reply.get("language_capability")
    wanted = "Traditional%20Chinese"
    return wanted if isinstance(offered, list) and wanted in offered else "English"


def posix_tz(name: str | None, now: datetime | None = None) -> str:
    """ONVIF wants a POSIX TZ: ``Asia/Taipei`` → ``UTC-8`` (signs are inverted)."""

    try:
        zone = ZoneInfo(name or "Asia/Taipei")
    except (ValueError, KeyError, OSError):
        zone = ZoneInfo("Asia/Taipei")
    offset = (now or datetime.now(UTC)).astimezone(zone).utcoffset()
    minutes = int(offset.total_seconds() // 60) if offset else 0
    sign = "-" if minutes >= 0 else "+"
    hours, rest = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours}" + (f":{rest:02d}" if rest else "")


async def set_clock(
    session: Any, host: str, port: int, password: str, time_zone: str | None
) -> bool:
    """The hub's time and time zone, over ONVIF, right after activation.

    This is what the camera page's country / time zone fields set; the camera
    may take a few seconds to bring ONVIF up with the new password.
    """

    now = datetime.now(UTC)
    body = (
        "<tds:SetSystemDateAndTime><tds:DateTimeType>Manual</tds:DateTimeType>"
        "<tds:DaylightSavings>false</tds:DaylightSavings>"
        f"<tds:TimeZone><tt:TZ>{posix_tz(time_zone, now)}</tt:TZ></tds:TimeZone>"
        "<tds:UTCDateTime>"
        f"<tt:Date><tt:Year>{now.year}</tt:Year><tt:Month>{now.month}</tt:Month>"
        f"<tt:Day>{now.day}</tt:Day></tt:Date>"
        f"<tt:Time><tt:Hour>{now.hour}</tt:Hour><tt:Minute>{now.minute}</tt:Minute>"
        f"<tt:Second>{now.second}</tt:Second></tt:Time>"
        "</tds:UTCDateTime></tds:SetSystemDateAndTime>"
    )
    for attempt in range(3):
        if attempt:
            await asyncio.sleep(2)
        client = OnvifClient(session, host, port, USERNAME, password, seconds=5)
        with contextlib.suppress(CameraError):
            await client.sync_clock()
            await client.call(
                client.device_url,
                "http://www.onvif.org/ver10/device/wsdl/SetSystemDateAndTime",
                body,
            )
            return True
    return False


async def vigi_activate(
    session: Any,
    host: str,
    password: str,
    *,
    onvif_port: int = 80,
    time_zone: str | None = None,
) -> dict:
    """Set the ``admin`` password on a VIGI camera that has never been set up,
    then its clock, so nothing on the camera's own setup page is left to fill.
    """

    host = camera_host(host)
    problem = password_error(password)
    if problem:
        raise CameraError("camera_weak_password", problem)
    state = await vigi_state(session, host, 5)
    if state is None:
        raise CameraError("camera_not_vigi", "這台攝影機不支援開通，請用原廠 App")
    if state == "ready":
        raise CameraError(
            "camera_already_active", "攝影機已開通，請輸入開通時設定的密碼"
        )
    request: dict[str, Any] = {
        "username": USERNAME,
        "ciphertext": encrypt(password),
        "reset_pwd_email": encrypt(""),
        "pwd_sec_type": "1",
        "language": await _language(session, host),
    }
    for index, question in enumerate(_QUESTIONS, start=1):
        request[f"sec_question_{index}"] = question
        request[f"sec_answer_{index}"] = ""
    reply = await _call(session, host, {"method": "do", "set_password": request}, 15)
    if reply.get("error_code") != 0:
        raise CameraError(
            "camera_activate_failed",
            f"開通失敗（{reply.get('error_code')}），請到攝影機網頁開通",
        )
    clock = await set_clock(session, host, onvif_port, password, time_zone)
    return {"host": host, "username": USERNAME, "activated": True, "clock": clock}
