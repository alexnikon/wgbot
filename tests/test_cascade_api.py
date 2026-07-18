import json
import os
import tempfile
import unittest
from pathlib import Path

import httpx

from cascade_api import (
    CascadeAPI,
    CascadeError,
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
            self.assertEqual(db.get_peer_count(10), 1)
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass


if __name__ == "__main__":
    unittest.main()
