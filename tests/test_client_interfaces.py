import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from callbacks import ClientConfigCallback, ClientConfigFlowCallback
from cascade_api import (
    CascadeError,
    CascadeRouter,
    CascadeServer,
    ClientInterface,
    load_cascade_servers,
)
from database import Database
from handlers.access import (
    annotated_config_details,
    capture_client_config_name,
    create_client_config,
    creation_panel,
    get_client_config_workflow,
    save_creation_step,
    select_client_config_location,
    start_client_config_workflow,
)

OPTIONS = (
    ClientInterface("wg11", "AWG 2.0", "Supports AWG 2.0."),
    ClientInterface("awg3", "AWG 3.1", "Supports AWG 3.1."),
)


class ClientInterfaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Database(str(Path(directory.name) / "test.db"))
        self.db.ensure_subscription(10, "alice", "2099-01-01 00:00:00", "paid", "30_days", "stars")
        self.server = CascadeServer(
            "server", "https://example.test", "x" * 32, "uuid-2", 1, 100,
            server_name="Location", client_interfaces=OPTIONS,
        )
        self.router = CascadeRouter(self.db, servers=[self.server])
        self.live = [{"id": "uuid-2", "name": "wg11"}, {"id": "uuid-3", "name": "awg3"}]
        self.api = SimpleNamespace(
            list_interfaces=AsyncMock(side_effect=lambda: self.live),
            list_peers=AsyncMock(return_value=[]),
            get_peer=AsyncMock(return_value={"groupId": "basic"}),
            resolve_client_group_name=AsyncMock(return_value="Basic"),
            list_client_groups=AsyncMock(return_value=[{"id": "basic", "name": "Basic"}]),
            download_config=AsyncMock(return_value=b"config"),
            delete_peer=AsyncMock(),
        )
        self.counter = 0

        async def create(name, expiry, interface_id, client_group=None):
            self.counter += 1
            return {"id": f"peer-{self.counter}", "publicKey": f"key-{self.counter}", "name": name}

        self.api.create_peer = AsyncMock(side_effect=create)
        self.router.apis = {"server": self.api}
        self.callback = SimpleNamespace(
            from_user=SimpleNamespace(id=10),
            message=SimpleNamespace(chat=SimpleNamespace(id=10), message_id=100),
        )
        self.edit = AsyncMock()

    async def create(self, name, interface_id="uuid-2", interface_name="wg11"):
        return await self.router.create_managed_config(
            10, name, "server", interface_id, interface_name=interface_name,
            production_only=True, self_service_limit=3,
        )

    async def test_both_versions_use_resolved_ids_and_share_limit(self):
        for index, (uid, name) in enumerate((("uuid-2", "wg11"), ("uuid-3", "awg3"), ("uuid-2", "wg11"))):
            stored, content = await self.create(f"Device {index}", uid, name)
            self.assertEqual(stored["interface_id"], uid)
            self.assertEqual(content, b"config")
            self.assertEqual(self.api.create_peer.await_args.args[2], uid)
            self.assertEqual(self.api.download_config.await_args.args[1], uid)
        with self.assertRaisesRegex(CascadeError, "limit reached"):
            await self.create("Fourth", "uuid-3", "awg3")
        self.assertEqual(self.api.create_peer.await_count, 3)

    async def test_missing_ambiguous_and_recreated_interfaces_block_creation(self):
        for live in (
            self.live[:1], self.live + [dict(self.live[0], id="other")],
            [dict(self.live[0], id="replacement"), self.live[1]],
            [dict(self.live[0], name="renamed"), self.live[1]],
        ):
            with self.subTest(live=live):
                self.live = live
                with self.assertRaises(CascadeError):
                    await self.create("Phone")
        self.api.create_peer.assert_not_awaited()

    async def test_binding_rechecked_inside_creation_lock(self):
        original = await self.router.get_client_interfaces("server")
        changed = [dict(original[0], interface_id="replacement"), original[1]]
        with (
            patch.object(self.router, "get_client_interfaces", AsyncMock(side_effect=[original, changed])),
            self.assertRaises(CascadeError),
        ):
            await self.create("Phone")
        self.api.create_peer.assert_not_awaited()

    async def test_unlisted_interface_and_changed_allowlist_block_creation(self):
        with self.assertRaises(CascadeError):
            await self.create("Phone", "uuid-3", "wg11")
        self.router.servers = [replace(self.server, client_interfaces=())]
        self.assertEqual(self.router.get_client_production_locations(), [])
        with self.assertRaises(CascadeError):
            await self.create("Phone")
        self.api.create_peer.assert_not_awaited()

    async def test_legacy_and_annotations_do_not_guess_unknown_versions(self):
        text = await annotated_config_details(
            self.router, {"server_key": "server", "interface_id": "uuid-3", "config_name": "Phone"}, "Location"
        )
        self.assertIn("AWG 3.1", text)
        self.assertIn("Supports AWG 3.1.", text)
        self.assertIsNone(await self.router.get_client_interface_annotation("server", "unknown"))
        self.router.servers = [replace(self.server, client_interfaces=None)]
        await self.create("Legacy", interface_name=None)
        self.assertIsNone(await self.router.get_client_interface_annotation("server", "uuid-2"))

    async def step(self, action, value=0, token=None):
        flow = get_client_config_workflow(self.db, 10)
        data = ClientConfigFlowCallback(action=action, token=token or flow["token"], value=value)
        await select_client_config_location(
            self.callback, self.db, self.router, AsyncMock(), self.edit, data,
        )
        return get_client_config_workflow(self.db, 10)

    def start(self):
        flow = {"config_name": "Phone", "locations": self.router.get_client_production_locations()}
        save_creation_step(self.db, 10, flow, "select_location")
        return flow

    async def test_name_entry_starts_token_bound_location_selection(self):
        await start_client_config_workflow(
            self.callback, self.db, AsyncMock(), self.edit, lambda _: None,
            lambda _: True, ClientConfigCallback(action="create"),
        )
        flow = get_client_config_workflow(self.db, 10)
        self.assertEqual(flow["state"], "await_create_name")
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=10), text="Phone", bot=object(), delete=AsyncMock(),
        )
        with patch("handlers.access.edit_telegram_text", AsyncMock()):
            await capture_client_config_name(message, self.db, self.router)
        flow = get_client_config_workflow(self.db, 10)
        self.assertEqual(flow["state"], "select_location")
        self.assertEqual(flow["config_name"], "Phone")
        self.assertEqual(flow["locations"][0]["server_key"], "server")

    async def test_api_failure_and_revoked_access_do_not_create(self):
        self.start()
        self.api.list_interfaces.side_effect = CascadeError("Unavailable")
        self.assertIsNone(await self.step("location"))
        self.api.list_interfaces.side_effect = lambda: self.live
        self.start()
        with patch.object(self.db, "has_active_access", return_value=False):
            self.assertIsNone(await self.step("location"))
        self.api.create_peer.assert_not_awaited()

    async def test_selection_annotations_back_cancel_and_stale_buttons(self):
        initial = self.start()
        flow = await self.step("location")
        self.assertEqual(flow["state"], "select_interface")
        text, keyboard = creation_panel(flow)
        for option in OPTIONS:
            self.assertIn(option.description, text)
        for row in keyboard.inline_keyboard:
            self.assertLessEqual(len(row[0].callback_data.encode()), 64)
        unchanged = await self.step("location", token=initial["token"])
        self.assertEqual(flow, unchanged)
        flow = await self.step("interface", 1)
        self.assertEqual(flow["interface_id"], "uuid-3")
        self.assertIn("Supports AWG 3.1.", creation_panel(flow)[0])
        flow = await self.step("interfaces")
        self.assertEqual(flow["state"], "select_interface")
        flow = await self.step("locations")
        self.assertEqual(flow["state"], "select_location")
        self.assertIsNone(await self.step("cancel"))

    async def test_single_option_still_shows_annotation_and_legacy_skips_selection(self):
        self.router.servers = [replace(self.server, client_interfaces=OPTIONS[:1])]
        self.start()
        flow = await self.step("location")
        self.assertEqual(flow["state"], "select_interface")
        self.assertIn(OPTIONS[0].description, creation_panel(flow)[0])
        self.router.servers = [replace(self.server, client_interfaces=None)]
        self.start()
        flow = await self.step("location")
        self.assertEqual(flow["state"], "confirm_create")
        self.assertNotIn("interface_name", flow)

    async def test_concurrent_confirmations_create_and_send_only_once(self):
        self.start()
        await self.step("location")
        flow = await self.step("interface", 1)
        data = ClientConfigFlowCallback(action="create_confirm", token=flow["token"])
        sender = AsyncMock(return_value=True)
        await asyncio.gather(*(
            create_client_config(self.callback, self.db, self.router, AsyncMock(), self.edit, sender, data)
            for _ in range(2)
        ))
        self.api.create_peer.assert_awaited_once()
        sender.assert_awaited_once()
        self.assertIsNone(get_client_config_workflow(self.db, 10))

    async def test_changed_interface_at_confirmation_requires_new_selection(self):
        self.start()
        await self.step("location")
        flow = await self.step("interface")
        self.live[0]["id"] = "replacement"
        sender = AsyncMock()
        await create_client_config(
            self.callback, self.db, self.router, AsyncMock(), self.edit, sender,
            ClientConfigFlowCallback(action="create_confirm", token=flow["token"]),
        )
        self.api.create_peer.assert_not_awaited()
        sender.assert_not_awaited()
        self.assertIsNone(get_client_config_workflow(self.db, 10))
        self.assertIn("начни выбор заново", self.edit.await_args.args[1])


class ClientInterfaceRegistryTests(unittest.TestCase):
    def load(self, value, present=True):
        server = {
            "server_key": "server", "base_url": "https://example.test", "api_token": "x" * 32,
            "interface_id": "uuid", "max_peers": 100,
        }
        if present:
            server["client_interfaces"] = value
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "servers.json"
            path.write_text(json.dumps({"servers": [server]}))
            return load_cascade_servers(path)[0]

    def test_new_legacy_and_empty_lists(self):
        options = [vars(option) for option in OPTIONS]
        self.assertEqual(self.load(options).client_interfaces, OPTIONS)
        self.assertIsNone(self.load(None, present=False).client_interfaces)
        self.assertEqual(self.load([]).client_interfaces, ())

    def test_invalid_and_duplicate_names(self):
        option = vars(OPTIONS[0])
        for invalid in (None, {}, [None], [option, option], [dict(option, interface_name="")],
                        [dict(option, name=123)], [dict(option, description="bad\ntext")]):
            with self.subTest(value=invalid), self.assertRaises(CascadeError):
                self.load(invalid)
