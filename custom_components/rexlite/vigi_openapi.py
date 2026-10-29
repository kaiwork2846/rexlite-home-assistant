"""TP-Link VIGI / InSight camera OpenAPI: the vendor's documented control API.

TP-Link publishes it as the "VIGI IPC Open API Document" (V1.3); the InSight
LPR345Z edition adds the license plate methods. The camera answers on HTTPS
port 20443 once the installer turns it on in the camera's web UI (設定 → 網路
設定 → OpenAPI); until then the port refuses connections. Authentication is a
two-step ``doAuth`` digest (SHA-256 of ``admin:realm:password``) that returns a
session token (``stok``) for the following requests, which go to
``https://<ip>:20443/stok=<stok>``. The camera wants a new connection for
every request.

Credentials come from the hub's Frigate configuration, like the ONVIF control
(see ``camera_control``); they are used for the request at hand and never
returned or logged. Tokens are kept per camera for a few minutes and renewed
when the camera rejects one.
"""

from __future__ import annotations

import hashlib
import json
import ssl
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import unquote

from .camera_probe import CameraError, _timeout, camera_host

OPENAPI_PORT = 20443
USERNAME = "admin"
# Plate pictures come back base64 in the reply; a scene picture is ~0.5 MB.
MAX_REPLY_BYTES = 4 * 1024 * 1024
TOKEN_SECONDS = 600
DAY_NIGHT_MODES = ("auto", "day", "night")
# What the camera's encoders take; "+" is TP-Link's smart coding.
VIDEO_CODES = ("H264", "H265", "H264+", "H265+")
OFF_MESSAGE = "攝影機 OpenAPI 未開啟（20443 埠拒絕連線）"
# Failures that mean OpenAPI is not reachable, as opposed to a refused request.
DOWN = ("camera_openapi_off", "camera_openapi_tls", "camera_unreachable")

# Appendix 1 of the OpenAPI document.
_ERRORS: dict[int, tuple[str, str]] = {
    -10003: ("camera_not_supported", "攝影機不支援此功能，請更新攝影機韌體"),
    -10030: ("camera_not_supported", "攝影機不支援此功能，請更新攝影機韌體"),
    -10006: ("camera_invalid_request", "攝影機不接受此設定"),
    -10009: ("camera_invalid_request", "攝影機不接受此設定"),
    -10010: ("camera_invalid_request", "攝影機不接受此設定"),
    -10011: ("camera_busy", "攝影機查詢人數已滿，請稍後再試"),
    -10013: ("camera_no_storage", "攝影機沒有插 microSD 卡，無法保存車牌紀錄"),
    -10014: ("camera_search_failed", "攝影機查詢紀錄失敗，請稍後再試"),
    -10015: ("camera_busy", "攝影機忙碌中，請再試一次"),
    -10020: ("camera_auth_failed", "攝影機密碼已變更，請移除後重新加入這台攝影機"),
    -10021: ("camera_busy", "攝影機連線數已滿，請稍後再試"),
    -10022: ("camera_locked", "攝影機登入失敗次數過多，已暫時鎖定，請稍後再試"),
}
# A rejected token (expired, or the camera restarted): log in again once.
_STALE_TOKEN = (-10002, -10020)


def digest(
    password: str,
    realm: str,
    nonce: str,
    method: str,
    uri: str,
    algorithm: str = "SHA-256",
) -> str:
    """The ``doAuth`` response: ``H(H(admin:realm:password):nonce:H(method:uri))``."""

    name = algorithm.upper().replace("_", "-")
    hash_ = hashlib.md5 if name == "MD5" else hashlib.sha256

    def h(text: str) -> str:
        return hash_(text.encode()).hexdigest()

    return h(f"{h(f'{USERNAME}:{realm}:{password}')}:{nonce}:{h(f'{method}:{uri}')}")


def error_code(reply: dict) -> int:
    """``errCode`` (or ``error_code``, which some methods nest in ``result``)."""

    for source in (reply, reply.get("result")):
        if isinstance(source, dict):
            for key in ("errCode", "error_code"):
                value = source.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    return value
                if isinstance(value, str) and value.lstrip("-").isdigit():
                    return int(value)
    return 0


def _tls_failure(err: BaseException) -> bool:
    """A TLS handshake that failed (as opposed to a closed port)."""

    for item in (err, getattr(err, "os_error", None), err.__cause__):
        if isinstance(item, ssl.SSLError) or "SSL" in type(item).__name__:
            return True
    return False


def _reason(err: BaseException) -> str:
    text = str(getattr(err, "os_error", None) or err).strip()
    return (text or type(err).__name__)[:120]


def openapi_error(code: int) -> CameraError:
    known = _ERRORS.get(code)
    if known:
        return CameraError(*known)
    return CameraError("camera_openapi_failed", f"攝影機回應錯誤（{code}）")


def _on(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return True if text in ("on", "1", "true") else False if text else None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def plate_record(item: dict) -> dict | None:
    """One ``searchLprInfo`` row; the camera nests each one under its own key."""

    if "license_plate_number" not in item and len(item) == 1:
        inner = next(iter(item.values()))
        item = inner if isinstance(inner, dict) else {}
    plate = str(item.get("license_plate_number") or "").strip()
    timestamp = _int(item.get("snapshot_timestamp"))
    if not plate or timestamp is None:
        return None
    list_type = _int(item.get("list_type"))
    return {
        "time": timestamp,
        "plate": plate[:20],
        # Doc: 1 block list, 2 allow list, others temporary; unlisted plates
        # come back as 0 in practice.
        "cameraList": {1: "block", 2: "allow", 0: None}.get(list_type, "temporary"),
        "eventType": _int(item.get("event_type")),
        "thumbId": str(item.get("thumbnail_file_id") or "")[:64] or None,
        "thumbSize": _int(item.get("thumbnail_size")),
        "backgroundId": str(item.get("background_file_id") or "")[:64] or None,
        "backgroundSize": _int(item.get("background_size")),
        "device": unquote(str(item.get("device_name") or ""))[:80] or None,
    }


class VigiOpenApi:
    """One camera's OpenAPI; ``tokens`` is shared by every request on the hub."""

    def __init__(
        self,
        session: Any,
        host: str,
        password: str,
        *,
        port: int = OPENAPI_PORT,
        seconds: float = 8,
        tokens: dict[str, tuple[str, float]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session = session
        self.host = camera_host(host)
        self.password = password
        self.port = port
        self.seconds = seconds
        self.tokens = tokens if tokens is not None else {}
        self.clock = clock
        # A token belongs to this password too: a re-added camera logs in anew.
        secret = hashlib.sha256(password.encode()).hexdigest()[:16]
        self.key = f"{self.host}:{port}:{secret}"

    async def _post(self, path: str, body: dict, seconds: float | None) -> dict:
        try:
            async with self.session.request(
                "POST",
                f"https://{self.host}:{self.port}/{path}",
                data=json.dumps(body),
                headers={"Content-Type": "application/json", "Connection": "close"},
                ssl=False,
                timeout=_timeout(seconds or self.seconds),
            ) as response:
                data = (await response.read())[:MAX_REPLY_BYTES]
        except CameraError:
            raise
        except TimeoutError as err:
            raise CameraError(
                "camera_unreachable", "攝影機沒有回應，請確認攝影機在線"
            ) from err
        except Exception as err:  # noqa: BLE001 - a refused port means OpenAPI is off
            if _tls_failure(err):
                raise CameraError(
                    "camera_openapi_tls",
                    f"攝影機 OpenAPI 加密連線失敗（{_reason(err)}）",
                ) from err
            raise CameraError("camera_openapi_off", OFF_MESSAGE) from err
        try:
            reply = json.loads(data)
        except ValueError:
            reply = None
        if not isinstance(reply, dict):
            raise CameraError("camera_openapi_failed", "攝影機 OpenAPI 回應格式不正確")
        return reply

    async def _login(self) -> str:
        first = await self._post("", {"method": "doAuth", "params": None}, None)
        challenge = first.get("authenticate")
        if not isinstance(challenge, dict) or not challenge.get("nonce"):
            raise openapi_error(error_code(first) or -10020)
        nonce = str(challenge["nonce"])
        response = digest(
            self.password,
            str(challenge.get("realm") or ""),
            nonce,
            str(challenge.get("method") or "POST"),
            str(challenge.get("uri") or "doAuth"),
            str(challenge.get("algorithm") or "SHA-256"),
        )
        second = await self._post(
            "",
            {"method": "doAuth", "params": {"nonce": nonce, "response": response}},
            None,
        )
        stok = second.get("stok")
        if not isinstance(stok, str) or not stok:
            raise openapi_error(error_code(second) or -10020)
        self.tokens[self.key] = (stok, self.clock())
        return stok

    async def _token(self, fresh: bool) -> str:
        cached = self.tokens.get(self.key)
        if cached and not fresh and self.clock() - cached[1] < TOKEN_SECONDS:
            return cached[0]
        self.tokens.pop(self.key, None)
        return await self._login()

    async def call(
        self, method: str, params: dict | None = None, *, seconds: float | None = None
    ) -> dict:
        """``result`` of one method; a stale token is renewed once."""

        body: dict[str, Any] = {"method": method}
        if params is not None:
            body["params"] = params
        for attempt in range(2):
            stok = await self._token(fresh=attempt > 0)
            reply = await self._post(f"stok={stok}", body, seconds)
            code = error_code(reply)
            if code in _STALE_TOKEN and attempt == 0:
                continue
            if code:
                raise openapi_error(code)
            result = reply.get("result")
            return result if isinstance(result, dict) else {}
        raise openapi_error(-10020)

    # System -----------------------------------------------------------------

    async def device(self) -> dict:
        result = await self.call("getDeviceInfo")
        info = (
            result.get("deviceInfo")
            if isinstance(result.get("deviceInfo"), dict)
            else result
        )
        return {
            "model": str(info.get("model") or "")[:60] or None,
            "alias": unquote(str(info.get("alias") or ""))[:80] or None,
            "firmware": str(info.get("swVer") or "")[:80] or None,
            "hardware": str(info.get("hwVer") or "")[:20] or None,
            "mac": str(info.get("mac") or "")[:20] or None,
        }

    async def modules(self) -> list[str]:
        result = await self.call("getModuleList")
        found = result.get("moduleList")
        return [
            str(m.get("name"))[:40]
            for m in (found if isinstance(found, list) else [])
            if isinstance(m, dict) and m.get("name")
        ]

    # Picture ----------------------------------------------------------------

    async def day_night(self) -> str | None:
        mode = str((await self.call("getDayNightMode")).get("mode") or "")
        return mode if mode in DAY_NIGHT_MODES else None

    async def set_day_night(self, mode: str) -> None:
        if mode not in DAY_NIGHT_MODES:
            raise CameraError("camera_invalid_request", "日夜模式不正確")
        await self.call("setDayNightMode", {"mode": mode})

    async def video_code(self) -> dict:
        result = await self.call("getVideoCode")
        return {
            "main": str(result.get("main_encode_type") or "")[:10] or None,
            "sub": str(result.get("minor_encode_type") or "")[:10] or None,
        }

    async def set_video_code(self, main: str | None, sub: str | None) -> None:
        params: dict[str, str] = {}
        for key, value in (("main_encode_type", main), ("minor_encode_type", sub)):
            if value is None:
                continue
            if value not in VIDEO_CODES:
                raise CameraError("camera_invalid_request", "編碼格式不正確")
            params[key] = value
        if not params:
            raise CameraError("camera_invalid_request", "沒有要變更的設定")
        await self.call("setVideoCode", params)

    # Storage ----------------------------------------------------------------

    async def sd_card(self) -> dict:
        result = await self.call("getSdCardStatus")
        card = (
            result.get("sdCardInfo")
            if isinstance(result.get("sdCardInfo"), dict)
            else result
        )
        if isinstance(card, list):
            card = card[0] if card and isinstance(card[0], dict) else {}
        return {
            "status": str(card.get("status") or "")[:20] or None,
            "total": str(card.get("total_space") or "")[:20] or None,
            "free": str(card.get("free_space") or "")[:20] or None,
            "loop": _on(card.get("loop_record_status")),
        }

    # License plates (InSight LPR models) ------------------------------------

    async def open_gate(self) -> None:
        """Pulse the camera's relay output that drives the barrier gate."""

        await self.call("openBarrierGate")

    async def set_lpr_trigger(self, video: bool, alarm: bool) -> None:
        """Recognise on the picture, on the alarm input (a ground loop), or both.

        The camera has no read-back for this setting.
        """

        await self.call(
            "lprTriggerMode",
            {
                "video_trigger": "on" if video else "off",
                "alarm_trigger": "on" if alarm else "off",
            },
        )

    async def plate_records(
        self, start: int, end: int, page: int, per_page: int, plate: str = ""
    ) -> dict:
        user = await self.call("getUserId")
        user_id = _int(user.get("user_id"))
        if user_id is None:
            raise CameraError("camera_openapi_failed", "攝影機沒有回傳查詢編號")
        result = await self.call(
            "searchLprInfo",
            {
                "start_time": int(start),
                "end_time": int(end),
                "user_id": user_id,
                "item_per_page": int(per_page),
                "page_num": int(page),
                "license_plate_number": plate,
            },
            seconds=15,
        )
        rows = result.get("license_plate_result")
        records = [
            record
            for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, dict)
            for record in [plate_record(row)]
            if record
        ]
        return {
            "total": _int(result.get("total")) or 0,
            "page": _int(result.get("page")) or int(page),
            "records": records,
        }

    async def plate_images(
        self, timestamp: int, thumb_id: str | None, background_id: str | None
    ) -> dict:
        result = await self.call(
            "downloadLprImage",
            {
                "timestamp": int(timestamp),
                "thumbnail_file_id": thumb_id or "",
                "background_file_id": background_id or "",
            },
            seconds=20,
        )
        images = result.get("license_plate_image_result")
        images = images if isinstance(images, dict) else {}
        return {
            "thumbnail": str(images.get("thumbnail_image") or "") or None,
            "background": str(images.get("background_image") or "") or None,
        }
