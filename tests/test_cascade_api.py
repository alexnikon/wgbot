import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from cascade_api import (
    CascadeAPI,
    CascadeError,
    CascadeNotFound,
    CascadeRouter,
    CascadeServer,
    load_cascade_servers,
)
from database import ActiveSubscriptionError, Database


class FakeCascadeAPI:
    def __init__(self, peer_count):
        self.peer_count = peer_count

    async def list_peers(self):
        return [{"id": str(index)} for index in range(self.peer_count)]


class ProvisioningCascadeAPI(FakeCascadeAPI):
    def __init__(self):
        super().__init__(peer_count=0)
        self.created = 0

    async def create_peer(self, name, expired_at, interface_id=None):
        self.created += 1
        return {
            "id": "new-peer",
            "name": name,
            "publicKey": "new-public-key",
            "enabled": True,
        }

    async def download_config(self, peer_id, interface_id=None):
        return b"[Interface]\nPrivateKey = test"

    async def delete_peer(self, peer_id, interface_id=None):
        return None


class CascadeServerRegistryTests(unittest.TestCase):
    def _write_registry(self, payload):
        handle, path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        Path(path).write_text(json.dumps(payload), encoding="utf-8")
        self.addCleanup(Path(path).unlink, missing_ok=True)
        return Path(path)

    def test_loads_and_sorts_valid_servers(self):
        path = self._write_registry(
            {
                "servers": [
                    {
                        "server_key": "server-b",
                        "base_url": "https://b.example/admin",
                        "api_token": "b" * 32,
                        "interface_id": "interface-b",
                        "priority": 20,
                        "max_peers": 100,
                    },
                    {
                        "server_key": "server-a",
                        "base_url": "https://a.example/admin",
                        "api_token": "a" * 32,
                        "interface_id": "interface-a",
                        "priority": 10,
                        "max_peers": 100,
                        "server_name": "Netherlands",
                        "client_group": "Basic",
                        "assignable_client_groups": ["Basic", "Premium"],
                    },
                ]
            }
        )

        servers = load_cascade_servers(path)

        self.assertEqual([server.server_key for server in servers], ["server-a", "server-b"])
        self.assertEqual(servers[0].server_name, "Netherlands")
        self.assertEqual(servers[0].selectable_client_groups, ("Basic", "Premium"))
        self.assertEqual(servers[1].server_name, "server-b")
        self.assertEqual(servers[1].selectable_client_groups, ("Basic",))

    def test_rejects_duplicate_assignable_groups(self):
        path = self._write_registry(
            {
                "servers": [
                    {
                        "server_key": "server-a",
                        "base_url": "https://a.example/admin",
                        "api_token": "a" * 32,
                        "interface_id": "interface-a",
                        "priority": 10,
                        "max_peers": 100,
                        "client_group": "Basic",
                        "assignable_client_groups": ["Basic", "basic"],
                    }
                ]
            }
        )
        with self.assertRaisesRegex(CascadeError, "unique printable"):
            load_cascade_servers(path)

    def test_rejects_invalid_server_name(self):
        path = self._write_registry(
            {
                "servers": [
                    {
                        "server_key": "server-a",
                        "server_name": "Invalid\nName",
                        "base_url": "https://a.example/admin",
                        "api_token": "a" * 32,
                        "interface_id": "interface-a",
                        "priority": 10,
                        "max_peers": 100,
                    }
                ]
            }
        )

        with self.assertRaisesRegex(CascadeError, "server_name"):
            load_cascade_servers(path)

    def test_rejects_http_when_tls_verification_is_enabled(self):
        path = self._write_registry(
            {
                "servers": [
                    {
                        "server_key": "server-a",
                        "base_url": "http://cascade.internal/admin",
                        "api_token": "a" * 32,
                        "interface_id": "interface-a",
                        "priority": 10,
                        "max_peers": 100,
                    }
                ]
            }
        )

        with self.assertRaisesRegex(CascadeError, "HTTPS is required"):
            load_cascade_servers(path)

    def test_rejects_string_boolean_values(self):
        path = self._write_registry(
            {
                "servers": [
                    {
                        "server_key": "server-a",
                        "base_url": "https://a.example/admin",
                        "api_token": "a" * 32,
                        "interface_id": "interface-a",
                        "priority": 10,
                        "max_peers": 100,
                        "enabled": "false",
                    }
                ]
            }
        )

        with self.assertRaisesRegex(CascadeError, "must be JSON booleans"):
            load_cascade_servers(path)


class CascadeAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_peer_not_found_400_is_normalized(self):
        def handler(request):
            return httpx.Response(400, json={"error": "peer not found"})

        server = CascadeServer(
            server_key="server-a",
            base_url="https://vpn.example/admin",
            api_token="token",
            interface_id="interface-a",
            priority=1,
            max_peers=10,
        )
        api = CascadeAPI(server)
        await api.client.aclose()
        api.client = httpx.AsyncClient(
            base_url=server.api_url.rstrip("/") + "/",
            transport=httpx.MockTransport(handler),
        )
        try:
            with self.assertRaises(CascadeNotFound):
                await api.update_expiry("missing-peer", "2030-01-01 00:00:00")
        finally:
            await api.close()

    async def test_native_interface_import_uses_documented_payload(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(
                201,
                json={
                    "interface": {"id": "migration-if"},
                    "peersCreated": 47,
                    "started": True,
                },
            )

        server = CascadeServer(
            server_key="server-a",
            base_url="https://vpn.example/hidden-admin",
            api_token="token",
            interface_id="interface-a",
            priority=1,
            max_peers=100,
        )
        api = CascadeAPI(server)
        await api.client.aclose()
        api.client = httpx.AsyncClient(
            base_url=server.api_url.rstrip("/") + "/",
            transport=httpx.MockTransport(handler),
        )
        try:
            result = await api.import_interface('{"interface":{},"peers":[]}', 51900)
            payload = json.loads(requests[0].content)
            self.assertEqual(
                requests[0].url.path,
                "/hidden-admin/api/tunnel-interfaces/import-interface",
            )
            self.assertEqual(payload["listenPort"], 51900)
            self.assertIn('"interface"', payload["json"])
            self.assertEqual(result["interface"]["id"], "migration-if")
        finally:
            await api.close()

    async def test_create_peer_assigns_configured_client_group(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.path.endswith("/aliases/client-groups"):
                return httpx.Response(
                    200,
                    json={"groups": [{"id": "basic-id", "name": "Basic"}]},
                )
            return httpx.Response(
                201,
                json={
                    "peer": {
                        "id": "peer-id",
                        "name": "alice",
                        "publicKey": "public-key",
                    }
                },
            )

        server = CascadeServer(
            server_key="server-a",
            base_url="https://vpn.example/hidden-admin",
            api_token="token",
            interface_id="interface-a",
            priority=1,
            max_peers=10,
            client_group="Basic",
        )
        api = CascadeAPI(server)
        await api.client.aclose()
        api.client = httpx.AsyncClient(
            base_url=server.api_url.rstrip("/") + "/",
            transport=httpx.MockTransport(handler),
        )
        try:
            await api.create_peer("alice", "2030-01-01 00:00:00")
            payload = __import__("json").loads(requests[-1].content)
            self.assertEqual(payload["groupId"], "basic-id")
        finally:
            await api.close()

    async def test_create_and_update_peer_use_selected_group(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.path.endswith("/aliases/client-groups"):
                return httpx.Response(
                    200,
                    json={
                        "groups": [
                            {"id": "basic-id", "name": "Basic"},
                            {"id": "premium-id", "name": "Premium"},
                        ]
                    },
                )
            if request.method == "POST":
                return httpx.Response(
                    201,
                    json={
                        "peer": {
                            "id": "peer-id",
                            "name": "alice_phone",
                            "publicKey": "public-key",
                        }
                    },
                )
            return httpx.Response(200, json={"peer": {"id": "peer-id"}})

        server = CascadeServer(
            "server-a",
            "https://vpn.example/admin",
            "token",
            "interface-a",
            1,
            10,
            assignable_client_groups=("Basic", "Premium"),
        )
        api = CascadeAPI(server)
        await api.client.aclose()
        api.client = httpx.AsyncClient(
            base_url=server.api_url.rstrip("/") + "/",
            transport=httpx.MockTransport(handler),
        )
        try:
            await api.create_peer(
                "alice_phone",
                "2030-01-01 00:00:00",
                client_group="Premium",
            )
            await api.update_client_group("peer-id", "Basic")
            payloads = [json.loads(request.content) for request in requests if request.content]
            self.assertEqual(payloads[0]["groupId"], "premium-id")
            self.assertEqual(payloads[1], {"groupId": "basic-id"})
        finally:
            await api.close()

    async def test_hidden_admin_path_is_preserved(self):
        server = CascadeServer(
            server_key="server-a",
            base_url="https://vpn.example/hidden-admin",
            api_token="token",
            interface_id="interface-a",
            priority=1,
            max_peers=10,
        )
        api = CascadeAPI(server)
        try:
            request = api.client.build_request("GET", "health")
            self.assertEqual(
                str(request.url), "https://vpn.example/hidden-admin/api/health"
            )
        finally:
            await api.close()

    async def test_missing_managed_config_is_not_restored_automatically(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        db = Database(path)
        db.save_client_peer(
            10, "server-a", "if-a", "old-peer", "old-key", "alice", "primary"
        )
        db.ensure_subscription(
            10, "alice", "2030-01-01 00:00:00", "paid", "30_days", "stars"
        )
        old_primary = db.get_managed_client_configs(10)[0]
        db.rename_managed_config(old_primary["id"], 10, "Ноутбук")
        servers = [
            CascadeServer("server-a", "https://a.test/admin", "a", "if-a", 1, 2),
            CascadeServer("server-b", "https://b.test/admin", "b", "if-b", 2, 3),
        ]
        api_a = ProvisioningCascadeAPI()
        api_a.download_config = AsyncMock(side_effect=CascadeNotFound("missing"))
        api_b = ProvisioningCascadeAPI()
        router = CascadeRouter(db, servers=[])
        router.servers = servers
        router.apis = {"server-a": api_a, "server-b": api_b}
        try:
            with self.assertRaises(CascadeNotFound):
                await router.get_managed_config(10, old_primary["id"])
            self.assertEqual(api_a.created, 0)
            self.assertEqual(api_b.created, 0)
            primary = db.get_managed_client_configs(10)[0]
            self.assertEqual(primary["server_key"], "server-a")
            self.assertEqual(primary["cascade_peer_id"], "old-peer")
            self.assertEqual(primary["config_name"], "Ноутбук")
            self.assertEqual(primary["enabled"], 0)
            self.assertEqual(db.get_peer_count(10), 1)
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_additional_config_is_created_on_selected_interface(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        db = Database(path)
        db.activate_new_access(10, "alice", 30, "30_days", "stars")
        class AdditionalAPI:
            def __init__(self):
                self.created_interface = None
                self.created_group = None
                self.disabled = []
                self.deleted = []

            async def list_interfaces(self):
                return [
                    {"id": "if-b", "name": "Mobile"},
                    {"id": "if-c", "name": "Tablet"},
                ]

            async def list_peers(self, interface_id=None):
                return []

            async def create_peer(
                self, name, expired_at, interface_id=None, client_group=None
            ):
                self.created_interface = interface_id
                self.created_group = client_group
                return {
                    "id": "additional",
                    "name": name,
                    "publicKey": "additional-key",
                }

            async def download_config(self, peer_id, interface_id=None):
                return b"config"

            async def disable_peer(self, peer_id, interface_id=None):
                self.disabled.append(peer_id)

            async def delete_peer(self, peer_id, interface_id=None):
                self.deleted.append(peer_id)

        server = CascadeServer(
            "server-b", "https://b.test/admin", "token", "if-b", 1, 10
        )
        api = AdditionalAPI()
        router = CascadeRouter(db, servers=[])
        router.servers = [server]
        router.apis = {"server-b": api}
        try:
            config, content = await router.create_managed_config(
                10, "Телефон", "server-b", "if-c"
            )
            self.assertEqual(api.created_interface, "if-c")
            self.assertEqual(api.created_group, server.client_group)
            self.assertEqual(config["role"], "managed")
            self.assertEqual(config["config_name"], "Телефон")
            self.assertEqual(config["server_key"], "server-b")
            self.assertEqual(content, b"config")
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_admin_download_allows_paid_disabled_config_without_enabling_it(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        db = Database(path)
        db.ensure_subscription(
            10, "alice", "2030-01-01 00:00:00", "paid", "30_days", "stars"
        )
        db.save_client_peer(
            10,
            "server-a",
            "if-a",
            "peer-a",
            "key-a",
            "alice",
            "additional",
            enabled=False,
            config_name="Old phone",
            admin_enabled=False,
        )
        peer_id = db.get_managed_client_configs(10)[0]["id"]

        class DownloadAPI:
            async def download_config(self, cascade_peer_id, interface_id=None):
                self.request = (cascade_peer_id, interface_id)
                return b"paid-config"

        router = CascadeRouter(db, servers=[])
        api = DownloadAPI()
        router.apis = {"server-a": api}
        try:
            peer, content = await router.get_admin_managed_config(10, peer_id)
            self.assertEqual(content, b"paid-config")
            self.assertEqual(api.request, ("peer-a", "if-a"))
            self.assertEqual(peer["admin_enabled"], 0)
            self.assertEqual(db.get_client_peer(peer_id, 10)["enabled"], 0)
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_admin_download_marks_missing_peer_unavailable_without_restoring(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        db = Database(path)
        db.ensure_subscription(
            10, "alice", "2030-01-01 00:00:00", "paid", "30_days", "stars"
        )
        db.save_client_peer(
            10, "server-a", "if-a", "peer-a", "key-a", "alice", "primary"
        )
        peer_id = db.get_managed_client_configs(10)[0]["id"]

        class MissingAPI:
            async def download_config(self, cascade_peer_id, interface_id=None):
                raise CascadeNotFound("missing")

        router = CascadeRouter(db, servers=[])
        router.apis = {"server-a": MissingAPI()}
        try:
            with self.assertRaises(CascadeNotFound):
                await router.get_admin_managed_config(10, peer_id)
            self.assertEqual(db.get_client_peer(peer_id, 10)["enabled"], 0)
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_sync_does_not_reenable_admin_disabled_config(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        db = Database(path)
        db.upsert_client(10, "alice")
        db.upsert_client(11)
        db.save_client_peer(
            10, "server-a", "if-a", "primary", "primary-key", "alice", "primary"
        )
        db.save_client_peer(
            10,
            "server-b",
            "if-b",
            "additional",
            "additional-key",
            "phone",
            "additional",
            enabled=False,
            config_name="Телефон",
            admin_enabled=False,
        )

        class AccessSyncAPI:
            def __init__(self):
                self.enabled = []
                self.disabled = []

            async def update_expiry(self, peer_id, expire_date, interface_id=None):
                return None

            async def enable_peer(self, peer_id, interface_id=None):
                self.enabled.append(peer_id)

            async def disable_peer(self, peer_id, interface_id=None):
                self.disabled.append(peer_id)

        api_a = AccessSyncAPI()
        api_b = AccessSyncAPI()
        router = CascadeRouter(db, servers=[])
        router.apis = {"server-a": api_a, "server-b": api_b}
        try:
            result = await router.sync_user_access(10, "2030-01-01 00:00:00")
            self.assertEqual(result["updated"], 2)
            self.assertEqual(api_a.enabled, ["primary"])
            self.assertEqual(api_b.enabled, [])
            self.assertEqual(api_b.disabled, ["additional"])
            additional = db.get_managed_client_configs(10)[1]
            self.assertEqual(additional["admin_enabled"], 0)
            self.assertEqual(additional["enabled"], 0)
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_permanent_delete_removes_additional_from_cascade_and_database(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        db = Database(path)
        db.save_client_peer(
            10, "server-a", "if-a", "primary", "key-a", "alice", "primary"
        )
        db.save_client_peer(
            10,
            "server-a",
            "if-a",
            "additional",
            "key-b",
            "alice_phone",
            "additional",
            config_name="Phone",
        )
        primary_id, additional_id = [
            peer["id"] for peer in db.get_managed_client_configs(10)
        ]

        class DeleteAPI:
            def __init__(self):
                self.deleted = []

            async def get_peer(self, peer_id, interface_id=None):
                return {"id": peer_id}

            async def delete_peer(self, peer_id, interface_id=None):
                self.deleted.append((peer_id, interface_id))

        api = DeleteAPI()
        router = CascadeRouter(db, servers=[])
        router.apis = {"server-a": api}
        try:
            with self.assertRaises(CascadeNotFound):
                await router.delete_managed_config(11, additional_id)
            self.assertEqual(api.deleted, [])

            peer, was_missing = await router.delete_managed_config(
                10, additional_id
            )

            self.assertEqual(peer["cascade_peer_id"], "additional")
            self.assertFalse(was_missing)
            self.assertEqual(api.deleted, [("additional", "if-a")])
            self.assertIsNone(db.get_client_peer(additional_id, 10))
            self.assertEqual(len(db.get_managed_client_configs(10)), 1)
            primary, was_missing = await router.delete_managed_config(10, primary_id)
            self.assertFalse(was_missing)
            self.assertEqual(primary["cascade_peer_id"], "primary")
            self.assertEqual(
                api.deleted, [("additional", "if-a"), ("primary", "if-a")]
            )
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_permanent_delete_cleans_stale_local_additional_only(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        db = Database(path)
        db.save_client_peer(
            10,
            "server-a",
            "if-a",
            "missing",
            "key-b",
            "alice_phone",
            "additional",
            config_name="Phone",
        )
        peer_id = db.get_managed_client_configs(10)[0]["id"]

        class MissingAPI:
            async def get_peer(self, cascade_peer_id, interface_id=None):
                raise CascadeNotFound("missing")

            async def delete_peer(self, cascade_peer_id, interface_id=None):
                raise AssertionError("A missing peer must not be deleted again")

        router = CascadeRouter(db, servers=[])
        router.apis = {"server-a": MissingAPI()}
        try:
            _, was_missing = await router.delete_managed_config(10, peer_id)

            self.assertTrue(was_missing)
            self.assertIsNone(db.get_client_peer(peer_id, 10))
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_permanent_delete_preserves_database_on_cascade_failure(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        db = Database(path)
        db.save_client_peer(
            10,
            "server-a",
            "if-a",
            "additional",
            "key-b",
            "alice_phone",
            "additional",
            config_name="Phone",
        )
        peer_id = db.get_managed_client_configs(10)[0]["id"]

        class FailingAPI:
            async def get_peer(self, cascade_peer_id, interface_id=None):
                return {"id": cascade_peer_id}

            async def delete_peer(self, cascade_peer_id, interface_id=None):
                raise CascadeError("server unavailable")

        router = CascadeRouter(db, servers=[])
        router.apis = {"server-a": FailingAPI()}
        try:
            with self.assertRaises(CascadeError):
                await router.delete_managed_config(10, peer_id)

            self.assertIsNotNone(db.get_client_peer(peer_id, 10))
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_client_delete_blocks_active_subscription_before_cascade(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        db = Database(path)
        db.ensure_subscription(
            10, "alice", "2099-01-01 00:00:00", "paid", "30_days", "yookassa"
        )
        db.save_client_peer(
            10, "server-a", "if-a", "peer-a", "key-a", "alice", "managed"
        )

        api = SimpleNamespace(
            get_peer=AsyncMock(return_value={"id": "peer-a"}),
            delete_peer=AsyncMock(),
        )
        router = CascadeRouter(db, servers=[])
        router.apis = {"server-a": api}
        try:
            with self.assertRaises(ActiveSubscriptionError):
                await router.delete_client(10, 99)
            api.get_peer.assert_not_awaited()
            api.delete_peer.assert_not_awaited()
            self.assertIsNotNone(db.get_admin_client_details(10))

            result = await router.delete_client(
                10, 99, allow_active_subscription=True
            )
            self.assertEqual(result.deleted, 1)
            self.assertIsNone(db.get_admin_client_details(10))
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_client_delete_removes_all_roles_across_servers(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        db = Database(path)
        db.save_client_peer(
            10, "server-a", "if-a", "primary", "key-a", "alice", "primary"
        )
        db.save_client_peer(
            10, "server-b", "if-b", "additional", "key-b", "phone", "additional"
        )
        db.save_client_peer(
            10, "server-a", "if-a", "legacy", "key-c", "legacy", "manual"
        )
        db.add_provisioning_task(
            10,
            "delete_cascade_peer",
            {
                "server_key": "server-b",
                "interface_id": "if-orphan",
                "cascade_peer_id": "orphan",
            },
            "cleanup retry",
        )

        class DeleteAPI:
            def __init__(self, missing=()):
                self.missing = set(missing)
                self.deleted = []

            async def get_peer(self, peer_id, interface_id=None):
                if peer_id in self.missing:
                    raise CascadeNotFound("missing")
                return {"id": peer_id}

            async def delete_peer(self, peer_id, interface_id=None):
                self.deleted.append((peer_id, interface_id))

        api_a = DeleteAPI(missing={"legacy"})
        api_b = DeleteAPI()
        router = CascadeRouter(db, servers=[])
        router.apis = {"server-a": api_a, "server-b": api_b}
        try:
            result = await router.delete_client(10, 99)

            self.assertEqual(result.deleted, 3)
            self.assertEqual(result.already_missing, 1)
            self.assertEqual(result.failed, 0)
            self.assertEqual(api_a.deleted, [("primary", "if-a")])
            self.assertEqual(
                api_b.deleted,
                [("additional", "if-b"), ("orphan", "if-orphan")],
            )
            self.assertIsNone(db.get_admin_client_details(10))
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_client_delete_preserves_database_until_retry_succeeds(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        db = Database(path)
        db.save_client_peer(
            10, "server-a", "if-a", "peer-a", "key-a", "alice", "primary"
        )
        db.save_client_peer(
            10, "server-b", "if-b", "peer-b", "key-b", "phone", "additional"
        )

        class DeleteAPI:
            def __init__(self, failing=False):
                self.failing = failing
                self.present = True

            async def get_peer(self, peer_id, interface_id=None):
                if not self.present:
                    raise CascadeNotFound("missing")
                return {"id": peer_id}

            async def delete_peer(self, peer_id, interface_id=None):
                if self.failing:
                    raise CascadeError("unavailable")
                self.present = False

        api_a = DeleteAPI()
        api_b = DeleteAPI(failing=True)
        router = CascadeRouter(db, servers=[])
        router.apis = {"server-a": api_a, "server-b": api_b}
        try:
            first = await router.delete_client(10, 99)

            self.assertEqual(first.deleted, 1)
            self.assertEqual(first.failed, 1)
            self.assertIsNotNone(db.get_admin_client_details(10))

            api_b.failing = False
            second = await router.delete_client(10, 99)

            self.assertEqual(second.deleted, 1)
            self.assertEqual(second.already_missing, 1)
            self.assertEqual(second.failed, 0)
            self.assertIsNone(db.get_admin_client_details(10))
            with db._connect() as conn:
                operations = [
                    row[0]
                    for row in conn.execute(
                        "SELECT operation FROM operation_logs WHERE peer_name='telegram:10'"
                    )
                ]
            self.assertIn("admin_delete_client_failed", operations)
            self.assertIn("admin_delete_client", operations)
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_expired_client_cannot_create_managed_config(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        db = Database(path)
        db.activate_new_access(10, "alice", 30, "30_days", "stars")
        db.save_client_peer(
            10, "server-a", "if-a", "primary", "primary-key", "alice", "primary"
        )
        with db._connect() as conn:
            conn.execute(
                "UPDATE subscriptions SET expire_date='2000-01-01 00:00:00' "
                "WHERE telegram_user_id=10"
            )

        class ExpiredAPI:
            def __init__(self):
                self.created = 0
                self.disabled = []
                self.deleted = []

            async def list_interfaces(self):
                return [{"id": "if-b", "name": "Mobile"}]

            async def list_peers(self, interface_id=None):
                return []

            async def create_peer(self, name, expired_at, interface_id=None):
                self.created += 1
                return {
                    "id": f"additional-{self.created}",
                    "name": name,
                    "publicKey": f"key-{self.created}",
                }

            async def download_config(self, peer_id, interface_id=None):
                return b"config"

            async def disable_peer(self, peer_id, interface_id=None):
                self.disabled.append(peer_id)

            async def delete_peer(self, peer_id, interface_id=None):
                self.deleted.append(peer_id)

        server = CascadeServer(
            "server-b", "https://b.test/admin", "token", "if-b", 1, 10
        )
        api = ExpiredAPI()
        router = CascadeRouter(db, servers=[])
        router.servers = [server]
        router.apis = {"server-b": api}
        try:
            with self.assertRaisesRegex(CascadeError, "Active access"):
                await router.create_managed_config(
                    10, "Телефон", "server-b", "if-b"
                )
            self.assertEqual(api.disabled, [])
            self.assertEqual(api.deleted, [])
            self.assertEqual(len(db.get_managed_client_configs(10)), 1)
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_sync_access_skips_missing_additional_peer(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        db = Database(path)
        db.upsert_client(10, "alice")
        db.save_client_peer(
            10, "server-a", "if-a", "primary-peer", "primary-key", "alice", "primary"
        )
        db.save_client_peer(
            10,
            "server-a",
            "if-a",
            "missing-additional",
            "additional-key",
            "phone",
            "additional",
            enabled=False,
            config_name="Phone",
        )

        class AccessSyncAPI:
            async def update_expiry(self, peer_id, expire_date, interface_id=None):
                if peer_id == "missing-additional":
                    raise CascadeNotFound("peer not found")

            async def enable_peer(self, peer_id, interface_id=None):
                return None

        router = CascadeRouter(db, servers=[])
        router.apis = {"server-a": AccessSyncAPI()}
        try:
            result = await router.sync_user_access(10, "2030-01-01 00:00:00")
            peers = {
                peer["cascade_peer_id"]: peer for peer in db.get_client_peers(10)
            }

            self.assertEqual(
                result, {"total": 2, "updated": 1, "missing": 1, "failed": 0}
            )
            self.assertEqual(peers["primary-peer"]["enabled"], 1)
            self.assertEqual(peers["missing-additional"]["enabled"], 0)
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_banned_client_sync_disables_all_admin_enabled_peers(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        db = Database(path)
        db.ensure_subscription(
            10, "alice", "2099-01-01 00:00:00", "paid", "30_days", "stars"
        )
        db.save_client_peer(
            10, "server-a", "if-a", "primary", "key-a", "alice", "primary"
        )

        class StateAPI:
            def __init__(self):
                self.enabled = []
                self.disabled = []

            async def update_expiry(self, peer_id, expire_date, interface_id=None):
                return None

            async def enable_peer(self, peer_id, interface_id=None):
                self.enabled.append(peer_id)

            async def disable_peer(self, peer_id, interface_id=None):
                self.disabled.append(peer_id)

        api = StateAPI()
        router = CascadeRouter(db, servers=[])
        router.apis = {"server-a": api}
        try:
            result = await router.set_client_ban(10, 99, True, "test")
            self.assertEqual(result["failed"], 0)
            self.assertEqual(api.enabled, [])
            self.assertEqual(api.disabled, ["primary"])
            self.assertTrue(db.is_client_banned(10))

            api.disabled.clear()
            result = await router.set_client_ban(10, 99, False)
            self.assertEqual(result["failed"], 0)
            self.assertEqual(api.enabled, ["primary"])
            self.assertFalse(db.is_client_banned(10))
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

    def test_production_locations_use_configured_interface_only(self):
        server = CascadeServer(
            "server-a",
            "https://a.test/admin",
            "token",
            "production-if",
            1,
            10,
            server_name="Netherlands",
        )
        router = CascadeRouter(Database(":memory:"), servers=[server])
        self.assertEqual(
            router.get_client_production_locations(),
            [
                {
                    "server_key": "server-a",
                    "server_name": "Netherlands",
                    "interface_id": "production-if",
                }
            ],
        )

    async def test_self_service_creation_uses_production_interface_and_limit(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        db = Database(path)
        db.ensure_subscription(
            10, "alice", "2099-01-01 00:00:00", "paid", "30_days", "stars"
        )
        class SelfServiceAPI:
            def __init__(self):
                self.created_interfaces = []
                self.created = 0

            async def list_interfaces(self):
                return [
                    {"id": "production-if"},
                    {"id": "admin-only-if"},
                ]

            async def list_peers(self, interface_id=None):
                return []

            async def list_client_groups(self):
                return [{"id": "basic-id", "name": "Basic"}]

            async def get_peer(self, peer_id, interface_id=None):
                return {"id": peer_id, "groupId": "basic-id"}

            async def resolve_client_group_name(self, group_id):
                return "Basic" if group_id == "basic-id" else None

            async def create_peer(
                self, name, expired_at, interface_id=None, client_group=None
            ):
                self.created += 1
                self.created_interfaces.append(interface_id)
                return {
                    "id": f"additional-{self.created}",
                    "name": name,
                    "publicKey": f"key-{self.created}",
                }

            async def download_config(self, peer_id, interface_id=None):
                return b"config"

            async def delete_peer(self, peer_id, interface_id=None):
                return None

        server = CascadeServer(
            "server-a",
            "https://a.test/admin",
            "token",
            "production-if",
            1,
            10,
            client_group="Basic",
        )
        api = SelfServiceAPI()
        router = CascadeRouter(db, servers=[server])
        router.apis = {"server-a": api}
        try:
            with self.assertRaisesRegex(CascadeError, "production interface"):
                await router.create_managed_config(
                    10,
                    "Forbidden",
                    "server-a",
                    "admin-only-if",
                    self_service_limit=2,
                    production_only=True,
                )
            for name in ("Phone", "Tablet"):
                await router.create_managed_config(
                    10,
                    name,
                    "server-a",
                    "production-if",
                    self_service_limit=2,
                    production_only=True,
                )
            with self.assertRaisesRegex(CascadeError, "limit reached"):
                await router.create_managed_config(
                    10,
                    "Laptop",
                    "server-a",
                    "production-if",
                    self_service_limit=2,
                    production_only=True,
                )
            self.assertEqual(
                api.created_interfaces, ["production-if", "production-if"]
            )
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass
    async def test_readable_additional_peer_name_and_stable_suffix(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        db = Database(path)
        db.upsert_client(10, "alice")

        class NameAPI:
            async def list_peers(self, interface_id=None):
                return [{"name": "alice_Phone"}]

        router = CascadeRouter(db, servers=[])
        router.apis = {"server-a": NameAPI()}
        try:
            collision = await router.build_managed_peer_name(
                10, "Phone", "server-a", "if-a"
            )
            long_name = await router.build_managed_peer_name(
                10, "Очень длинное название конфигурации для телефона", "server-a", "if-a"
            )
            self.assertRegex(collision, r"^alice_Phone-[0-9a-f]{8}$")
            self.assertEqual(
                await router.build_managed_peer_name(
                    11, "Tablet", "server-a", "if-a"
                ),
                "11_Tablet",
            )
            self.assertLessEqual(len(long_name), 50)
            self.assertEqual(
                long_name,
                await router.build_managed_peer_name(
                    10,
                    "Очень длинное название конфигурации для телефона",
                    "server-a",
                    "if-a",
                ),
            )
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_change_client_group_updates_all_peers_without_enabling(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        db = Database(path)
        db.save_client_peer(
            10, "server-a", "if-a", "primary", "key-a", "alice", "primary",
            client_group="Basic"
        )
        db.save_client_peer(
            10, "server-b", "if-b", "additional", "key-b", "phone", "additional",
            enabled=False, config_name="Phone", admin_enabled=False, client_group="Basic"
        )

        class GroupAPI:
            def __init__(self):
                self.groups = {"primary": "basic-id", "additional": "basic-id"}
                self.changes = []

            async def list_client_groups(self):
                return [
                    {"id": "basic-id", "name": "Basic"},
                    {"id": "premium-id", "name": "Premium"},
                ]

            async def get_peer(self, peer_id, interface_id=None):
                return {"id": peer_id, "groupId": self.groups[peer_id]}

            async def resolve_client_group_name(self, group_id):
                return {"basic-id": "Basic", "premium-id": "Premium"}.get(group_id)

            async def update_client_group(self, peer_id, group_name, interface_id=None):
                self.groups[peer_id] = f"{group_name.casefold()}-id"
                self.changes.append((peer_id, group_name))
                return {}

        server_a = CascadeServer(
            "server-a", "https://a.test/admin", "a", "if-a", 1, 10,
            assignable_client_groups=("Basic", "Premium")
        )
        server_b = CascadeServer(
            "server-b", "https://b.test/admin", "b", "if-b", 1, 10,
            assignable_client_groups=("Basic", "Premium")
        )
        api = GroupAPI()
        router = CascadeRouter(db, servers=[])
        router.servers = [server_a, server_b]
        router.apis = {"server-a": api, "server-b": api}
        try:
            self.assertEqual(await router.change_client_group(10, "Premium"), 2)
            configs = db.get_managed_client_configs(10)
            self.assertEqual({item["client_group"] for item in configs}, {"Premium"})
            self.assertEqual(configs[1]["enabled"], 0)
            self.assertEqual(configs[1]["admin_enabled"], 0)
            self.assertEqual(
                api.changes, [("primary", "Premium"), ("additional", "Premium")]
            )
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_inherited_group_verification_never_changes_cascade(self):
        db = SimpleNamespace(
            get_managed_client_configs=lambda _user_id: [
                {"id": 1, "client_group": "Basic"},
                {"id": 2, "client_group": "Basic"},
            ]
        )
        router = CascadeRouter(db, servers=[])

        async def list_groups(_user_id):
            return ["Basic", "Premium"]

        live_groups = iter(["Basic", "Basic"])

        async def read_group(_peer):
            return next(live_groups)

        router.list_assignable_client_groups = list_groups
        router._read_peer_group = read_group
        await router._verify_client_group_unlocked(10, "Basic")

        live_groups = iter(["Basic", "Premium"])
        with self.assertRaisesRegex(CascadeError, "changed during creation"):
            await router._verify_client_group_unlocked(10, "Basic")

    async def test_group_change_rolls_back_peers_changed_before_failure(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        db = Database(path)
        db.save_client_peer(
            10, "server-a", "if-a", "primary", "key-a", "alice", "primary",
            client_group="Basic",
        )
        db.save_client_peer(
            10, "server-b", "if-b", "additional", "key-b", "phone", "additional",
            config_name="Phone", client_group="Basic",
        )

        class GroupAPI:
            def __init__(self, peer_id, fail_premium=False):
                self.peer_id = peer_id
                self.group_id = "basic-id"
                self.fail_premium = fail_premium
                self.changes = []

            async def list_client_groups(self):
                return [
                    {"id": "basic-id", "name": "Basic"},
                    {"id": "premium-id", "name": "Premium"},
                ]

            async def get_peer(self, peer_id, interface_id=None):
                return {"id": peer_id, "groupId": self.group_id}

            async def resolve_client_group_name(self, group_id):
                return {"basic-id": "Basic", "premium-id": "Premium"}.get(group_id)

            async def update_client_group(self, peer_id, group_name, interface_id=None):
                self.changes.append(group_name)
                if group_name == "Premium" and self.fail_premium:
                    raise CascadeError("group update failed")
                self.group_id = f"{group_name.casefold()}-id"

        servers = [
            CascadeServer(
                key, f"https://{key}.test/admin", key, interface, 1, 10,
                assignable_client_groups=("Basic", "Premium"),
            )
            for key, interface in (("server-a", "if-a"), ("server-b", "if-b"))
        ]
        api_a = GroupAPI("primary")
        api_b = GroupAPI("additional", fail_premium=True)
        router = CascadeRouter(db, servers=[])
        router.servers = servers
        router.apis = {"server-a": api_a, "server-b": api_b}
        try:
            with self.assertRaises(CascadeError):
                await router.change_client_group(10, "Premium")
            self.assertEqual(api_a.changes, ["Premium", "Basic"])
            self.assertEqual(api_b.changes, ["Premium"])
            self.assertEqual(
                {peer["client_group"] for peer in db.get_managed_client_configs(10)},
                {"Basic"},
            )
            self.assertEqual(db.get_runtime_stats()["provisioning_pending"], 0)
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_group_reconciliation_only_updates_local_group(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        db = Database(path)
        db.save_client_peer(
            10, "server-a", "if-a", "primary", "key-a", "alice", "primary"
        )

        class ReconcileAPI:
            def __init__(self):
                self.mutations = 0

            async def list_peers(self, interface_id=None):
                return [{"id": "primary", "groupId": "premium-id"}]

            async def resolve_client_group_name(self, group_id):
                return "Premium" if group_id == "premium-id" else None

            async def update_client_group(self, *args, **kwargs):
                self.mutations += 1

        api = ReconcileAPI()
        router = CascadeRouter(db, servers=[])
        router.apis = {"server-a": api}
        try:
            self.assertEqual(
                await router.reconcile_client_groups(),
                {"total": 1, "updated": 1, "unknown": 0},
            )
            self.assertEqual(
                db.get_managed_client_configs(10)[0]["client_group"], "Premium"
            )
            self.assertEqual(api.mutations, 0)
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

if __name__ == "__main__":
    unittest.main()
