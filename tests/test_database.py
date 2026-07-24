import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from database import DEFAULT_PRIMARY_CONFIG_NAME, Database


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = Database(self.path)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except FileNotFoundError:
                pass

    def test_reservations_are_counted_and_released(self):
        self.db.create_reservation(10, "server-a", "interface-a", 30)
        self.assertEqual(self.db.count_active_reservations("server-a"), 1)
        self.assertEqual(self.db.get_active_reservation(10)["server_key"], "server-a")
        self.db.release_reservation(10)
        self.assertEqual(self.db.count_active_reservations("server-a"), 0)

    def test_expired_reservation_is_removed(self):
        self.db.create_reservation(10, "server-a", "interface-a", 30)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(
                "UPDATE server_reservations SET expires_at='2000-01-01 00:00:00'"
            )
        self.assertEqual(self.db.cleanup_expired_reservations(), 1)
        self.assertIsNone(self.db.get_active_reservation(10))

    def test_payment_success_is_claimed_once(self):
        self.assertTrue(self.db.add_payment("payment-1", 10, 100, "stars", "14_days"))
        self.assertTrue(self.db.claim_payment_success("payment-1"))
        self.assertFalse(self.db.claim_payment_success("payment-1"))

    def test_verified_payment_updates_subscription_atomically(self):
        self.db.add_payment("pay-atomic", 77, 12500, "yookassa", "14_days")
        result = self.db.apply_verified_payment(
            "pay-atomic", 77, "alice", 12500, "yookassa", "14_days", 14
        )
        self.assertIsNotNone(result)
        self.assertFalse(result["is_extension"])
        self.assertEqual(self.db.get_payment_by_id("pay-atomic")["status"], "succeeded")
        subscription = self.db.get_peer_by_telegram_id(77)
        self.assertEqual(subscription["payment_status"], "paid")
        self.assertEqual(subscription["rub_paid"], 125)
        self.assertIsNone(
            self.db.apply_verified_payment(
                "pay-atomic", 77, "alice", 12500, "yookassa", "14_days", 14
            )
        )

    def test_refund_is_applied_once(self):
        self.db.activate_new_access(10, "alice", 30, "30_days", "stars")
        self.db.add_payment("payment-1", 10, 100, "stars", "30_days")
        self.db.claim_payment_success("payment-1")
        first = self.db.apply_refund("payment-1", 14)
        second = self.db.apply_refund("payment-1", 14)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_pending_provisioning_task_is_reused(self):
        first = self.db.add_provisioning_task(10, "create_peer", {"value": 1}, "one")
        second = self.db.add_provisioning_task(10, "create_peer", {"value": 2}, "two")
        self.assertEqual(first, second)
        tasks = self.db.get_pending_provisioning_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["payload"], {"value": 2})

    def test_provisioning_task_is_leased_once(self):
        task_id = self.db.add_provisioning_task(
            88, "create_peer", {"peer_name": "alice"}, "test"
        )
        first = self.db.claim_provisioning_tasks("worker-a", 120)
        reused_id = self.db.add_provisioning_task(
            88, "create_peer", {"peer_name": "alice-new"}, "user retry"
        )
        second = self.db.claim_provisioning_tasks("worker-b", 120)
        self.assertEqual([task["id"] for task in first], [task_id])
        self.assertEqual(reused_id, task_id)
        self.assertEqual(second, [])
        self.assertTrue(self.db.renew_provisioning_lease(task_id, "worker-a", 120))
        self.assertFalse(self.db.complete_provisioning_task(task_id, "worker-b"))
        self.db.fail_provisioning_task(task_id, "retry", "worker-a")

    def test_promo_factor_comes_from_database(self):
        self.db.upsert_client(10, "alice")
        self.db.set_client_promo(10, 25)
        self.assertEqual(self.db.get_user_promo_factor(10), 0.75)

    def test_promo_update_requires_existing_client_and_valid_range(self):
        self.assertFalse(self.db.set_client_promo(999, 10))
        self.db.upsert_client(10, "alice")
        self.assertFalse(self.db.set_client_promo(10, -1))
        self.assertFalse(self.db.set_client_promo(10, 91))
        self.assertTrue(self.db.set_client_promo(10, 30))
        self.assertEqual(self.db.get_user_promo_factor(10), 0.7)

    def test_admin_client_search_includes_server_and_device_count(self):
        self.db.activate_new_access(10, "Alice_Test", 30, "30_days", "stars")
        self.assertTrue(
            self.db.save_client_peer(
                10, "server-a", "if-a", "peer-a", "key-a", "alice", "primary"
            )
        )
        self.assertTrue(
            self.db.save_client_peer(
                10, "server-a", "if-a", "peer-b", "key-b", "phone", "manual"
            )
        )
        clients, total = self.db.get_admin_clients_page(0, 8, "alice")
        self.assertEqual(total, 1)
        self.assertEqual(clients[0]["server_key"], "server-a")
        self.assertEqual(clients[0]["interface_id"], "if-a")
        self.assertEqual(clients[0]["peer_name"], "alice")
        self.assertEqual(clients[0]["device_count"], 2)
        self.assertTrue(
            self.db.save_client_peer(
                10,
                "server-b",
                "if-b",
                "peer-c",
                "key-c",
                "tablet",
                "additional",
                config_name="Tablet",
            )
        )
        details = self.db.get_admin_client_details(10)
        self.assertEqual(details["server_keys"], "server-a, server-b")
        by_id, total_by_id = self.db.get_admin_clients_page(0, 8, "10")
        self.assertEqual(total_by_id, 1)
        self.assertEqual(by_id[0]["telegram_user_id"], 10)

    def test_admin_promo_change_is_audited(self):
        self.db.upsert_client(10, "alice")
        self.db.log_admin_promo_change(99, 10, "server-a", 10, 30)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            operation, details = conn.execute(
                "SELECT operation, details FROM operation_logs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(operation, "admin_set_discount")
        self.assertIn('"admin_id": 99', details)
        self.assertIn('"new_promo": 30', details)

    def test_user_peers_cannot_span_multiple_servers(self):
        self.assertTrue(
            self.db.save_client_peer(
                10, "server-a", "if-a", "peer-a", "key-a", "alice", "primary"
            )
        )
        self.assertFalse(
            self.db.save_client_peer(
                10, "server-b", "if-b", "peer-b", "key-b", "phone", "manual"
            )
        )

    def test_named_additional_configs_can_span_servers_and_remain_unique(self):
        self.assertTrue(
            self.db.save_client_peer(
                10, "server-a", "if-a", "peer-a", "key-a", "alice", "primary"
            )
        )
        self.assertTrue(
            self.db.save_client_peer(
                10,
                "server-b",
                "if-b",
                "peer-b",
                "key-b",
                "phone",
                "additional",
                config_name="Телефон",
            )
        )
        self.assertFalse(
            self.db.save_client_peer(
                10,
                "server-c",
                "if-c",
                "peer-c",
                "key-c",
                "tablet",
                "additional",
                config_name="телефон",
            )
        )
        configs = self.db.get_managed_client_configs(10)
        self.assertEqual(
            [config["config_name"] for config in configs],
            [DEFAULT_PRIMARY_CONFIG_NAME, "Телефон"],
        )
        additional = configs[1]
        self.assertTrue(
            self.db.set_config_admin_enabled(additional["id"], 10, False)
        )
        self.db.set_client_peer_enabled("peer-b", False)
        self.assertEqual(len(self.db.get_managed_client_configs(10)), 2)
        self.assertEqual(
            len(self.db.get_managed_client_configs(10, available_only=True)), 1
        )

    def test_schema_migration_names_primary_but_not_manual_peer(self):
        handle, legacy_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.addCleanup(
            lambda: [
                os.path.exists(legacy_path + suffix)
                and os.remove(legacy_path + suffix)
                for suffix in ("", "-wal", "-shm")
            ]
        )
        with closing(sqlite3.connect(legacy_path)) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE client_peers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id INTEGER NOT NULL,
                    server_key TEXT,
                    interface_id TEXT,
                    cascade_peer_id TEXT,
                    public_key TEXT NOT NULL DEFAULT '',
                    peer_name TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT 'manual',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(server_key, interface_id, cascade_peer_id),
                    UNIQUE(telegram_user_id, public_key)
                );
                INSERT INTO client_peers(
                    telegram_user_id, server_key, interface_id, cascade_peer_id,
                    public_key, peer_name, role
                ) VALUES
                    (10, 'server-a', 'if-a', 'primary', 'key-a', 'alice', 'primary'),
                    (10, 'server-a', 'if-a', 'manual', 'key-b', 'phone', 'manual');
                """
            )
        migrated = Database(legacy_path)
        peers = {peer["role"]: peer for peer in migrated.get_client_peers(10)}
        self.assertEqual(peers["primary"]["config_name"], DEFAULT_PRIMARY_CONFIG_NAME)
        self.assertIsNone(peers["manual"]["config_name"])
        self.assertEqual(peers["primary"]["admin_enabled"], 1)

    def test_extension_uses_current_expiry_for_active_subscription(self):
        self.db.activate_new_access(10, "alice", 30, "30_days", "stars")
        before = self.db.get_peer_by_telegram_id(10)["expire_date"]
        success, after = self.db.extend_access(10, 14)
        self.assertTrue(success)
        self.assertGreater(after, before)

    def test_expiration_sync_updates_subscription_and_local_peer_state(self):
        self.db.activate_new_access(10, "alice", 30, "30_days", "stars")
        self.assertTrue(
            self.db.save_client_peer(
                10, "server-a", "if-a", "peer-a", "key-a", "alice", "primary"
            )
        )
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(
                "UPDATE subscriptions SET expire_date='2000-01-01 00:00:00' "
                "WHERE telegram_user_id=10"
            )

        self.assertEqual(self.db.sync_expired_access_statuses(), 1)

        subscription = self.db.get_peer_by_telegram_id(10)
        self.assertEqual(subscription["payment_status"], "expired")
        self.assertEqual(subscription["enabled"], 0)

    def test_runtime_stats_report_queue_and_subscription_gauges(self):
        self.db.activate_new_access(10, "alice", 30, "30_days", "stars")
        self.db.create_reservation(11, "server-a", "interface-a", 30)
        self.db.add_provisioning_task(
            10, "sync_access", {"expire_date": "2030"}, "test task"
        )

        stats = self.db.get_runtime_stats()

        self.assertEqual(stats["clients"], 1)
        self.assertEqual(stats["active_subscriptions"], 1)
        self.assertEqual(stats["provisioning_pending"], 1)
        self.assertEqual(stats["provisioning_running"], 0)
        self.assertEqual(stats["provisioning_failed"], 0)
        self.assertEqual(stats["active_reservations"], 1)


if __name__ == "__main__":
    unittest.main()
