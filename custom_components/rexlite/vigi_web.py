"""TP-Link VIGI / InSight camera web API: the JSON API of the camera's own web page.

TP-Link documents OpenAPI (``vigi_openapi``) but not the settings only the
camera's web page has: the OpenAPI switch itself, plate recognition, the Alarm
Server, the camera's own allow / block list and what it does with a matched
plate. The page is a plain JSON client, so the hub sets a camera up the same
way, with the password it remembers, in one tap.

Requests and replies come from TP-Link's public web emulator of the InSight
LPR345Z (firmware 1.0.1 Build 251118, emulator.tp-link.com), which ships the
camera's real page with canned replies; the login matches what the VIGICam
project recorded from live VIGI C540S / C540V and InSight S245 cameras:

* ``{"method":"do","user_management":{"get_encrypt_info":null}}`` on
  ``https://<ip>/`` hands out RSA public keys and a nonce (``error_code``
  -40401 by design);
* the password travels as ``RSA-PKCS1v1.5(MD5("TPCQ75NF2Y:" + password) +
  ":" + nonce)`` in ``{"method":"do","login":{...}}``, which answers ``stok``;
* every other request is ``POST https://<ip>/stok=<stok>/ds`` with
  ``{"method": "get" | "set" | "do" | "add" | "delete", <module>: {...}}``.

Values come back URL-encoded. Not yet run against a real LPR345Z: every
setup step reports on its own, so a camera that answers differently is
finished by hand on its web page.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date, datetime
from datetime import time as day_time
from typing import Any
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo

from .camera_probe import CameraError, _timeout, camera_host
from .lpr_gate import normalize_plate
from .vigi_setup import UNINITIALIZED, USERNAME, encrypt, public_key

WEB_PORT = 443
# Fixed salt of the camera's login (``Tool.md5AuthPwd`` in its page).
PASSWORD_SALT = "TPCQ75NF2Y:"
MAX_REPLY_BYTES = 512 * 1024
LOCKED = -40404
STALE = -40401
# ``list_type`` on the camera's allow / block list (0 is its temporary list).
LIST_TYPES = {"allow": 2, "block": 1}
# The page accepts validity dates in 2000–2037.
FIRST_DAY = date(2000, 1, 1)
LAST_DAY = date(2037, 12, 31)
MAX_NOTES = 16
PAGE_SIZE = 100
MAX_PAGES = 100
DELETE_BATCH = 50

_ERRORS: dict[int, tuple[str, str]] = {
    -40105: ("camera_not_supported", "攝影機不支援此設定，請更新攝影機韌體"),
    -40106: ("camera_not_supported", "攝影機不支援此設定，請更新攝影機韌體"),
    -40107: ("camera_auth_failed", "攝影機帳號權限已變更，請重新登入"),
    STALE: ("camera_auth_failed", "攝影機登入已失效，請再試一次"),
    LOCKED: ("camera_locked", "攝影機登入失敗次數過多，已暫時鎖定，請稍後再試"),
}


def web_error(code: int) -> CameraError:
    known = _ERRORS.get(code)
    if known:
        return CameraError(*known)
    return CameraError("camera_web_failed", f"攝影機回應錯誤（{code}）")


def _code(reply: dict) -> int:
    value = reply.get("error_code")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return 0


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    return unquote(str(value or ""))


def _row(item: Any, key: str) -> dict:
    """``{"whitelist_0": {...}}`` → ``{...}``: the camera keys each table row."""

    if isinstance(item, dict) and key not in item and len(item) == 1:
        inner = next(iter(item.values()))
        return inner if isinstance(inner, dict) else {}
    return item if isinstance(item, dict) else {}


def zone_of(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "Asia/Taipei")
    except (ValueError, KeyError, OSError):
        return ZoneInfo("Asia/Taipei")


def day_start(day: str | None, zone: ZoneInfo) -> int:
    value = max(date.fromisoformat(day), FIRST_DAY) if day else FIRST_DAY
    return int(datetime.combine(value, day_time.min, zone).timestamp())


def day_end(day: str | None, zone: ZoneInfo) -> int:
    value = min(date.fromisoformat(day), LAST_DAY) if day else LAST_DAY
    return int(datetime.combine(value, day_time(23, 59, 59), zone).timestamp())


def _day_of(seconds: int | None, zone: ZoneInfo, *, end: bool) -> str | None:
    """A camera validity bound as a platform date; the page's limits mean none."""

    if seconds is None:
        return None
    day = datetime.fromtimestamp(seconds, zone).date()
    if (end and day >= LAST_DAY) or (not end and day <= FIRST_DAY):
        return None
    return day.isoformat()


def camera_plate(item: Any) -> dict | None:
    """One row of the camera's allow / block list (its temporary list is skipped)."""

    row = _row(item, "license_plate")
    raw = _text(row.get("license_plate")).strip()
    kind = {value: key for key, value in LIST_TYPES.items()}.get(
        _int(row.get("list_type")) or 0
    )
    if not raw or kind is None:
        return None
    return {
        "raw": raw,
        "plate": normalize_plate(raw),
        "kind": kind,
        "enabled": str(row.get("enabled") or "on").lower() != "off",
        "start": _int(_text(row.get("start_time"))),
        "end": _int(_text(row.get("end_time"))),
        "notes": _text(row.get("notes")).strip(),
    }


def wanted_row(entry: dict, zone: ZoneInfo) -> dict:
    """A platform list entry as the camera keeps it."""

    return {
        "plate": entry["plate"],
        "kind": entry["kind"],
        "enabled": entry.get("enabled", True) is not False,
        "start": day_start(entry.get("from"), zone),
        "end": day_end(entry.get("until"), zone),
        "notes": str(entry.get("name") or "")[:MAX_NOTES],
    }


def plan_sync(
    entries: list[dict], rows: list[dict], zone: ZoneInfo
) -> tuple[list[dict], list[tuple[str, dict]], list[str]]:
    """Adds, updates (old plate, new row) and removals that make the camera's
    list equal the platform's."""

    wanted = {entry["plate"]: wanted_row(entry, zone) for entry in entries}
    adds: list[dict] = []
    updates: list[tuple[str, dict]] = []
    removals: list[str] = []
    kept: set[str] = set()
    for row in rows:
        target = wanted.get(row["plate"])
        if target is None or row["plate"] in kept:
            removals.append(row["raw"])
            continue
        kept.add(row["plate"])
        same = all(
            row[key] == target[key]
            for key in ("kind", "enabled", "start", "end", "notes")
        )
        if not same or row["raw"] != row["plate"]:
            updates.append((row["raw"], target))
    adds = [target for plate, target in wanted.items() if plate not in kept]
    return adds, updates, removals


def merge_rows(entries: list[dict], rows: list[dict], zone: ZoneInfo) -> list[dict]:
    """The platform's list plus the camera's plates it lacks (first setup only,
    so nothing an installer typed on the camera is lost)."""

    have = {entry["plate"] for entry in entries}
    extra: list[dict] = []
    for row in rows:
        if len(row["plate"]) < 2 or row["plate"] in have:
            continue
        have.add(row["plate"])
        start = _day_of(row["start"], zone, end=False)
        until = _day_of(row["end"], zone, end=True)
        extra.append(
            {
                "plate": row["plate"],
                "kind": row["kind"],
                "name": row["notes"] or None,
                "from": start,
                "until": None if start and until and until < start else until,
                "enabled": row["enabled"],
            }
        )
    return [*entries, *extra]


def _list_body(row: dict) -> dict:
    """What the camera's page sends for a list row (values URL-encoded)."""

    return {
        "enabled": "on" if row["enabled"] else "off",
        "license_plate": quote(row["plate"], safe=""),
        "start_time": str(row["start"]),
        "end_time": str(row["end"]),
        "notes": quote(row["notes"], safe=""),
        "list_type": LIST_TYPES[row["kind"]],
    }


class VigiWeb:
    """One camera's web API session (``admin``, the password the hub knows)."""

    def __init__(
        self,
        session: Any,
        host: str,
        password: str,
        *,
        port: int = WEB_PORT,
        seconds: float = 8,
    ) -> None:
        self.session = session
        self.host = camera_host(host)
        self.password = password
        self.port = port
        self.seconds = seconds
        self.stok: str | None = None

    def _url(self, path: str) -> str:
        port = "" if self.port == WEB_PORT else f":{self.port}"
        return f"https://{self.host}{port}/{path}"

    async def _post(self, path: str, body: dict, seconds: float | None = None) -> dict:
        # The camera's HTTPS server drops the odd connection: ask once more.
        for attempt in range(2):
            try:
                async with self.session.request(
                    "POST",
                    self._url(path),
                    data=json.dumps(body),
                    headers={"Content-Type": "application/json"},
                    ssl=False,
                    timeout=_timeout(seconds or self.seconds),
                ) as response:
                    data = (await response.read())[:MAX_REPLY_BYTES]
                break
            except CameraError:
                raise
            except Exception as err:  # noqa: BLE001 - every network failure is one code
                if attempt:
                    raise CameraError(
                        "camera_unreachable", "攝影機沒有回應，請確認攝影機在線"
                    ) from err
                await asyncio.sleep(1)
        try:
            reply = json.loads(data)
        except ValueError:
            reply = None
        if not isinstance(reply, dict):
            raise CameraError(
                "camera_web_unsupported", "這台攝影機的網頁介面不支援自動設定"
            )
        return reply

    async def login(self) -> None:
        offer = await self._post(
            "", {"method": "do", "user_management": {"get_encrypt_info": None}}
        )
        data = offer.get("data") if isinstance(offer.get("data"), dict) else {}
        if data.get("code") == UNINITIALIZED:
            raise CameraError("camera_uninitialized", "攝影機尚未開通")
        if data.get("code") == LOCKED:
            raise web_error(LOCKED)
        key = str(data.get("key_2") or data.get("key") or "")
        nonce = str(data.get("nonce") or "")
        if not key or not nonce:
            raise CameraError(
                "camera_web_unsupported", "這台攝影機的網頁介面不支援自動設定"
            )
        if str(data.get("passwdType") or "md5") != "md5":
            raise CameraError(
                "camera_web_unsupported", "攝影機韌體過舊，請先更新攝影機韌體"
            )
        offered = [str(value) for value in data.get("encrypt_type") or []]
        kind = str(
            data.get("current_encrypt_type")
            or max(offered, key=lambda value: _int(value) or 0, default="1")
        )
        secret = hashlib.md5(f"{PASSWORD_SALT}{self.password}".encode()).hexdigest()
        secret = secret.upper()
        if kind == "2":
            modulus, exponent = public_key(key)
            password = encrypt(f"{secret}:{nonce}", modulus, exponent)
        else:
            password = secret
        request: dict[str, Any] = {
            "encrypt_type": kind,
            "passwdType": "md5",
            "password": password,
            "username": USERNAME,
        }
        if data.get("key_2"):
            request["keyType"] = "1"
        reply = await self._post("", {"method": "do", "login": request})
        stok = reply.get("stok")
        if isinstance(stok, str) and stok:
            self.stok = unquote(stok)
            return
        failed = reply.get("data") if isinstance(reply.get("data"), dict) else {}
        if failed.get("code") == LOCKED or _code(reply) == LOCKED:
            raise web_error(LOCKED)
        # ``time`` is what the page shows as attempts left before the lock-out.
        left = _int(failed.get("time"))
        hint = f"（剩 {left} 次機會）" if left else ""
        raise CameraError("camera_auth_failed", f"攝影機密碼不正確{hint}")

    async def request(
        self, method: str, body: dict, *, seconds: float | None = None
    ) -> dict:
        """One ``ds`` request; an expired ``stok`` logs in again once."""

        for attempt in range(2):
            if not self.stok:
                await self.login()
            reply = await self._post(
                f"stok={self.stok}/ds", {"method": method, **body}, seconds
            )
            code = _code(reply)
            if code == STALE and attempt == 0:
                self.stok = None
                continue
            if code:
                raise web_error(code)
            return reply
        raise web_error(STALE)

    # Device -------------------------------------------------------------------

    async def device(self) -> dict:
        reply = await self.request("get", {"device_info": {"name": "basic_info"}})
        info = (reply.get("device_info") or {}).get("basic_info") or {}
        return {
            "model": _text(info.get("device_model"))[:60] or None,
            "firmware": _text(info.get("sw_version"))[:80] or None,
            "mac": _text(info.get("mac"))[:20] or None,
        }

    # OpenAPI ------------------------------------------------------------------

    async def openapi(self) -> dict:
        reply = await self.request("get", {"openapi": {"name": ["server"]}})
        server = (reply.get("openapi") or {}).get("server") or {}
        return {
            "enabled": str(server.get("enabled") or "").lower() == "on",
            "port": _int(server.get("openapi_port")) or 20443,
        }

    async def enable_openapi(self) -> bool:
        """Switch OpenAPI on; True when it was off."""

        current = await self.openapi()
        if current["enabled"]:
            return False
        await self.request(
            "set",
            {
                "openapi": {
                    "server": {"enabled": "on", "openapi_port": str(current["port"])}
                }
            },
        )
        return True

    # Plate recognition ----------------------------------------------------------

    async def enable_plates(self) -> bool:
        """Recognise plates on the picture (the page's 影像觸發); True when changed."""

        reply = await self.request(
            "get", {"license_plate_recognition": {"name": ["lpr_para", "lpss_rule"]}}
        )
        section = reply.get("license_plate_recognition") or {}
        para = section.get("lpr_para") or {}
        rule = section.get("lpss_rule") or {}
        changed = False
        if str(para.get("reg_video") or "").lower() != "on":
            # The page sends these three; the others are read-only.
            kept = {
                key: para[key]
                for key in ("same_tri_interval", "reg_no_plate")
                if key in para
            }
            await self.request(
                "set",
                {
                    "license_plate_recognition": {
                        "lpr_para": {**kept, "reg_video": "on"}
                    }
                },
            )
            changed = True
        if rule and str(rule.get("enabled") or "").lower() == "off":
            await self.request(
                "set",
                {"license_plate_recognition": {"lpss_rule": {**rule, "enabled": "on"}}},
            )
            changed = True
        return changed

    # Alarm Server (where the camera posts each plate) ---------------------------

    async def alarm_servers(self) -> list[dict]:
        reply = await self.request(
            "get", {"report_event": {"table": ["report_server_list"]}}
        )
        rows = (reply.get("report_event") or {}).get("report_server_list") or []
        servers = []
        for item in rows if isinstance(rows, list) else []:
            if not isinstance(item, dict) or len(item) != 1:
                continue
            key, row = next(iter(item.items()))
            if isinstance(row, dict):
                servers.append(
                    {
                        "id": str(key),
                        "host": _text(row.get("ip_or_domain")),
                        "port": _int(row.get("port")),
                        "url": _text(row.get("url")),
                        "protocol": str(row.get("protocol") or "HTTP"),
                        "picture": str(row.get("push_picture_enabled") or "").lower()
                        == "on",
                    }
                )
        return servers

    async def point_alarm_server(self, host: str, port: int, path: str) -> str:
        """Make one of the camera's (up to 3) Alarm Servers this hub's webhook.

        ``kept`` when one already is, ``updated`` when an entry for this hub had
        another path, ``added`` otherwise. Pictures stay off: the hub keeps no
        pictures, the camera's records do.
        """

        servers = await self.alarm_servers()
        para = {
            "ip_or_domain": host,
            "port": str(port),
            "url": path,
            "protocol": "HTTP",
            "push_picture_enabled": "off",
        }
        for server in servers:
            if (server["host"], server["port"], server["url"]) == (host, port, path):
                return "kept"
        for server in servers:
            if server["host"] == host and server["port"] == port:
                await self.request("set", {"report_event": {server["id"]: para}})
                return "updated"
        taken = {server["id"] for server in servers}
        free = next(
            (
                f"report_server_list_{n}"
                for n in (1, 2, 3)
                if f"report_server_list_{n}" not in taken
            ),
            None,
        )
        if free is None:
            raise CameraError(
                "camera_alarm_server_full",
                "攝影機的警報伺服器已設滿 3 組，請先在攝影機網頁刪除一組",
            )
        await self.request(
            "add",
            {
                "report_event": {
                    "name": free,
                    "table": "report_server_list",
                    "para": para,
                }
            },
        )
        return "added"

    # Barrier gate ---------------------------------------------------------------

    async def set_gate(self, allowed: bool) -> None:
        """What the camera does with its relay output after a recognition.

        ``allowed``: an allowed plate opens the gate and nothing else does.
        Rows are keyed by trigger (0 plate recognition, 1 alarm input) and
        verdict (1 allowed, 2 blocked, 3 temporary, 4 no plate).
        """

        reply = await self.request("get", {"lpr": {"table": ["device_linkage_info"]}})
        found = (reply.get("lpr") or {}).get("device_linkage_info") or []
        rows: dict[str, dict] = {}
        changed = False
        for item in found if isinstance(found, list) else []:
            if not isinstance(item, dict) or len(item) != 1:
                continue
            key, row = next(iter(item.items()))
            if not isinstance(row, dict):
                continue
            allow_row = (
                _int(row.get("lpr_trigger_type")) == 0
                and _int(row.get("lp_verify_type")) == 1
            )
            # Switching off only touches the allowed row; switching on also
            # stops every other verdict from pulsing the gate's relay.
            if allowed:
                wanted = "on" if allow_row else "off"
            else:
                wanted = "off" if allow_row else None
            if wanted and str(row.get("alarm_out_enabled")) != wanted:
                row = {**row, "alarm_out_enabled": wanted}
                changed = True
            rows[str(key)] = row
        if not rows:
            raise CameraError("camera_not_supported", "攝影機沒有開閘連動設定")
        if changed:
            # Like the camera's page: every row in one request.
            await self.request("set", {"lpr": rows})

    # Allow / block list ---------------------------------------------------------

    async def plates(self) -> list[dict]:
        rows: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            reply = await self.request(
                "do",
                {
                    "lpr": {
                        "get_whitelist": {
                            "page_num": str(page),
                            "item_per_page": str(PAGE_SIZE),
                            "license_plate": "",
                            "whitelist_filter": [1, 2],
                        }
                    }
                },
            )
            items = (
                reply.get("whitelist")
                if isinstance(reply.get("whitelist"), list)
                else []
            )
            rows += [row for item in items for row in [camera_plate(item)] if row]
            if not items or page * PAGE_SIZE >= (_int(reply.get("total")) or 0):
                break
        return rows

    async def apply_plates(
        self,
        adds: list[dict],
        updates: list[tuple[str, dict]],
        removals: list[str],
    ) -> None:
        for start in range(0, len(removals), DELETE_BATCH):
            await self.request(
                "do",
                {
                    "lpr": {
                        "del_whitelist": {
                            "license_plate": removals[start : start + DELETE_BATCH]
                        }
                    }
                },
            )
        for old, row in updates:
            await self.request(
                "do",
                {
                    "lpr": {
                        "mod_whitelist": {
                            **_list_body(row),
                            "old_license_plate": quote(old, safe=""),
                        }
                    }
                },
            )
        for row in adds:
            await self.request("do", {"lpr": {"add_whitelist": _list_body(row)}})
