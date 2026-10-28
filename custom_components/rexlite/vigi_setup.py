"""Activate a factory-fresh TP-Link VIGI camera from the hub.

A new or reset VIGI camera answers ONVIF discovery but refuses every login
until an ``admin`` password is set. Two ways, both from the hub on the camera
LAN, the password always RSA-encrypted (PKCS#1 v1.5) so it never crosses the
LAN in clear text:

* the documented OpenAPI initialisation (VIGI IPC Open API Document V1.3,
  §2.4; InSight models such as the LPR345Z): ``getEncrptInfo`` on
  ``https://<ip>/openapi/initialize`` hands out the camera's own RSA key,
  then ``setPassword``. It only answers before the first password;
* otherwise the anonymous ``set_password`` the camera's own web page sends,
  with the fixed public key every VIGI web UI ships (VIGI C540V).

The password is never logged; the bridge remembers it on the hub so the
camera can be added and set up without typing it (see ``frigate_bridge``).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import re
import secrets
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, unquote
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
INIT_PATH = "openapi/initialize"
# Recovery questions the web UI preselects; answers stay empty, like skipping
# them on the camera's own page.
_QUESTIONS = ("father_name", "mother_name", "senior_teacher_name")


def encrypt(text: str, modulus: int | None = None, exponent: int = _EXPONENT) -> str:
    """RSA PKCS#1 v1.5 (the VIGI web key by default), base64 then URL-encoded."""

    modulus = modulus or _MODULUS
    size = (modulus.bit_length() + 7) // 8
    message = text.encode()
    if len(message) > size - 11:
        raise CameraError("camera_invalid_request", "密碼過長")
    padding = bytearray()
    while len(padding) < size - 3 - len(message):
        padding += bytes(b for b in secrets.token_bytes(16) if b)
    filler = bytes(padding[: size - 3 - len(message)])
    block = b"\x00\x02" + filler + b"\x00" + message
    cipher = pow(int.from_bytes(block, "big"), exponent, modulus)
    encoded = base64.b64encode(cipher.to_bytes(size, "big")).decode()
    return quote(encoded, safe="")


def _der(data: bytes, index: int) -> tuple[int, int, int]:
    """Tag, content start and content end of the DER element at ``index``."""

    tag, length = data[index], data[index + 1]
    index += 2
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(data[index : index + count], "big")
        index += count
    if index + length > len(data):
        raise ValueError("truncated")
    return tag, index, index + length


def public_key(spki: str) -> tuple[int, int]:
    """Modulus and exponent of the base64 SubjectPublicKeyInfo OpenAPI returns."""

    text = re.sub(r"-----[A-Z ]+-----|\s", "", unquote(str(spki or "")))
    try:
        data = base64.b64decode(text, validate=True)
        _tag, start, _end = _der(data, 0)  # SubjectPublicKeyInfo
        _tag, _start, algorithm_end = _der(data, start)  # AlgorithmIdentifier
        tag, start, _end = _der(data, algorithm_end)  # BIT STRING
        if tag != 0x03:
            raise ValueError("no key")
        _tag, start, _end = _der(data, start + 1)  # RSAPublicKey, past unused bits
        _tag, n_start, n_end = _der(data, start)
        _tag, e_start, e_end = _der(data, n_end)
    except (ValueError, IndexError, binascii.Error) as err:
        raise CameraError(
            "camera_activate_failed", "攝影機的加密金鑰格式不正確，請到攝影機網頁開通"
        ) from err
    modulus = int.from_bytes(data[n_start:n_end], "big")
    exponent = int.from_bytes(data[e_start:e_end], "big")
    if modulus.bit_length() < 1024 or exponent < 3:
        raise CameraError("camera_activate_failed", "攝影機的加密金鑰不正確")
    return modulus, exponent


def password_error(password: str) -> str:
    """The camera's own strong-password rule; empty when it is acceptable."""

    # Both ways' rules at once: the web page's (9–32, no "admin") and
    # OpenAPI's (10–64, no two identical characters in a row).
    if not re.fullmatch(r"[\x21-\x7e]{10,32}", password):
        return "密碼需 10–32 個字元，不可含空白或中文"
    if not (
        re.search(r"[0-9]", password)
        and re.search(r"[a-z]", password)
        and re.search(r"[A-Z]", password)
        and re.search(r"\W", password)
    ):
        return "密碼需包含大寫、小寫、數字和符號"
    for index in range(len(password) - 2):
        a, b, c = password[index : index + 3]
        if a == b or b == c:
            return "密碼不可有 2 個相同的連續字元"
        same_kind = all(ch.isdigit() for ch in (a, b, c)) or all(
            ch.isascii() and ch.isalpha() and ch.islower() == a.islower()
            for ch in (a, b, c)
        )
        if same_kind and ord(a) + 1 == ord(b) and ord(b) + 1 == ord(c):
            return "密碼不可有 3 個連續字元（如 123、abc）"
    if USERNAME in password:
        return "密碼不可包含 admin"
    return ""


async def _call(
    session: Any, host: str, body: dict, seconds: float, path: str = ""
) -> dict:
    try:
        async with session.request(
            "POST",
            f"https://{host}/{path}",
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


def _error(reply: dict) -> int | None:
    for source in (reply, reply.get("result")):
        if isinstance(source, dict):
            for key in ("error_code", "errCode"):
                value = source.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    return value
    return None


async def openapi_init_key(session: Any, host: str, seconds: float = 3) -> str | None:
    """The camera's RSA key for the OpenAPI initialisation; None when it offers
    none (older firmware, or a camera that already has a password)."""

    # The document's example spells the method "getEncrptInfo", its table
    # "getEncryptInfo"; firmware may take either.
    for method in ("getEncrptInfo", "getEncryptInfo"):
        try:
            reply = await _call(session, host, {"method": method}, seconds, INIT_PATH)
        except CameraError:
            return None
        result = reply.get("result") if isinstance(reply.get("result"), dict) else {}
        info = result.get("encryptInfo") if isinstance(result, dict) else None
        key = info.get("rsakey") if isinstance(info, dict) else None
        if _error(reply) in (0, None) and isinstance(key, str) and key:
            return key
        if _error(reply) not in (-10003, -10004):
            return None
    return None


async def _web_state(session: Any, host: str, seconds: float) -> str | None:
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


async def vigi_state(session: Any, host: str, seconds: float = 3) -> str | None:
    """``uninitialized``, ``ready``, or None when the host is no VIGI camera.

    The web API answers first (VIGI C540V); a camera whose web API says
    nothing either way is asked for the OpenAPI initialisation key, which it
    only offers before its first password (InSight LPR345Z). The camera's
    HTTPS server drops the odd connection (and restarts it right after
    activation), so an unreachable camera is asked once more.
    """

    host = camera_host(host)
    state = await _web_state(session, host, seconds)
    if state is None and await openapi_init_key(session, host, seconds):
        return "uninitialized"
    return state


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


async def _activate_openapi(session: Any, host: str, password: str, key: str) -> int:
    """``setPassword`` on the OpenAPI initialisation; the camera's error code."""

    modulus, exponent = public_key(key)
    reply = await _call(
        session,
        host,
        {
            "method": "setPassword",
            "params": {"encrypted_password": encrypt(password, modulus, exponent)},
        },
        15,
        INIT_PATH,
    )
    return _error(reply) or 0


async def _activate_web(session: Any, host: str, password: str) -> int:
    """The camera web page's anonymous ``set_password``; the camera's error code."""

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
    code = reply.get("error_code")
    return code if isinstance(code, int) else -1


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
    web = await _web_state(session, host, 5)
    if web == "ready":
        raise CameraError(
            "camera_already_active", "攝影機已開通，請輸入開通時設定的密碼"
        )
    key = await openapi_init_key(session, host, 5)
    if web is None and not key:
        raise CameraError("camera_not_vigi", "這台攝影機不支援開通，請用原廠 App")
    code, method = -1, "openapi"
    if key:
        code = await _activate_openapi(session, host, password, key)
    if code != 0 and web == "uninitialized":
        code, method = await _activate_web(session, host, password), "web"
    if code != 0:
        raise CameraError(
            "camera_activate_failed", f"開通失敗（{code}），請到攝影機網頁開通"
        )
    clock = await set_clock(session, host, onvif_port, password, time_zone)
    return {
        "host": host,
        "username": USERNAME,
        "activated": True,
        "clock": clock,
        "method": method,
    }
