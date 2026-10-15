"""HACS binding with the operation platform's GitHub token."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path

PACKAGE = Path(__file__).parents[1] / "custom_components/rexlite"
package = types.ModuleType("rexlite_hacs_bind_tests")
package.__path__ = [str(PACKAGE)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(
    f"{package.__name__}.hacs_bind", PACKAGE / "hacs_bind.py"
)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)

TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
OTHER = "github_pat_" + "11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz"


class Entry:
    def __init__(
        self, token: str = "old", state: str = "loaded", disabled_by: str | None = None
    ) -> None:
        self.entry_id = "e1"
        self.data = {"token": token}
        self.state = types.SimpleNamespace(value=state)
        self.disabled_by = disabled_by


class Hub:
    """A HA config entry registry with HACS installed."""

    def __init__(
        self,
        *,
        installed: bool = True,
        entries: list[Entry] | None = None,
        token_error=None,
    ) -> None:
        self.installed = installed
        self.list = entries or []
        self.token_error = token_error
        self.rebind_error = None
        self.calls: list[str] = []
        self.checked: list[str] = []

    def binder(self):
        async def hacs_installed():
            return self.installed

        async def check_token(token):
            self.checked.append(token)
            if self.token_error:
                raise self.token_error

        async def add_entry(token):
            self.calls.append("add")
            self.list.append(Entry(token))
            return "loaded"

        async def rebind(entry, token):
            self.calls.append("rebind")
            if self.rebind_error:
                raise self.rebind_error
            entry.data = {**entry.data, "token": token}
            entry.state = types.SimpleNamespace(value="loaded")
            return "loaded"

        def abort_reauth():
            self.calls.append("abort_reauth")
            return 1

        return m.HacsBinder(
            hacs_installed=hacs_installed,
            check_token=check_token,
            entries=lambda: list(self.list),
            add_entry=add_entry,
            rebind=rebind,
            abort_reauth=abort_reauth,
        )


def bind(hub: Hub, token):
    return asyncio.run(hub.binder().bind(token))


class HacsBindTests(unittest.TestCase):
    def test_creates_the_entry_hacs_would_create(self) -> None:
        hub = Hub()
        self.assertEqual(
            bind(hub, f"  {TOKEN}\n"),
            {"bound": True, "created": True, "state": "loaded"},
        )
        self.assertEqual(hub.calls, ["add"])
        self.assertEqual(hub.checked, [TOKEN])
        self.assertEqual(hub.list[0].data, {"token": TOKEN})

    def test_rebinding_updates_the_token_reloads_and_ends_reauth(self) -> None:
        entry = Entry(token="expired", state="setup_error")
        hub = Hub(entries=[entry])
        self.assertEqual(
            bind(hub, OTHER), {"bound": True, "created": False, "state": "loaded"}
        )
        self.assertEqual(hub.calls, ["abort_reauth", "rebind"])
        self.assertEqual(entry.data["token"], OTHER)

    def test_same_token_on_a_loaded_hacs_does_not_reload(self) -> None:
        hub = Hub(entries=[Entry(token=TOKEN)])
        self.assertEqual(bind(hub, TOKEN)["state"], "loaded")
        self.assertEqual(hub.calls, ["abort_reauth"])

    def test_refusals_happen_before_anything_changes(self) -> None:
        cases = [
            (Hub(), "not-a-token", "hacs_token_invalid"),
            (Hub(), None, "hacs_token_invalid"),
            (Hub(installed=False), TOKEN, "hacs_not_installed"),
            (
                Hub(token_error=m.HacsBindError("hacs_token_invalid", "x")),
                TOKEN,
                "hacs_token_invalid",
            ),
            (Hub(entries=[Entry(disabled_by="user")]), TOKEN, "hacs_disabled"),
        ]
        for hub, token, code in cases:
            with (
                self.subTest(code=code, token=token),
                self.assertRaises(m.HacsBindError) as caught,
            ):
                bind(hub, token)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("add", hub.calls)
            self.assertNotIn("rebind", hub.calls)
            self.assertNotIn(TOKEN, caught.exception.message)

    def test_a_new_token_on_a_loaded_hacs_rebinds_it(self) -> None:
        # HACS reloads itself on entry updates; the binder must go through
        # HA's unload and setup instead of updating a loaded entry in place.
        entry = Entry(token=TOKEN, state="loaded")
        hub = Hub(entries=[entry])
        self.assertEqual(bind(hub, OTHER)["state"], "loaded")
        self.assertEqual(hub.calls, ["abort_reauth", "rebind"])
        self.assertEqual(entry.data["token"], OTHER)

    def test_rebind_failures_keep_their_codes(self) -> None:
        hub = Hub(entries=[Entry(token="expired", state="setup_error")])
        hub.rebind_error = m.HacsBindError("hacs_busy", "HACS 正在處理工作，請稍後再試")
        with self.assertRaises(m.HacsBindError) as caught:
            bind(hub, TOKEN)
        self.assertEqual(caught.exception.code, "hacs_busy")

    def test_rebind_unloads_before_updating_the_token(self) -> None:
        source = (PACKAGE / "hacs_bind.py").read_text(encoding="utf-8")
        body = source[source.index("async def rebind") :]
        unload = body.index("async_unload(")
        update = body.index("async_update_entry(")
        setup = body.index("async_setup(")
        self.assertLess(unload, update)
        self.assertLess(update, setup)
        self.assertNotIn("async_reload(", source)

    def test_github_answers_map_to_stable_codes(self) -> None:
        self.assertIsNone(m.github_token_error(200, 4999))
        self.assertIsNone(m.github_token_error(200, None))
        self.assertEqual(m.github_token_error(401, None).code, "hacs_token_invalid")
        self.assertEqual(m.github_token_error(403, None).code, "hacs_rate_limited")
        self.assertEqual(m.github_token_error(200, 0).code, "hacs_rate_limited")
        self.assertEqual(
            m.github_token_error(502, None).code, "hacs_github_unavailable"
        )

    def test_the_command_schema_never_echoes_the_token(self) -> None:
        source = (PACKAGE / "hacs_bind.py").read_text(encoding="utf-8")
        self.assertIn('vol.Required("token"): str}', source)
        self.assertNotIn("_LOGGER", source)


if __name__ == "__main__":
    unittest.main()
