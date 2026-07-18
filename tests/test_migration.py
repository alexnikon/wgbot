import base64
import json
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cascade_api import CascadeNotFound
from database import Database
from migrate_to_cascade import (
    MigrationError,
    SourcePaths,
    apply_bind_transaction,
    build_snapshot,
    cascade_peer_map,
    derive_x25519_public_key,
    import_command,
    load_client_registry,
    native_backup,
)


def wg_key(seed: int) -> str:
    return base64.b64encode(bytes([seed]) * 32).decode()


class MigrationFixture:
    def __init__(self, root: Path):
        self.root = root
        self.wg_database = root / "wgdashboard.db"
        self.jobs_database = root / "wgdashboard_job.db"
        self.bot_database = root / "wgbot.db"
        self.wg_config = root / "awg0.conf"
        self.clients_json = root / "clients.json"
        self.public_keys = [wg_key(index + 20) for index in range(47)]
        self.private_keys = [wg_key(index + 100) for index in range(47)]
        self.server_private_key = wg_key(200)
        self.server_public_key = wg_key(201)
        self.derived = {
            self.server_private_key: self.server_public_key,
            **dict(zip(self.private_keys, self.public_keys, strict=True)),
        }
        self._create_wg_database()
        self._create_jobs_database()
        self._create_bot_database()
        self._create_config()
        self._create_registry()

    @property
    def paths(self) -> SourcePaths:
        return SourcePaths(
            self.wg_database,
            self.jobs_database,
            self.wg_config,
            self.bot_database,
            self.clients_json,
        )

    def _create_wg_database(self):
        connection = sqlite3.connect(self.wg_database)
        for table in ("awg0", "awg0_restrict_access"):
            connection.execute(
                f"""
                CREATE TABLE {table} (
                    id TEXT, private_key TEXT, preshared_key TEXT,
                    allowed_ip TEXT, name TEXT, created_at TEXT
                )
                """
            )
        for index, public_key in enumerate(self.public_keys):
            table = "awg0" if index < 42 else "awg0_restrict_access"
            private_key = "" if index == 46 else self.private_keys[index]
            connection.execute(
                f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?, ?)",
                (
                    public_key,
                    private_key,
                    "",
                    f"10.8.0.{index + 2}/32",
                    f"peer-{index}",
                    "2026-01-01T00:00:00Z",
                ),
            )
        connection.commit()
        connection.close()

    def _create_jobs_database(self):
        connection = sqlite3.connect(self.jobs_database)
        connection.execute(
            """
            CREATE TABLE PeerJobs (
                Peer TEXT, Value TEXT, CreationDate TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO PeerJobs VALUES (?, ?, ?)",
            (self.public_keys[0], "2027-01-01 00:00:00", "2026-01-01"),
        )
        connection.execute(
            "INSERT INTO PeerJobs VALUES (?, ?, ?)",
            (self.public_keys[1], "2027-02-01 00:00:00", "2026-02-01"),
        )
        connection.execute(
            "INSERT INTO PeerJobs VALUES (?, ?, ?)",
            (self.public_keys[2], "2025-01-01 00:00:00", "2025-01-01"),
        )
        connection.commit()
        connection.close()

    def _create_bot_database(self):
        connection = sqlite3.connect(self.bot_database)
        connection.execute(
            """
            CREATE TABLE peers (
                id INTEGER PRIMARY KEY, peer_id TEXT, telegram_user_id INTEGER,
                telegram_username TEXT, expire_date TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO peers VALUES (1, ?, 10, 'alice', '2028-01-01 00:00:00')",
            (self.public_keys[0],),
        )
        connection.commit()
        connection.close()

    def _create_config(self):
        self.wg_config.write_text(
            "\n".join(
                (
                    "[Interface]",
                    f"PrivateKey = {self.server_private_key}",
                    "Address = 10.8.0.1/24",
                    "Address = fe80::1/64",
                    "ListenPort = 47393",
                    "DNS = 1.1.1.1",
                    "MTU = 1420",
                    "Jc = 4",
                    "Jmin = 40",
                    "Jmax = 70",
                    "S1 = 0",
                    "S2 = 0",
                    "H1 = 1",
                    "H2 = 2",
                    "H3 = 3",
                    "H4 = 4",
                    "",
                    "[Peer]",
                    f"PublicKey = {self.public_keys[0]}",
                )
            ),
            encoding="utf-8",
        )

    def _create_registry(self):
        self.clients_json.write_text(
            json.dumps(
                {
                    "clients": [
                        {
                            "telegramId": 10,
                            "username": "alice",
                            "promo": 25,
                            "peers": [
                                {
                                    "role": "bot",
                                    "clientId": "alice",
                                    "publicKey": self.public_keys[0],
                                },
                                {
                                    "role": "manual",
                                    "clientId": "phone",
                                    "publicKey": self.public_keys[1],
                                },
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    def derive(self, private_key: str, _executable: str = "awg") -> str:
        return self.derived[private_key]


class MigrationSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = MigrationFixture(Path(self.temporary.name))

    def tearDown(self):
        self.temporary.cleanup()

    def snapshot(self):
        with patch("migrate_to_cascade.derive_public_key", self.fixture.derive):
            return build_snapshot(self.fixture.paths)

    def test_internal_x25519_matches_known_wireguard_key_pair(self):
        private_key = "dwdtCnMYpX08FsFyUbJmRd9ML4frwJkqsXf7pR25LCo="
        public_key = "hSDwCYkwp1R0i33ctD73Wg2/Og0mOBr066SpjqqbTmo="
        self.assertEqual(derive_x25519_public_key(private_key), public_key)

    def test_reads_active_and_restricted_peers_and_private_keys(self):
        before = {
            path: path.read_bytes()
            for path in (
                self.fixture.wg_database,
                self.fixture.jobs_database,
                self.fixture.bot_database,
            )
        }
        snapshot = self.snapshot()
        self.assertEqual(snapshot["stats"]["peers"], 47)
        self.assertEqual(snapshot["stats"]["enabled"], 42)
        self.assertEqual(snapshot["stats"]["disabled"], 5)
        self.assertEqual(snapshot["stats"]["effective_enabled"], 41)
        self.assertEqual(snapshot["stats"]["effective_disabled"], 6)
        self.assertTrue(
            any(
                item["type"] == "expired-peer-enabled-in-source"
                for item in snapshot["warnings"]
            )
        )
        self.assertEqual(snapshot["stats"]["private_keys"], 46)
        self.assertEqual(
            sum(item["type"] == "missing-private-key" for item in snapshot["warnings"]),
            1,
        )
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_bot_expiry_takes_precedence_over_latest_job(self):
        snapshot = self.snapshot()
        first = snapshot["peers"][0]
        second = snapshot["peers"][1]
        self.assertEqual(first["expired_at"], "2028-01-01T00:00:00Z")
        self.assertEqual(second["expired_at"], "2027-02-01T00:00:00Z")
        self.assertTrue(
            any(
                item["type"] == "expiry-source-difference"
                for item in snapshot["warnings"]
            )
        )

    def test_native_backup_preserves_keys_state_and_basic_group(self):
        payload = native_backup(self.snapshot(), "basic-group-id")
        self.assertEqual(payload["interface"]["privateKey"], self.fixture.server_private_key)
        self.assertEqual(payload["interface"]["address"], "10.8.0.1/24")
        self.assertEqual(payload["interface"]["protocol"], "amneziawg-2.0")
        self.assertEqual(payload["peers"][0]["groupId"], "basic-group-id")
        self.assertTrue(payload["peers"][0]["enabled"])
        self.assertTrue(payload["peers"][1]["enabled"])
        self.assertFalse(payload["peers"][2]["enabled"])
        self.assertFalse(payload["peers"][-1]["enabled"])
        self.assertEqual(payload["peers"][-1]["privateKey"], "")

    def test_primary_key_conflict_blocks_prepare(self):
        data = json.loads(self.fixture.clients_json.read_text(encoding="utf-8"))
        data["clients"][0]["peers"][0]["publicKey"] = self.fixture.public_keys[2]
        self.fixture.clients_json.write_text(json.dumps(data), encoding="utf-8")
        snapshot = self.snapshot()
        self.assertTrue(
            any(item["type"] == "primary-key-conflict" for item in snapshot["conflicts"])
        )

    def test_primary_key_conflict_can_prefer_bot_database(self):
        data = json.loads(self.fixture.clients_json.read_text(encoding="utf-8"))
        data["clients"][0]["peers"][0]["publicKey"] = self.fixture.public_keys[2]
        data["clients"][0]["peers"][1]["publicKey"] = self.fixture.public_keys[0]
        self.fixture.clients_json.write_text(json.dumps(data), encoding="utf-8")
        with patch("migrate_to_cascade.derive_public_key", self.fixture.derive):
            snapshot = build_snapshot(
                self.fixture.paths,
                resolutions={"primary_role_source": {"10": "bot_database"}},
            )
        self.assertFalse(snapshot["conflicts"])
        roles = {item["public_key"]: item["role"] for item in snapshot["peers"]}
        self.assertEqual(roles[self.fixture.public_keys[0]], "primary")
        self.assertEqual(roles[self.fixture.public_keys[2]], "manual")

    def test_private_key_mismatch_is_reported_without_key_value(self):
        self.fixture.derived[self.fixture.private_keys[0]] = self.fixture.public_keys[2]
        snapshot = self.snapshot()
        conflict = next(
            item for item in snapshot["conflicts"] if item["type"] == "private-key-mismatch"
        )
        self.assertNotIn(self.fixture.public_keys[0], json.dumps(conflict))


class ClientRegistryMigrationTests(unittest.TestCase):
    def test_unified_registry_preserves_promo_and_peer_roles(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "clients.json"
            path.write_text(
                json.dumps(
                    {
                        "clients": [
                            {
                                "telegramId": 10,
                                "username": "alice",
                                "promo": 25,
                                "peers": [
                                    {"role": "bot", "publicKey": "primary-key"},
                                    {"role": "manual", "publicKey": "manual-key"},
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            entries = load_client_registry(path)
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0]["promo"], 25)
            self.assertEqual(entries[0]["role"], "primary")
            self.assertEqual(entries[1]["role"], "manual")

    def test_invalid_registry_has_actionable_location(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "clients.json"
            path.write_text('{"clients": [', encoding="utf-8")
            with self.assertRaisesRegex(MigrationError, "line 1, column"):
                load_client_registry(path)


class BindingTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "target.db"
        Database(str(self.database))
        self.peers = [
            {
                "telegram_user_id": 10,
                "telegram_username": "alice",
                "promo": 10,
                "public_key": "key-a",
                "name": "alice",
                "role": "primary",
            },
            {
                "telegram_user_id": 11,
                "telegram_username": "bob",
                "promo": 20,
                "public_key": "key-b",
                "name": "bob",
                "role": "primary",
            },
        ]

    def tearDown(self):
        self.temporary.cleanup()

    def test_bind_is_idempotent(self):
        mapping = {
            "key-a": {"id": "peer-a", "name": "alice", "enabled": True},
            "key-b": {"id": "peer-b", "name": "bob", "enabled": False},
        }
        for _ in range(2):
            self.assertEqual(
                apply_bind_transaction(
                    self.database, self.peers, mapping, "server-a", "migration-if"
                ),
                2,
            )
        connection = sqlite3.connect(self.database)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM clients").fetchone()[0], 2)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM client_peers").fetchone()[0], 2
        )
        connection.close()

    def test_integrity_failure_rolls_back_entire_bind(self):
        mapping = {
            "key-a": {"id": "same-peer", "name": "alice", "enabled": True},
            "key-b": {"id": "same-peer", "name": "bob", "enabled": True},
        }
        with self.assertRaises(sqlite3.IntegrityError):
            apply_bind_transaction(
                self.database, self.peers, mapping, "server-a", "migration-if"
            )
        connection = sqlite3.connect(self.database)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM clients").fetchone()[0], 0)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM client_peers").fetchone()[0], 0
        )
        connection.close()


class ImportRollbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_peer_map_resolves_duplicate_rows_for_same_peer_id(self):
        peer_key = wg_key(11)

        class FakeAPI:
            def __init__(self):
                self.closed = False

            async def list_peers(self, interface_id):
                return [
                    {
                        "id": "peer-1",
                        "publicKey": peer_key,
                        "enabled": True,
                    },
                    {
                        "id": "peer-1",
                        "publicKey": peer_key,
                        "enabled": False,
                    },
                ]

            async def get_peer(self, peer_id, interface_id):
                return {
                    "id": peer_id,
                    "publicKey": peer_key,
                    "enabled": False,
                }

            async def close(self):
                self.closed = True

        api = FakeAPI()
        with (
            patch(
                "migrate_to_cascade.select_server",
                return_value=SimpleNamespace(server_key="server-a"),
            ),
            patch("migrate_to_cascade.CascadeAPI", return_value=api),
        ):
            peers = await cascade_peer_map("server-a", "migration-if")

        self.assertFalse(peers[peer_key]["enabled"])
        self.assertTrue(api.closed)

    async def test_peer_map_treats_missing_expired_detail_as_disabled(self):
        peer_key = wg_key(12)

        class FakeAPI:
            async def list_peers(self, interface_id):
                return [
                    {
                        "id": "expired-peer",
                        "publicKey": peer_key,
                        "enabled": True,
                    }
                ]

            async def get_peer(self, peer_id, interface_id):
                raise CascadeNotFound("expired peer is not in runtime lookup")

            async def close(self):
                return None

        with (
            patch(
                "migrate_to_cascade.select_server",
                return_value=SimpleNamespace(server_key="server-a"),
            ),
            patch("migrate_to_cascade.CascadeAPI", return_value=FakeAPI()),
        ):
            peers = await cascade_peer_map("server-a", "migration-if")

        self.assertFalse(peers[peer_key]["enabled"])

    async def test_existing_interface_with_stale_peer_set_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            payload = Path(temporary) / "payload.json"
            payload.write_text(
                json.dumps(
                    {
                        "interface": {"publicKey": wg_key(10)},
                        "peers": [
                            {
                                "publicKey": wg_key(11),
                                "enabled": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            class FakeAPI:
                async def list_interfaces(self):
                    return [{"id": "stale-if", "publicKey": wg_key(10)}]

                async def list_peers(self, interface_id):
                    self.interface_id = interface_id
                    return []

                async def close(self):
                    return None

            api = FakeAPI()
            args = Namespace(
                payload=payload,
                server_key="server-a",
                listen_port=51900,
                receipt=Path(temporary) / "receipt.json",
                apply=False,
            )
            with (
                patch(
                    "migrate_to_cascade.select_server",
                    return_value=SimpleNamespace(server_key="server-a"),
                ),
                patch("migrate_to_cascade.CascadeAPI", return_value=api),
                self.assertRaisesRegex(MigrationError, "peer set differs"),
            ):
                await import_command(args)
            self.assertEqual(api.interface_id, "stale-if")

    async def test_import_restores_enabled_state_before_writing_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            payload = Path(temporary) / "payload.json"
            receipt = Path(temporary) / "receipt.json"
            peer_key = wg_key(11)
            payload.write_text(
                json.dumps(
                    {
                        "interface": {"publicKey": wg_key(10)},
                        "peers": [{"publicKey": peer_key, "enabled": True}],
                    }
                ),
                encoding="utf-8",
            )

            class FakeAPI:
                def __init__(self):
                    self.enabled = False
                    self.deleted = []

                async def list_interfaces(self):
                    return []

                async def import_interface(self, raw_json, listen_port):
                    return {
                        "interface": {"id": "migration-if"},
                        "peersCreated": 1,
                        "peersFailed": [],
                        "started": True,
                    }

                async def list_peers(self, interface_id):
                    return [
                        {
                            "id": "peer-1",
                            "publicKey": peer_key,
                            "enabled": self.enabled,
                        }
                    ]

                async def enable_peer(self, peer_id, interface_id):
                    self.enabled = True

                async def disable_peer(self, peer_id, interface_id):
                    self.enabled = False

                async def delete_interface(self, interface_id):
                    self.deleted.append(interface_id)

                async def close(self):
                    return None

            api = FakeAPI()
            args = Namespace(
                payload=payload,
                server_key="server-a",
                listen_port=51900,
                receipt=receipt,
                apply=True,
            )
            with (
                patch(
                    "migrate_to_cascade.select_server",
                    return_value=SimpleNamespace(server_key="server-a"),
                ),
                patch("migrate_to_cascade.CascadeAPI", return_value=api),
            ):
                await import_command(args)
            self.assertTrue(api.enabled)
            self.assertEqual(api.deleted, [])
            self.assertTrue(receipt.exists())

    async def test_partial_cascade_import_is_deleted(self):
        with tempfile.TemporaryDirectory() as temporary:
            payload = Path(temporary) / "payload.json"
            receipt = Path(temporary) / "receipt.json"
            payload.write_text(
                json.dumps(
                    {
                        "interface": {"publicKey": wg_key(10)},
                        "peers": [{"name": "peer"}],
                    }
                ),
                encoding="utf-8",
            )

            class FakeAPI:
                def __init__(self):
                    self.deleted = []

                async def list_interfaces(self):
                    return []

                async def import_interface(self, raw_json, listen_port):
                    return {
                        "interface": {"id": "partial-if"},
                        "peersCreated": 0,
                        "peersFailed": ["peer"],
                        "started": False,
                    }

                async def delete_interface(self, interface_id):
                    self.deleted.append(interface_id)

                async def close(self):
                    return None

            api = FakeAPI()
            args = Namespace(
                payload=payload,
                server_key="server-a",
                listen_port=51900,
                receipt=receipt,
                apply=True,
            )
            with (
                patch(
                    "migrate_to_cascade.select_server",
                    return_value=SimpleNamespace(server_key="server-a"),
                ),
                patch("migrate_to_cascade.CascadeAPI", return_value=api),
                self.assertRaisesRegex(MigrationError, "partial interface was removed"),
            ):
                await import_command(args)
            self.assertEqual(api.deleted, ["partial-if"])
            self.assertFalse(receipt.exists())

if __name__ == "__main__":
    unittest.main()
