"""The platform's plate list for VIGI license plate cameras.

The camera recognises plates on its own. With an Alarm Server pointed at this
hub (``POST /api/webhook/<id>``, LAN only) it reports every plate here, and
the hub looks the plate up in the platform's list:

- an allowed plate opens the camera's barrier gate (OpenAPI
  ``openBarrierGate``) when 自動開閘 is on for that camera;
- a blocked plate is only reported;
- any other plate is logged as 未登記.

Each decision is kept (the last ``MAX_LOG``) and handed to ``fire`` so Home
Assistant automations can react.

Once a camera was set up in one tap (``vigi_web``), the camera holds a copy of
this list and opens the gate itself (``cameraGate``): faster, and it keeps
working while the hub is away. The hub then only logs, and never pulses the
relay a second time (a single-button gate would close again).
"""

from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from typing import Any

from .camera_probe import CameraError

MAX_ENTRIES = 2000
MAX_LOG = 200
# A car waiting at the gate is reported again and again.
REPEAT_SECONDS = 8
KINDS = ("allow", "block")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def normalize_plate(value: Any) -> str:
    """``abc-1234`` → ``ABC1234``: the camera reports plates without separators."""

    return re.sub(r"[^0-9A-Z]", "", str(value or "").upper())[:12]


def _day(value: Any, field: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if not _DATE.match(text):
        raise CameraError("camera_invalid_request", f"{field}格式需為 YYYY-MM-DD")
    try:
        date.fromisoformat(text)
    except ValueError:
        raise CameraError("camera_invalid_request", f"{field}不是有效日期") from None
    return text


def clean_entries(entries: Any) -> list[dict]:
    """The list as saved: one entry per plate, validated."""

    if not isinstance(entries, list):
        raise CameraError("camera_invalid_request", "車牌名單格式不正確")
    if len(entries) > MAX_ENTRIES:
        raise CameraError("camera_invalid_request", f"車牌名單最多 {MAX_ENTRIES} 筆")
    cleaned: list[dict] = []
    seen: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            raise CameraError("camera_invalid_request", "車牌名單格式不正確")
        plate = normalize_plate(item.get("plate"))
        if len(plate) < 2:
            raise CameraError("camera_invalid_request", "請輸入車牌號碼")
        if plate in seen:
            raise CameraError("camera_invalid_request", f"車牌重複：{plate}")
        seen.add(plate)
        kind = str(item.get("kind") or "")
        if kind not in KINDS:
            raise CameraError("camera_invalid_request", "請選擇白名單或黑名單")
        name = re.sub(r"\s+", " ", str(item.get("name") or "")).strip()[:40]
        start = _day(item.get("from"), "開始日期")
        until = _day(item.get("until"), "到期日")
        if start and until and until < start:
            raise CameraError("camera_invalid_request", f"{plate} 的到期日早於開始日期")
        cleaned.append(
            {
                "plate": plate,
                "kind": kind,
                "name": name or None,
                "from": start,
                "until": until,
                "enabled": item.get("enabled") is not False,
            }
        )
    return cleaned


def find_entry(entries: list[dict], plate: str, today: date) -> dict | None:
    """The enabled entry for a plate that is valid today."""

    wanted = normalize_plate(plate)
    day = today.isoformat()
    for entry in entries:
        if entry.get("plate") != wanted or not entry.get("enabled", True):
            continue
        if entry.get("from") and day < entry["from"]:
            continue
        if entry.get("until") and day > entry["until"]:
            continue
        return entry
    return None


def alarm_report(body: Any) -> dict | None:
    """An Alarm Server packet: ``{"camid", "date", "plate", ...}``.

    The pictures (``plate_image``, ``image``) are not kept: the camera's own
    records keep them, and the platform reads them from there on demand.
    """

    if not isinstance(body, dict):
        return None
    return {
        "plate": normalize_plate(body.get("plate")),
        "camid": str(body.get("camid") or "")[:80] or None,
        "date": str(body.get("date") or "")[:40] or None,
    }


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class PlateGate:
    """List, per-camera settings and decision log, kept in one HA store."""

    def __init__(
        self,
        store: Any,
        *,
        open_gate: Callable[[str], Awaitable[None]],
        fire: Callable[[dict], None] = lambda _event: None,
        today: Callable[[], date] = date.today,
        clock: Callable[[], float] | None = None,
        token: Callable[[], str] = lambda: secrets.token_hex(16),
    ) -> None:
        self.store = store
        self.open_gate = open_gate
        self.fire = fire
        self.today = today
        self.clock = clock or (lambda: asyncio.get_running_loop().time())
        self.token = token
        self.data: dict[str, Any] = {}
        self.loaded = False
        self.lock = asyncio.Lock()
        self.recent: dict[tuple[str, str], tuple[float, dict]] = {}

    async def load(self) -> None:
        if self.loaded:
            return
        stored = await self.store.async_load() or {}
        self.data = {
            "webhookId": str(stored.get("webhookId") or "") or self.token(),
            "entries": list(stored.get("entries") or []),
            "cameras": dict(stored.get("cameras") or {}),
            "log": list(stored.get("log") or [])[-MAX_LOG:],
            "contact": dict(stored.get("contact") or {}),
        }
        self.loaded = True
        if not stored.get("webhookId"):
            await self._save()

    async def _save(self) -> None:
        await self.store.async_save(self.data)

    async def webhook_id(self) -> str:
        await self.load()
        return self.data["webhookId"]

    async def snapshot(self, limit: int = 50) -> dict:
        await self.load()
        return {
            "entries": list(self.data["entries"]),
            "log": list(reversed(self.data["log"][-limit:])),
            "cameras": dict(self.data["cameras"]),
        }

    async def replace(self, entries: Any) -> list[dict]:
        cleaned = clean_entries(entries)
        await self.load()
        async with self.lock:
            self.data["entries"] = cleaned
            await self._save()
        return cleaned

    async def entries(self) -> list[dict]:
        await self.load()
        return [dict(entry) for entry in self.data["entries"]]

    async def cameras(self) -> dict[str, dict]:
        await self.load()
        return {key: dict(value) for key, value in self.data["cameras"].items()}

    async def camera_settings(self, camera: str) -> dict:
        await self.load()
        current = self.data["cameras"].get(camera) or {}
        return {
            "autoGate": current.get("autoGate") is True,
            # The camera has no read-back: this is what the platform last set.
            "trigger": current.get("trigger"),
            # Set up in one tap: the camera opens the gate and holds the list.
            "cameraGate": current.get("cameraGate") is True,
            "cameraList": current.get("cameraList") is True,
            "listSync": current.get("listSync"),
            "setupAt": current.get("setupAt"),
        }

    async def set_camera(self, camera: str, **changes: Any) -> dict:
        await self.load()
        async with self.lock:
            current = dict(self.data["cameras"].get(camera) or {})
            current.update(changes)
            self.data["cameras"][camera] = current
            await self._save()
        return await self.camera_settings(camera)

    async def forget_camera(self, camera: str) -> None:
        await self.load()
        async with self.lock:
            if self.data["cameras"].pop(camera, None) is not None:
                await self._save()

    async def last_contact(self, camera: str) -> str | None:
        await self.load()
        return self.data["contact"].get(camera)

    async def log_manual(self, camera: str, label: str) -> None:
        """A gate opened from the platform, kept with the automatic ones."""

        await self.load()
        async with self.lock:
            self._append(
                {
                    "at": _now(),
                    "camera": camera,
                    "label": label,
                    "plate": None,
                    "match": None,
                    "name": None,
                    "action": "manual",
                    "message": None,
                }
            )
            await self._save()

    def _append(self, decision: dict) -> None:
        self.data["log"] = [*self.data["log"], decision][-MAX_LOG:]

    async def report(self, camera: str | None, label: str, report: dict) -> dict | None:
        """Decide on one recognised plate; None for a test packet without one."""

        await self.load()
        source = camera or label or "unknown"
        async with self.lock:
            self.data["contact"][source] = _now()
            plate = report.get("plate") or ""
            if not plate:
                await self._save()
                return None
            now = self.clock()
            previous = self.recent.get((source, plate))
            if previous and now - previous[0] < REPEAT_SECONDS:
                return previous[1]
            self.recent = {
                key: value
                for key, value in self.recent.items()
                if now - value[0] < REPEAT_SECONDS
            }
            entry = find_entry(self.data["entries"], plate, self.today())
            settings = self.data["cameras"].get(camera or "") or {}
            decision = {
                "at": _now(),
                "camera": camera,
                "label": label,
                "plate": plate,
                "match": entry["kind"] if entry else None,
                "name": entry.get("name") if entry else None,
                "action": "none",
                "message": None,
            }
            if entry and entry["kind"] == "block":
                decision["action"] = "blocked"
            elif entry and not (camera and settings.get("autoGate") is True):
                decision["action"] = "auto_off"
            elif entry and settings.get("cameraGate") is True:
                # The camera matched its own copy of the list and opened.
                decision["action"] = "camera"
            elif entry:
                try:
                    await self.open_gate(camera)
                except CameraError as err:
                    decision.update(action="gate_failed", message=err.message)
                except Exception:  # noqa: BLE001 - a decision is logged regardless
                    decision.update(action="gate_failed", message="開閘失敗")
                else:
                    decision["action"] = "opened"
            self.recent[(source, plate)] = (now, decision)
            self._append(decision)
            await self._save()
        self.fire(dict(decision))
        return decision
