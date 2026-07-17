import os
import sqlite3
import tempfile
import unittest

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
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "UPDATE server_reservations SET expires_at='2000-01-01 00:00:00'"
            )
        self.assertEqual(self.db.cleanup_expired_reservations(), 1)
        self.assertIsNone(self.db.get_active_reservation(10))

    def test_payment_success_is_claimed_once(self):
        self.assertTrue(self.db.add_payment("payment-1", 10, 100, "stars", "14_days"))
        self.assertTrue(self.db.claim_payment_success("payment-1"))
        self.assertFalse(self.db.claim_payment_success("payment-1"))

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

    def test_promo_factor_comes_from_database(self):
        self.db.set_client_promo(10, 25)
        self.assertEqual(self.db.get_user_promo_factor(10), 0.75)

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


class LegacyDatabaseMigrationTests(unittest.TestCase):
    def test_legacy_peer_is_migrated_without_deleting_source_table(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            with sqlite3.connect(path) as conn:
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
            with sqlite3.connect(path) as conn:
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
