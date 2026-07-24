import json
import os
import tempfile
import unittest
from pathlib import Path

import httpx

from cascade_api import (
    CascadeAPI,
    CascadeError,
    CascadeNotFound,
    CascadeRouter,
    CascadeServer,
    load_cascade_servers,
)
from database import Database


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
                    },
                ]
            }
        )

        servers = load_cascade_servers(path)

        self.assertEqual([server.server_key for server in servers], ["server-a", "server-b"])

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

    async def test_placement_moves_to_next_server_when_first_is_full(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        db = Database(path)
        servers = [
            CascadeServer("server-a", "https://a.test/admin", "a", "if-a", 1, 2),
            CascadeServer("server-b", "https://b.test/admin", "b", "if-b", 2, 3),
        ]
        router = CascadeRouter(db, servers=[])
        router.servers = servers
        router.apis = {
            "server-a": FakeCascadeAPI(peer_count=2),
            "server-b": FakeCascadeAPI(peer_count=1),
        }
        try:
            reservation = await router.ensure_reservation(10)
            self.assertEqual(reservation["server_key"], "server-b")
            self.assertEqual(reservation["interface_id"], "if-b")
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_missing_primary_is_restored_on_assigned_server(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        db = Database(path)
        db.save_client_peer(
            10, "server-a", "if-a", "old-peer", "old-key", "alice", "primary"
        )
        old_primary = db.get_primary_client_peer(10)
        db.rename_managed_config(old_primary["id"], 10, "Ноутбук")
        servers = [
            CascadeServer("server-a", "https://a.test/admin", "a", "if-a", 1, 2),
            CascadeServer("server-b", "https://b.test/admin", "b", "if-b", 2, 3),
        ]
        api_a = ProvisioningCascadeAPI()
        api_b = ProvisioningCascadeAPI()
        router = CascadeRouter(db, servers=[])
        router.servers = servers
        router.apis = {"server-a": api_a, "server-b": api_b}
        try:
            await router.create_user_peer(
                10, "alice", "alice", "2030-01-01 00:00:00"
            )
            self.assertEqual(api_a.created, 1)
            self.assertEqual(api_b.created, 0)
            primary = db.get_primary_client_peer(10)
            self.assertEqual(primary["server_key"], "server-a")
            self.assertEqual(primary["cascade_peer_id"], "new-peer")
            self.assertEqual(primary["config_name"], "Ноутбук")
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
        db.save_client_peer(
            10, "server-a", "if-a", "primary", "primary-key", "alice", "primary"
        )

        class AdditionalAPI:
            def __init__(self):
                self.created_interface = None
                self.disabled = []
                self.deleted = []

            async def list_interfaces(self):
                return [
                    {"id": "if-b", "name": "Mobile"},
                    {"id": "if-c", "name": "Tablet"},
                ]

            async def list_peers(self, interface_id=None):
                return []

            async def create_peer(self, name, expired_at, interface_id=None):
                self.created_interface = interface_id
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
            config = await router.create_additional_config(
                10, "Телефон", "server-b", "if-c"
            )
            self.assertEqual(api.created_interface, "if-c")
            self.assertEqual(config["role"], "additional")
            self.assertEqual(config["config_name"], "Телефон")
            self.assertEqual(config["server_key"], "server-b")
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

    async def test_expired_additional_is_disabled_and_duplicate_is_compensated(self):
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
            config = await router.create_additional_config(
                10, "Телефон", "server-b", "if-b"
            )
            self.assertEqual(config["enabled"], 0)
            self.assertEqual(api.disabled, ["additional-1"])
            with self.assertRaisesRegex(
                CascadeError, "Failed to persist the additional"
            ):
                await router.create_additional_config(
                    10, "телефон", "server-b", "if-b"
                )
            self.assertEqual(api.deleted, ["additional-2"])
            self.assertEqual(len(db.get_managed_client_configs(10)), 2)
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass

    async def test_sync_access_skips_missing_manual_peer(self):
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
            "missing-manual",
            "manual-key",
            "phone",
            "manual",
            enabled=False,
        )

        class AccessSyncAPI:
            async def update_expiry(self, peer_id, expire_date, interface_id=None):
                if peer_id == "missing-manual":
                    raise CascadeNotFound("peer not found")

            async def enable_peer(self, peer_id, interface_id=None):
                return None

        router = CascadeRouter(db, servers=[])
        router.apis = {"server-a": AccessSyncAPI()}
        try:
            result = await router.sync_user_access(10, "2030-01-01 00:00:00")
            peers = {peer["role"]: peer for peer in db.get_client_peers(10)}

            self.assertEqual(
                result, {"total": 2, "updated": 1, "missing": 1, "failed": 0}
            )
            self.assertEqual(peers["primary"]["enabled"], 1)
            self.assertEqual(peers["manual"]["enabled"], 0)
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass


if __name__ == "__main__":
    unittest.main()
