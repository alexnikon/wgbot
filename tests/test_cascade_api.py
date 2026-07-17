import os
import tempfile
import unittest

from cascade_api import CascadeAPI, CascadeRouter, CascadeServer
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


class CascadeAPITests(unittest.IsolatedAsyncioTestCase):
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
