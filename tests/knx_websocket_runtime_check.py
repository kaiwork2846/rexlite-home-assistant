"""Exercise registered commands through HA's real ActiveConnection dispatcher.

The deployment boundary is isolated: no host configuration or bus is changed.
Unlike a mock connection, ActiveConnection has no require_admin() method.
"""

import asyncio
import hashlib
import importlib.util
import json
import logging
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

from homeassistant.auth.models import User
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.const import __version__
from homeassistant.core import HomeAssistant

BASE = Path(__file__).parents[1] / "custom_components/rexlite"
package = types.ModuleType("rexlite_real_websocket")
package.__path__ = [str(BASE)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(
    package.__name__ + ".knx_project_deployment", BASE / "knx_project_deployment.py"
)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


async def main() -> None:
    fingerprint = "a" * 64
    commands = [
        ("project_capabilities", "capabilities", {}, ()),
        (
            "process_project",
            "process_project",
            {"file_id": "test-file", "password": "", "projectFingerprint": fingerprint},
            ("test-file", "", fingerprint),
        ),
        (
            "deploy_project",
            "deploy",
            {"projectFingerprint": fingerprint},
            (fingerprint,),
        ),
        (
            "project_deployment_status",
            "status",
            {"projectFingerprint": fingerprint},
            (fingerprint,),
        ),
    ]
    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / "configuration.yaml").write_text("default_config:\n")
        hass = HomeAssistant(directory)
        deployer = m.register_websocket_commands(hass)
        assert m.register_websocket_commands(hass) is deployer
        assert len(hass.data["websocket_api"]) == 7
        admin = User(name="Test administrator", perm_lookup=None, is_owner=True)
        viewer = User(name="Test viewer", perm_lookup=None)
        responses = asyncio.Queue()

        def send(message):
            responses.put_nowait(
                json.loads(message) if isinstance(message, bytes | str) else message
            )

        # Core 2026.5 added the remote argument to the connection constructor.
        connection_options = (
            {"remote": None}
            if "remote" in ActiveConnection.__init__.__code__.co_varnames
            else {}
        )
        connection = ActiveConnection(
            logging.getLogger(__name__),
            hass,
            send,
            admin,
            types.SimpleNamespace(id="test-session"),
            **connection_options,
        )
        assert not hasattr(connection, "require_admin")
        message_id = 0

        async def request(command, fields):
            nonlocal message_id
            message_id += 1
            connection.async_handle(
                {"id": message_id, "type": f"rexlite/knx/{command}", **fields}
            )
            response = await asyncio.wait_for(responses.get(), 5)
            assert response["id"] == message_id, response
            return response

        # Exercise the real capability implementation before isolating mutations.
        response = await request("project_capabilities", {})
        assert response["success"] and response["result"]["supported"], response

        transfer = {
            "uploadId": "b" * 32,
            "owner": "test",
            "action": "start",
            "fileName": "test.knxproj",
            "size": 4,
            "projectFingerprint": hashlib.sha256(b"PK\x03\x04").hexdigest(),
        }
        connection.user = viewer
        assert (await request("project_upload", transfer))["error"][
            "code"
        ] == "unauthorized"
        connection.user = admin
        assert (await request("project_upload", transfer))["success"]
        chunk = {
            "uploadId": "b" * 32,
            "owner": "test",
            "action": "chunk",
            "offset": 0,
            "data": "UEsDBA==",
        }
        assert (await request("project_upload", chunk))["result"]["offset"] == 4
        assert (await request("project_upload", chunk))["result"]["offset"] == 4
        assert (
            await request(
                "project_upload",
                {"uploadId": "b" * 32, "owner": "test", "action": "seal"},
            )
        )["result"]["sealed"]
        assert (
            await request(
                "project_upload",
                {"uploadId": "b" * 32, "owner": "test", "action": "discard"},
            )
        )["success"]

        for user in (viewer, admin):
            connection.user = user
            for command, method, fields, arguments in commands:
                result = {"command": command}
                with patch.object(
                    deployer, method, AsyncMock(return_value=result)
                ) as work:
                    response = await request(command, fields)
                    if user is admin:
                        assert response["success"] and response["result"] == result, (
                            response
                        )
                        work.assert_awaited_once_with(*arguments)
                    else:
                        assert not response["success"], response
                        assert response["error"]["code"] == "unauthorized", response
                        work.assert_not_called()

        for user in (viewer, admin):
            connection.user = user
            with patch.object(
                deployer, "deploy", AsyncMock(return_value={"status": "ready"})
            ) as work:
                response = await request(
                    "manual_yaml",
                    {
                        "projectFingerprint": fingerprint,
                        "yaml": "knx: {}",
                        "action": "check",
                    },
                )
                if user is admin:
                    assert response["success"], response
                    work.assert_awaited_once_with(
                        fingerprint, manual_yaml="knx: {}", check_only=True, baseline=""
                    )
                else:
                    assert response["error"]["code"] == "unauthorized", response
                    work.assert_not_called()

        # Schema validation must reject bad fingerprints before any operation.
        with patch.object(deployer, "deploy", AsyncMock()) as work:
            response = await request(
                "deploy_project", {"projectFingerprint": "invalid"}
            )
            assert response["error"]["code"] == "invalid_format", response
            work.assert_not_called()

        # Failed imports remain explicit errors and a retry can succeed.
        fields = commands[1][2]
        for failure, expected in (
            (
                m.DeploymentError("project_file_fingerprint_mismatch"),
                "project_file_fingerprint_mismatch",
            ),
            (ValueError("private parser details"), "project_parse_failed"),
        ):
            with patch.object(
                deployer, "process_project", AsyncMock(side_effect=failure)
            ):
                response = await request("process_project", fields)
                assert response["error"] == {
                    "code": "project_import_failed",
                    "message": expected,
                }, response
        with patch.object(
            deployer, "process_project", AsyncMock(return_value={"ok": True})
        ):
            assert (await request("process_project", fields))["result"] == {"ok": True}

        # The gateway scan is admin-only too; isolate it from the host network.
        class Scanner:
            def __init__(self, xknx, **options):
                assert options == {"stop_on_found": 0, "timeout_in_seconds": 3}

            async def scan(self):
                return [
                    types.SimpleNamespace(
                        name="Router",
                        ip_addr="192.0.2.10",
                        port=3671,
                        individual_address=None,
                        supports_tunnelling=True,
                        supports_tunnelling_tcp=False,
                        supports_routing=True,
                        tunnelling_requires_secure=None,
                        routing_requires_secure=None,
                    )
                ]

        scan_module = sys.modules[package.__name__ + ".knx_gateway_scan"]
        with patch.object(scan_module, "_scanner_types", lambda: (object, Scanner)):
            connection.user = viewer
            response = await request("gateway_scan", {})
            assert response["error"]["code"] == "unauthorized", response
            connection.user = admin
            response = await request("gateway_scan", {})
            assert response["success"], response
            assert response["result"][0]["ip"] == "192.0.2.10", response
            assert response["result"][0]["tunnellingRequiresSecure"] is False

        connection.async_handle_close()
        await hass.async_stop(force=True)
    print(
        f"HA {__version__}: real WebSocket admin, denied access, "
        "schemas, errors and retry PASS"
    )


if __name__ == "__main__":
    asyncio.run(main())
