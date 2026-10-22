"""Resilient REXLiTE tunnel runtime."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import ssl
from collections.abc import Callable, Coroutine, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import urlsplit

import aiohttp

from .const import (
    ACCESS_MODE_FULL_CONTROL,
    ACCESS_MODE_HEALTH_ONLY,
    INTEGRATION_VERSION,
)
from .network import IPCLanAddress, detect_ipc_lan_ipv4
from .protocol import (
    TYPE_ERROR,
    TYPE_HEARTBEAT,
    TYPE_HEARTBEAT_ACK,
    TYPE_HELLO,
    TYPE_OTA_MANIFEST,
    TYPE_PROXY_REQUEST,
    TYPE_PROXY_RESPONSE,
    TYPE_STREAM_ACCEPT,
    TYPE_STREAM_CLOSE,
    TYPE_STREAM_DATA,
    TYPE_STREAM_OPEN,
    Envelope,
    HttpResponseBodyPlan,
    ProtocolError,
    agent_websocket_url,
    decode_bytes,
    encode_bytes,
    http_response_body_plan,
    local_request_url,
    make_envelope,
    parse_envelope,
)

_LOGGER = logging.getLogger(__name__)

_REMOTE_ADMIN_DISABLED: Final = "remote administration is disabled"
_SELF_RELOAD_RESPONSE: Final = b'{"require_restart": false}'
_HOP_BY_HOP_HEADERS: Final = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_MAX_HEADER_BYTES: Final = 64 * 1024
_MAX_STREAMS: Final = 32
_HTTP_METHOD_PATTERN: Final = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_HTTP_HEADER_PATTERN: Final = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


@dataclass(frozen=True, slots=True)
class TunnelConfig:
    """Validated runtime configuration."""

    agent_id: str
    auth_token: str
    gateway_url: str
    home_assistant_url: str
    home_assistant_version: str
    remote_admin_enabled: bool = False
    entry_id: str = ""
    heartbeat_interval: float = 25.0
    request_timeout: float = 60.0
    reconnect_delay: float = 2.0
    reconnect_max_delay: float = 60.0
    max_body_bytes: int = 64 * 1024 * 1024
    max_message_bytes: int = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class RuntimeState:
    """Operator-visible runtime state."""

    connected: bool = False
    remote_admin_enabled: bool = False
    reconnect_attempt: int = 0
    last_connected_at: str | None = None
    last_error: str | None = None
    authentication_failed: bool = False

    @property
    def access_mode(self) -> str:
        """Return the backend-compatible access mode."""

        return (
            ACCESS_MODE_FULL_CONTROL
            if self.remote_admin_enabled
            else ACCESS_MODE_HEALTH_ONLY
        )

    @property
    def cloud_service_state(self) -> str:
        """Return the customer-visible service lifecycle state."""

        if self.authentication_failed:
            return "verification_required"
        if not self.connected:
            return "disconnected"
        if not self.remote_admin_enabled:
            return "ready_for_activation"
        return "connected"


def _create_loop_task(
    coroutine: Coroutine[Any, Any, Any], name: str
) -> asyncio.Task[Any]:
    return asyncio.get_running_loop().create_task(coroutine, name=name)


def _consume_detached_result(task: asyncio.Task[Any]) -> None:
    """Retrieve a result nobody may await once its tunnel session ended."""

    if not task.cancelled() and (error := task.exception()) is not None:
        _LOGGER.debug("REXLiTE detached work ended: %s", type(error).__name__)


@dataclass(slots=True)
class _LocalStream:
    """One raw HTTP/WebSocket stream to Home Assistant."""

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    reader_task: asyncio.Task[None] | None = None


class REXLiTETunnelClient:
    """Maintain the authenticated, outbound-only REXLiTE gateway tunnel."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        config: TunnelConfig,
        state_callback: Callable[[RuntimeState], None],
        task_factory: Callable[[Coroutine[Any, Any, Any], str], asyncio.Task[Any]],
        auth_failure_callback: Callable[[], None] | None = None,
        ipc_ip_detector: Callable[[str], IPCLanAddress | None] | None = None,
        detached_task_factory: (
            Callable[[Coroutine[Any, Any, Any], str], asyncio.Task[Any]] | None
        ) = None,
        self_reload_callback: Callable[[], None] | None = None,
    ) -> None:
        self._session = session
        self._config = config
        self._state_callback = state_callback
        self._task_factory = task_factory
        # Work that must outlive this config entry's tasks, e.g. a Home
        # Assistant request whose tunnel session ended mid-flight.
        self._detached_task_factory = detached_task_factory or _create_loop_task
        self._self_reload_callback = self_reload_callback
        self._auth_failure_callback = auth_failure_callback
        self._ipc_ip_detector = ipc_ip_detector or detect_ipc_lan_ipv4
        self._state = RuntimeState(remote_admin_enabled=config.remote_admin_enabled)
        self._main_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._send_lock = asyncio.Lock()
        self._access_mode_lock = asyncio.Lock()
        self._request_semaphore = asyncio.Semaphore(8)
        self._handler_tasks: set[asyncio.Task[Any]] = set()
        self._streams: dict[str, _LocalStream] = {}
        self._stopping = False
        self._closed = False
        self._connected_since: float | None = None
        self._last_connection_duration = 0.0
        self._last_ip_signature: tuple[str, str, str] | None = None
        self._auth_failure_reported = False

    @property
    def state(self) -> RuntimeState:
        """Return the latest immutable runtime state."""

        return self._state

    async def async_start(self) -> None:
        """Start the tunnel supervisor once."""

        if self._closed:
            return
        if self._main_task is not None and not self._main_task.done():
            return
        self._stopping = False
        self._main_task = self._task_factory(
            self._run_forever(), f"REXLiTE tunnel {self._config.agent_id}"
        )

    async def async_stop(self) -> None:
        """Stop the tunnel for good because the config entry is unloading."""

        self._closed = True
        await self._async_stop_session()

    async def _async_stop_session(self) -> None:
        """Stop all background work and close active streams."""

        self._stopping = True
        tasks = [task for task in self._handler_tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            tasks.append(self._heartbeat_task)
        if self._main_task is not None and not self._main_task.done():
            self._main_task.cancel()
            tasks.append(self._main_task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if self._ws is not None and not self._ws.closed:
            await self._ws.close(code=aiohttp.WSCloseCode.GOING_AWAY)
        await self._close_all_streams()

        self._handler_tasks.clear()
        self._main_task = None
        self._heartbeat_task = None
        self._ws = None
        self._set_state(connected=False)

    async def async_set_remote_admin(self, enabled: bool) -> None:
        """Apply the remote-control gate using a clean gateway session."""

        # A toggle that arrived through this tunnel loses its caller as soon as
        # the old session closes, so the restart runs in its own task and can
        # never leave the tunnel stopped halfway.
        await asyncio.shield(
            self._spawn_detached(
                self._apply_remote_admin(enabled), "REXLiTE remote access mode"
            )
        )

    async def _apply_remote_admin(self, enabled: bool) -> None:
        async with self._access_mode_lock:
            if self._config.remote_admin_enabled == enabled:
                return

            self._config = replace(self._config, remote_admin_enabled=enabled)
            self._set_state(remote_admin_enabled=enabled)

            # The access mode is part of the authenticated WebSocket handshake.
            # Reusing that socket after disabling remote access can leave gateway
            # streams tied to the previous mode/session. Fully restart the tunnel
            # so both disable and re-enable begin with no stale requests or streams.
            await self._async_stop_session()
            await self.async_start()

    async def _run_forever(self) -> None:
        attempt = 0
        while not self._stopping:
            try:
                await self._run_once()
                raise ConnectionError("gateway connection closed")
            except asyncio.CancelledError:
                raise
            except aiohttp.WSServerHandshakeError as err:
                if self._stopping:
                    return
                if err.status not in (401, 403):
                    attempt = self._record_transient_failure(err, attempt)
                else:
                    self._record_auth_failure(err.status)
                    return
            except Exception as err:  # noqa: BLE001 - supervisor must remain alive
                if self._stopping:
                    return
                attempt = self._record_transient_failure(err, attempt)

            delay = self._reconnect_backoff(attempt)
            attempt = min(attempt + 1, 30)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise

    async def _run_once(self) -> None:
        self._last_connection_duration = 0.0
        url = agent_websocket_url(
            self._config.gateway_url,
            agent_id=self._config.agent_id,
            version=INTEGRATION_VERSION,
            remote_admin_enabled=self._config.remote_admin_enabled,
        )
        headers = {"Authorization": f"Bearer {self._config.auth_token}"}
        async with self._session.ws_connect(
            url,
            headers=headers,
            heartbeat=30,
            timeout=aiohttp.ClientWSTimeout(ws_receive=90, ws_close=5),
            max_msg_size=self._config.max_message_bytes,
            autoclose=True,
            autoping=True,
        ) as ws:
            self._ws = ws
            self._connected_since = asyncio.get_running_loop().time()
            now = datetime.now(UTC).isoformat(timespec="seconds")
            self._set_state(
                connected=True,
                reconnect_attempt=0,
                last_connected_at=now,
                last_error=None,
                authentication_failed=False,
            )
            self._auth_failure_reported = False
            receiver = None
            try:
                await self._send_hello()
                self._heartbeat_task = self._task_factory(
                    self._heartbeat_loop(),
                    f"REXLiTE heartbeat {self._config.agent_id}",
                )
                receiver = self._task_factory(
                    self._receive_messages(ws), "REXLiTE gateway receiver"
                )
                done, _ = await asyncio.wait(
                    (receiver, self._heartbeat_task),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    task.result()
                if self._heartbeat_task in done:
                    raise ConnectionError("gateway heartbeat stopped")
            finally:
                # End every task belonging to this session before reconnecting.
                # In particular, an old HTTP request must never reply on a new WS.
                self._ws = None
                tasks = [
                    task
                    for task in (receiver, self._heartbeat_task, *self._handler_tasks)
                    if task is not None
                ]
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                self._heartbeat_task = None
                self._handler_tasks.clear()
                if self._connected_since is not None:
                    self._last_connection_duration = (
                        asyncio.get_running_loop().time() - self._connected_since
                    )
                self._connected_since = None
                self._set_state(connected=False)
                await self._close_all_streams()

    async def _receive_messages(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Receive gateway traffic while the supervisor watches the heartbeat."""

        async for message in ws:
            if message.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                await self._handle_message(message.data)
            elif message.type == aiohttp.WSMsgType.ERROR:
                raise ws.exception() or ConnectionError("gateway WebSocket error")
            elif message.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
            ):
                return

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self._config.heartbeat_interval)
            await self._send_heartbeat()

    async def _send_hello(
        self,
        network_metadata: Mapping[str, str] | None = None,
    ) -> None:
        current_network_metadata = dict(network_metadata or self._network_metadata())
        await self._send(
            TYPE_HELLO,
            payload={
                "version": INTEGRATION_VERSION,
                "metadata": {
                    **self._metadata(),
                    **current_network_metadata,
                },
            },
        )
        self._last_ip_signature = self._network_signature(current_network_metadata)

    async def _send_heartbeat(self) -> None:
        await self._refresh_network_metadata()
        await self._send(
            TYPE_HEARTBEAT,
            payload={"status": "ok", "remote_access_mode": self._state.access_mode},
        )

    async def _refresh_network_metadata(self) -> None:
        metadata = self._network_metadata()
        if self._network_signature(metadata) == self._last_ip_signature:
            return
        await self._send_hello(metadata)

    def _network_metadata(self) -> dict[str, str]:
        try:
            detected = self._ipc_ip_detector(self._config.home_assistant_url)
        except Exception as err:  # noqa: BLE001 - discovery must not stop the tunnel
            _LOGGER.warning(
                "Unable to detect the Home Assistant LAN IPv4 address: %s",
                self._safe_error(err),
            )
            detected = None

        if detected is None:
            return {
                "ipc_lan_ip_status": "unavailable",
                "ipc_lan_ipv4": "",
                "ipc_lan_url": "",
                "ipc_lan_ip_source": "",
                "ipc_lan_ip_detected_at": "",
                "home_assistant_local_url": "",
            }

        local_url = f"http://{detected.address}:8123"
        return {
            "ipc_lan_ip_status": "ok",
            "ipc_lan_ipv4": detected.address,
            "ipc_lan_url": local_url,
            "ipc_lan_ip_source": detected.source,
            "ipc_lan_ip_detected_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "home_assistant_local_url": local_url,
        }

    @staticmethod
    def _network_signature(metadata: Mapping[str, str]) -> tuple[str, str, str]:
        return (
            metadata.get("ipc_lan_ip_status", ""),
            metadata.get("ipc_lan_ipv4", ""),
            metadata.get("ipc_lan_ip_source", ""),
        )

    def _metadata(self) -> dict[str, str]:
        return {
            "role": "home_assistant",
            "home_assistant_url": self._config.home_assistant_url,
            "forwarded_for_policy": "disabled",
            "remote_admin_enabled": str(self._config.remote_admin_enabled).lower(),
            "remote_access_mode": self._state.access_mode,
            "remote_health_monitoring_enabled": "true",
            "home_assistant_info_status": "ok",
            "home_assistant_info_synced_at": datetime.now(UTC).isoformat(
                timespec="seconds"
            ),
            "home_assistant_info_error_class": "",
            "home_assistant_core_version": self._config.home_assistant_version,
            "ha_core_version": self._config.home_assistant_version,
            "core_version": self._config.home_assistant_version,
            "ha_version": self._config.home_assistant_version,
        }

    async def _handle_message(self, raw: str | bytes) -> None:
        try:
            envelope = parse_envelope(raw)
        except ProtocolError as err:
            _LOGGER.warning("Ignoring invalid REXLiTE gateway message: %s", err)
            return

        if envelope.message_type == TYPE_HEARTBEAT:
            await self._send(
                TYPE_HEARTBEAT_ACK,
                envelope.request_id,
                {"status": "ok"},
            )
            return
        if envelope.message_type == TYPE_PROXY_REQUEST:
            if not self._config.remote_admin_enabled:
                await self._send_proxy_error(
                    envelope.request_id, 403, _REMOTE_ADMIN_DISABLED
                )
                return
            if self._is_self_reload(envelope):
                await self._answer_self_reload(envelope.request_id, streamed=False)
                return
            self._spawn_handler(self._handle_proxy_request(envelope))
            return
        if envelope.message_type == TYPE_STREAM_OPEN:
            if not self._config.remote_admin_enabled:
                await self._send(
                    TYPE_STREAM_ACCEPT,
                    envelope.request_id,
                    {"status_code": 403, "error": _REMOTE_ADMIN_DISABLED},
                )
                return
            if self._is_self_reload(envelope):
                await self._answer_self_reload(envelope.request_id, streamed=True)
                return
            self._spawn_handler(self._handle_stream_open(envelope))
            return
        if envelope.message_type == TYPE_STREAM_DATA:
            await self._handle_stream_data(envelope)
            return
        if envelope.message_type == TYPE_STREAM_CLOSE:
            await self._close_stream(envelope.request_id)
            return
        if envelope.message_type in (
            TYPE_HEARTBEAT_ACK,
            TYPE_OTA_MANIFEST,
            TYPE_ERROR,
        ):
            return
        _LOGGER.debug(
            "Ignoring unsupported gateway message type %s", envelope.message_type
        )

    def _is_self_reload(self, envelope: Envelope) -> bool:
        """Return whether a request asks Home Assistant to reload this tunnel."""

        if (
            not envelope.request_id
            or not self._config.entry_id
            or self._self_reload_callback is None
        ):
            return False
        method = envelope.payload.get("method")
        path = envelope.payload.get("path")
        if not isinstance(method, str) or not isinstance(path, str):
            return False
        return method.upper() == "POST" and path.rstrip("/") == (
            f"/api/config/config_entries/entry/{self._config.entry_id}/reload"
        )

    async def _answer_self_reload(self, request_id: str, *, streamed: bool) -> None:
        """Acknowledge a reload of this entry, then reload outside the tunnel.

        Reloading inside the forwarded request would close the session that
        carries it; Home Assistant then cancels the reload halfway and the
        entry stays unloaded with no tunnel left to repair it.
        """

        headers = {"Content-Type": ["application/json"]}
        if streamed:
            await self._send(
                TYPE_STREAM_ACCEPT,
                request_id,
                {"status_code": 200, "headers": headers},
            )
            await self._send_stream_data(request_id, _SELF_RELOAD_RESPONSE)
            await self._send(TYPE_STREAM_CLOSE, request_id, {})
        else:
            await self._send(
                TYPE_PROXY_RESPONSE,
                request_id,
                {
                    "status_code": 200,
                    "headers": headers,
                    "body": encode_bytes(_SELF_RELOAD_RESPONSE),
                },
            )
        _LOGGER.info("REXLiTE AI reload requested through the cloud tunnel")
        if self._self_reload_callback is not None:
            try:
                self._self_reload_callback()
            except Exception:  # noqa: BLE001 - isolate lifecycle notifications
                _LOGGER.exception("Unable to schedule the REXLiTE AI reload")

    def _spawn_handler(self, coroutine: Any) -> None:
        task = self._task_factory(coroutine, "REXLiTE tunnel request")
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_done)

    def _spawn_detached(
        self, coroutine: Coroutine[Any, Any, Any], name: str
    ) -> asyncio.Task[Any]:
        task = self._detached_task_factory(coroutine, name)
        task.add_done_callback(_consume_detached_result)
        return task

    def _handler_done(self, task: asyncio.Task[Any]) -> None:
        self._handler_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            _LOGGER.warning(
                "REXLiTE request handler stopped: %s", self._safe_error(error)
            )

    async def _handle_proxy_request(self, envelope: Envelope) -> None:
        async with self._request_semaphore:
            try:
                method = self._request_method(envelope.payload)
                path = self._required_string(envelope.payload, "path")
                raw_query = self._optional_string(envelope.payload, "raw_query")
                headers = self._request_headers(envelope.payload.get("headers"), False)
                body = decode_bytes(
                    envelope.payload.get("body"), maximum=self._config.max_body_bytes
                )
                url = local_request_url(
                    self._config.home_assistant_url, path, raw_query
                )
                # Home Assistant cancels a request whose client disconnects, so
                # the local exchange must not end with this tunnel session.
                status, response_headers, response_body = await asyncio.shield(
                    self._spawn_detached(
                        self._local_exchange(method, url, headers, body),
                        f"REXLiTE request {envelope.request_id}",
                    )
                )
                await self._send(
                    TYPE_PROXY_RESPONSE,
                    envelope.request_id,
                    {
                        "status_code": status,
                        "headers": response_headers,
                        "body": encode_bytes(response_body),
                    },
                )
            except asyncio.CancelledError:
                raise
            except ProtocolError as err:
                await self._send_proxy_error(envelope.request_id, 400, str(err))
            except (aiohttp.ClientError, TimeoutError, OSError) as err:
                await self._send_proxy_error(
                    envelope.request_id, 502, self._safe_error(err)
                )
            except Exception as err:  # noqa: BLE001 - isolate malformed remote request
                _LOGGER.exception("Unexpected REXLiTE proxy failure")
                await self._send_proxy_error(
                    envelope.request_id, 502, self._safe_error(err)
                )

    async def _local_exchange(
        self,
        method: str,
        url: str,
        headers: list[tuple[str, str]],
        body: bytes,
    ) -> tuple[int, dict[str, list[str]], bytes]:
        timeout = aiohttp.ClientTimeout(total=self._config.request_timeout)
        async with self._session.request(
            method,
            url,
            headers=headers,
            data=body,
            timeout=timeout,
            allow_redirects=False,
            auto_decompress=False,
        ) as response:
            response_body = await self._read_response_body(response)
            return (
                response.status,
                self._response_headers(response.raw_headers),
                response_body,
            )

    async def _read_response_body(self, response: aiohttp.ClientResponse) -> bytes:
        body = bytearray()
        async for chunk in response.content.iter_chunked(64 * 1024):
            body.extend(chunk)
            if len(body) > self._config.max_body_bytes:
                raise ProtocolError(
                    "Home Assistant response exceeds "
                    f"{self._config.max_body_bytes} bytes"
                )
        return bytes(body)

    async def _handle_stream_open(self, envelope: Envelope) -> None:
        if not envelope.request_id:
            return
        if len(self._streams) >= _MAX_STREAMS:
            await self._send(
                TYPE_STREAM_ACCEPT,
                envelope.request_id,
                {"status_code": 429, "error": "too many active streams"},
            )
            return
        awaiting_response = False
        try:
            method = self._request_method(envelope.payload)
            path = self._required_string(envelope.payload, "path")
            raw_query = self._optional_string(envelope.payload, "raw_query")
            body = decode_bytes(
                envelope.payload.get("body"), maximum=self._config.max_body_bytes
            )
            headers = self._request_headers(envelope.payload.get("headers"), True)
            url = local_request_url(self._config.home_assistant_url, path, raw_query)
            reader, writer = await asyncio.wait_for(
                self._open_local_connection(url), timeout=self._config.request_timeout
            )
            stream = _LocalStream(reader=reader, writer=writer)
            self._streams[envelope.request_id] = stream

            request = self._raw_http_request(method, url, headers, body)
            writer.write(request)
            awaiting_response = True
            await asyncio.wait_for(writer.drain(), timeout=self._config.request_timeout)
            response_head = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=self._config.request_timeout
            )
            awaiting_response = False
            if len(response_head) > _MAX_HEADER_BYTES:
                raise ProtocolError("Home Assistant response headers are too large")
            status, response_headers, body_plan = self._parse_http_response_head(
                response_head, method
            )
            await self._send(
                TYPE_STREAM_ACCEPT,
                envelope.request_id,
                {"status_code": status, "headers": response_headers},
            )
            stream.reader_task = self._task_factory(
                self._copy_local_stream(envelope.request_id, stream, body_plan),
                f"REXLiTE stream {envelope.request_id}",
            )
        except asyncio.CancelledError:
            # Only teardown cancels this handler; a caller that gives up sends
            # stream_close instead, which still aborts the request.
            if awaiting_response:
                self._detach_stream(envelope.request_id)
            else:
                await self._close_stream(envelope.request_id)
            raise
        except (
            ProtocolError,
            OSError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ) as err:
            await self._close_stream(envelope.request_id)
            await self._send(
                TYPE_STREAM_ACCEPT,
                envelope.request_id,
                {"status_code": 502, "error": self._safe_error(err)},
            )

    async def _handle_stream_data(self, envelope: Envelope) -> None:
        stream = self._streams.get(envelope.request_id)
        if stream is None:
            return
        try:
            data = decode_bytes(
                envelope.payload.get("data"), maximum=self._config.max_message_bytes
            )
            stream.writer.write(data)
            await asyncio.wait_for(
                stream.writer.drain(), timeout=self._config.request_timeout
            )
        except (ProtocolError, ConnectionError, OSError, TimeoutError) as err:
            await self._close_stream(envelope.request_id)
            await self._send(
                TYPE_STREAM_CLOSE,
                envelope.request_id,
                {"error": self._safe_error(err)},
            )

    async def _copy_local_stream(
        self,
        request_id: str,
        stream: _LocalStream,
        body_plan: HttpResponseBodyPlan,
    ) -> None:
        error = ""
        try:
            if body_plan.mode == "fixed":
                await self._copy_fixed_http_body(
                    request_id, stream.reader, body_plan.length
                )
            elif body_plan.mode == "chunked":
                await self._copy_chunked_http_body(request_id, stream.reader)
            elif body_plan.mode == "close":
                while data := await stream.reader.read(32 * 1024):
                    await self._send_stream_data(request_id, data)
        except asyncio.CancelledError:
            return
        except (
            ConnectionError,
            OSError,
            ProtocolError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ) as err:
            error = self._safe_error(err)
        finally:
            await self._close_stream(request_id, cancel_reader=False)
            if self._ws is not None and not self._ws.closed:
                with suppress(ConnectionError, aiohttp.ClientError):
                    await self._send(
                        TYPE_STREAM_CLOSE,
                        request_id,
                        {"error": error} if error else {},
                    )

    async def _copy_fixed_http_body(
        self,
        request_id: str,
        reader: asyncio.StreamReader,
        length: int,
    ) -> None:
        remaining = length
        while remaining > 0:
            data = await reader.readexactly(min(32 * 1024, remaining))
            remaining -= len(data)
            await self._send_stream_data(request_id, data)

    async def _copy_chunked_http_body(
        self,
        request_id: str,
        reader: asyncio.StreamReader,
    ) -> None:
        while True:
            size_line = await reader.readuntil(b"\r\n")
            if len(size_line) > _MAX_HEADER_BYTES:
                raise ProtocolError("Home Assistant chunk header is too large")
            try:
                chunk_size = int(size_line[:-2].split(b";", 1)[0].strip(), 16)
            except ValueError as err:
                raise ProtocolError("invalid Home Assistant chunk size") from err
            if chunk_size < 0:
                raise ProtocolError("invalid Home Assistant chunk size")
            if chunk_size == 0:
                while trailer := await reader.readuntil(b"\r\n"):
                    if trailer == b"\r\n":
                        return
                    if len(trailer) > _MAX_HEADER_BYTES:
                        raise ProtocolError("Home Assistant trailer is too large")

            remaining = chunk_size
            while remaining > 0:
                data = await reader.readexactly(min(32 * 1024, remaining))
                remaining -= len(data)
                await self._send_stream_data(request_id, data)
            if await reader.readexactly(2) != b"\r\n":
                raise ProtocolError("invalid Home Assistant chunk terminator")

    async def _send_stream_data(self, request_id: str, data: bytes) -> None:
        if data:
            await self._send(TYPE_STREAM_DATA, request_id, {"data": encode_bytes(data)})

    async def _open_local_connection(
        self, url: str
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        ssl_context: ssl.SSLContext | bool | None = None
        server_hostname: str | None = None
        if parsed.scheme == "https":
            ssl_context = ssl.create_default_context()
            server_hostname = parsed.hostname
        return await asyncio.open_connection(
            parsed.hostname,
            port,
            ssl=ssl_context,
            server_hostname=server_hostname,
            limit=_MAX_HEADER_BYTES,
        )

    def _raw_http_request(
        self,
        method: str,
        url: str,
        headers: list[tuple[str, str]],
        body: bytes,
    ) -> bytes:
        parsed = urlsplit(url)
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        if "\r" in method or "\n" in method or "\r" in target or "\n" in target:
            raise ProtocolError("invalid HTTP request target")

        is_upgrade = any(
            name.lower() == "upgrade" and value.strip() for name, value in headers
        )
        filtered = [
            (name, value)
            for name, value in headers
            if name.lower() not in {"host", "content-length"}
            and (is_upgrade or name.lower() not in _HOP_BY_HOP_HEADERS)
        ]
        filtered.append(("Host", parsed.netloc))
        filtered.append(("Content-Length", str(len(body))))
        if not is_upgrade:
            filtered.append(("Connection", "close"))
        lines = [f"{method} {target} HTTP/1.1"]
        for name, value in filtered:
            if "\r" in name or "\n" in name or "\r" in value or "\n" in value:
                raise ProtocolError("invalid HTTP header")
            lines.append(f"{name}: {value}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body

    def _parse_http_response_head(
        self, data: bytes, method: str
    ) -> tuple[int, dict[str, list[str]], HttpResponseBodyPlan]:
        try:
            text = data.decode("latin-1")
            lines = text.split("\r\n")
            status_parts = lines[0].split(" ", 2)
            status = int(status_parts[1])
        except (UnicodeDecodeError, ValueError, IndexError) as err:
            raise ProtocolError("invalid Home Assistant HTTP response") from err
        raw_headers: dict[str, list[str]] = {}
        headers: dict[str, list[str]] = {}
        for line in lines[1:]:
            if not line:
                break
            if ":" not in line:
                raise ProtocolError("invalid Home Assistant HTTP response header")
            name, value = line.split(":", 1)
            raw_headers.setdefault(name.strip(), []).append(value.strip())
            if name.lower() in _HOP_BY_HOP_HEADERS and status != 101:
                continue
            headers.setdefault(name.strip(), []).append(value.strip())
        return status, headers, http_response_body_plan(method, status, raw_headers)

    def _request_headers(self, value: Any, keep_upgrade: bool) -> list[tuple[str, str]]:
        if value is None:
            return []
        if not isinstance(value, Mapping):
            raise ProtocolError("headers must be an object")
        output: list[tuple[str, str]] = []
        local = urlsplit(self._config.home_assistant_url)
        local_origin = f"{local.scheme}://{local.netloc}"
        for raw_name, raw_values in value.items():
            if not isinstance(raw_name, str):
                raise ProtocolError("header name must be a string")
            name = raw_name.strip()
            lower = name.lower()
            if name and not _HTTP_HEADER_PATTERN.fullmatch(name):
                raise ProtocolError("header name contains invalid characters")
            if (
                not name
                or lower == "host"
                or lower == "forwarded"
                or lower.startswith("x-forwarded-")
                or (not keep_upgrade and lower in _HOP_BY_HOP_HEADERS)
            ):
                continue
            values = raw_values if isinstance(raw_values, list) else [raw_values]
            for raw_value in values:
                if not isinstance(raw_value, str):
                    raise ProtocolError("header value must be a string")
                header_value = local_origin if lower == "origin" else raw_value
                if "\r" in header_value or "\n" in header_value:
                    raise ProtocolError("header value contains invalid characters")
                output.append((name, header_value))
        return output

    def _response_headers(
        self, raw_headers: tuple[tuple[bytes, bytes], ...]
    ) -> dict[str, list[str]]:
        output: dict[str, list[str]] = {}
        local = urlsplit(self._config.home_assistant_url)
        local_origin = f"{local.scheme}://{local.netloc}"
        for raw_name, raw_value in raw_headers:
            name = raw_name.decode("latin-1")
            if name.lower() in _HOP_BY_HOP_HEADERS:
                continue
            value = raw_value.decode("latin-1")
            if name.lower() == "location" and value.startswith(local_origin):
                value = value[len(local_origin) :] or "/"
            output.setdefault(name, []).append(value)
        return output

    async def _send_proxy_error(
        self, request_id: str, status: int, message: str
    ) -> None:
        await self._send(
            TYPE_PROXY_RESPONSE,
            request_id,
            {"status_code": status, "error": message},
        )

    async def _send(
        self,
        message_type: str,
        request_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            raise ConnectionError("gateway is not connected")
        data = make_envelope(message_type, request_id, payload)
        async with asyncio.timeout(self._config.request_timeout):
            async with self._send_lock:
                await ws.send_str(data)

    async def _close_stream(
        self, request_id: str, *, cancel_reader: bool = True
    ) -> None:
        stream = self._streams.pop(request_id, None)
        if stream is None:
            return
        current = asyncio.current_task()
        if (
            cancel_reader
            and stream.reader_task is not None
            and stream.reader_task is not current
            and not stream.reader_task.done()
        ):
            stream.reader_task.cancel()
        stream.writer.close()
        with suppress(ConnectionError, OSError, TimeoutError):
            await asyncio.wait_for(
                stream.writer.wait_closed(),
                timeout=min(5.0, self._config.request_timeout),
            )

    def _detach_stream(self, request_id: str) -> None:
        """Let Home Assistant finish a request whose tunnel session ended.

        Home Assistant cancels a handler when its client disconnects, so closing
        the socket now would abort the operation (such as a config entry
        reload) halfway. Keep it open until the response starts, then drop it.
        """

        stream = self._streams.pop(request_id, None)
        if stream is not None:
            self._spawn_detached(
                self._drain_detached_stream(stream),
                f"REXLiTE detached request {request_id}",
            )

    async def _drain_detached_stream(self, stream: _LocalStream) -> None:
        try:
            await asyncio.wait_for(
                stream.reader.readuntil(b"\r\n\r\n"),
                timeout=self._config.request_timeout,
            )
        except (
            OSError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            pass
        finally:
            stream.writer.close()
            with suppress(ConnectionError, OSError, TimeoutError):
                await asyncio.wait_for(
                    stream.writer.wait_closed(),
                    timeout=min(5.0, self._config.request_timeout),
                )

    async def _close_all_streams(self) -> None:
        readers = [
            stream.reader_task
            for stream in self._streams.values()
            if stream.reader_task is not None
            and stream.reader_task is not asyncio.current_task()
        ]
        await asyncio.gather(
            *(self._close_stream(request_id) for request_id in list(self._streams)),
            return_exceptions=True,
        )
        if readers:
            await asyncio.gather(*readers, return_exceptions=True)

    def _set_state(self, **changes: Any) -> None:
        next_state = replace(self._state, **changes)
        if next_state == self._state:
            return
        self._state = next_state
        self._state_callback(next_state)

    def _record_auth_failure(self, status: int) -> None:
        """Stop retrying a permanently rejected credential and request reauth."""

        self._set_state(
            connected=False,
            reconnect_attempt=0,
            last_error=f"authentication_failed (HTTP {status})",
            authentication_failed=True,
        )
        if self._auth_failure_reported:
            return
        self._auth_failure_reported = True
        _LOGGER.warning(
            "REXLiTE AI rejected the stored service credential; "
            "Home Assistant reauthentication is required"
        )
        if self._auth_failure_callback is not None:
            try:
                self._auth_failure_callback()
            except Exception:  # noqa: BLE001 - isolate lifecycle notifications
                _LOGGER.exception("Unable to start REXLiTE reauthentication")

    def _record_transient_failure(self, err: BaseException, attempt: int) -> int:
        """Record a retryable failure without flooding the Home Assistant log."""

        if self._stopping:
            return attempt
        if self._last_connection_duration >= 300:
            attempt = 0
        safe_error = self._safe_error(err)
        self._set_state(
            connected=False,
            reconnect_attempt=attempt + 1,
            last_error=safe_error,
            authentication_failed=False,
        )
        log = _LOGGER.warning if attempt == 0 else _LOGGER.debug
        log(
            "REXLiTE gateway disconnected; retrying (attempt %s): %s",
            attempt + 1,
            safe_error,
        )
        return attempt

    def _reconnect_backoff(self, attempt: int) -> float:
        maximum = max(self._config.reconnect_delay, self._config.reconnect_max_delay)
        delay = min(self._config.reconnect_delay * (2 ** min(attempt, 20)), maximum)
        return random.uniform(delay / 2, delay)  # noqa: S311 - jitter, not cryptography

    def _safe_error(self, err: BaseException) -> str:
        message = f"{type(err).__name__}: {err}".strip()
        if self._config.auth_token:
            message = message.replace(self._config.auth_token, "[redacted]")
        return message[:512]

    @staticmethod
    def _required_string(payload: Mapping[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            raise ProtocolError(f"{key} is required")
        return value

    @staticmethod
    def _optional_string(payload: Mapping[str, Any], key: str) -> str:
        value = payload.get(key, "")
        if not isinstance(value, str):
            raise ProtocolError(f"{key} must be a string")
        return value

    @staticmethod
    def _request_method(payload: Mapping[str, Any]) -> str:
        value = REXLiTETunnelClient._required_string(payload, "method").upper()
        if not _HTTP_METHOD_PATTERN.fullmatch(value):
            raise ProtocolError("method contains invalid characters")
        return value
