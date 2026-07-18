import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from database import Database


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

    def test_extension_uses_current_expiry_for_active_subscription(self):
        self.db.activate_new_access(10, "alice", 30, "30_days", "stars")
        before = self.db.get_peer_by_telegram_id(10)["expire_date"]
        success, after = self.db.extend_access(10, 14)
        self.assertTrue(success)
        self.assertGreater(after, before)

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


class LegacyDatabaseMigrationTests(unittest.TestCase):
    def test_legacy_peer_is_migrated_without_deleting_source_table(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute(
                    """
                    CREATE TABLE peers (
                        id INTEGER PRIMARY KEY,
                        telegram_user_id INTEGER,
                        telegram_username TEXT,
                        peer_name TEXT,
                        peer_id TEXT,
                        expire_date TEXT,
                        payment_status TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO peers(
                        telegram_user_id, telegram_username, peer_name, peer_id,
                        expire_date, payment_status
                    ) VALUES (10, 'alice', 'legacy-alice', 'public-key',
                              '2030-01-01 00:00:00', 'paid')
                    """
                )
            db = Database(path)
            migrated = db.get_peer_by_telegram_id(10)
            self.assertEqual(migrated["telegram_username"], "alice")
            self.assertEqual(migrated["legacy_public_key"], "public-key")
            with closing(sqlite3.connect(path)) as conn, conn:
                self.assertIsNotNone(
                    conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='peers'"
                    ).fetchone()
                )
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except FileNotFoundError:
                    pass


if __name__ == "__main__":
    unittest.main()
