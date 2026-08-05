import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta

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

    def test_client_operational_delete_preserves_finance_and_audit(self):
        self.db.ensure_subscription(
            10, "alice", "2030-01-01 00:00:00", "paid", "30_days", "stars"
        )
        self.db.save_client_peer(
            10, "server-a", "if-a", "primary", "key-a", "alice", "primary"
        )
        self.db.save_client_peer(
            10, "server-a", "if-a", "legacy", "key-b", "legacy", "manual"
        )
        self.db.create_reservation(10, "server-a", "if-a", 30)
        self.db.add_provisioning_task(
            10, "sync_access", {"expire_date": "2030-01-01 00:00:00"}, "test"
        )
        self.db.set_telegram_ui_panel(10, 10, 100)
        self.db.set_admin_workflow(10, "own", "active", {"value": 1})
        self.db.set_admin_workflow(99, "target", "active", {"user_id": 10})
        self.db.set_admin_workflow(99, "other", "active", {"user_id": 11})
        self.db.add_payment("payment-10", 10, 100, "stars", "14_days")
        self.db.record_star_transaction(
            "stars-10", "incoming", 100, 1, user_id=10
        )
        self.db.log_admin_client_deletion(
            99,
            10,
            "previous_audit",
            deleted=0,
            already_missing=0,
            failed=1,
        )

        counts = self.db.delete_client_operational_data(
            99, 10, deleted=1, already_missing=1
        )

        self.assertEqual(counts["peers"], 2)
        self.assertIsNone(self.db.get_admin_client_details(10))
        self.assertIsNone(self.db.get_active_reservation(10))
        self.assertIsNone(self.db.get_telegram_ui_panel(10))
        self.assertIsNone(self.db.get_admin_workflow(10, "own"))
        self.assertIsNone(self.db.get_admin_workflow(99, "target"))
        self.assertIsNotNone(self.db.get_admin_workflow(99, "other"))
        self.assertIsNotNone(self.db.get_payment_by_id("payment-10"))
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM star_transactions WHERE user_id=10"
                ).fetchone()[0],
                1,
            )
            operations = {
                row[0]
                for row in conn.execute(
                    "SELECT operation FROM operation_logs WHERE peer_name='telegram:10'"
                )
            }
            self.assertIn("previous_audit", operations)
            self.assertIn("admin_delete_client", operations)

        self.db.upsert_client(10, "alice-returned")
        self.assertEqual(
            self.db.get_admin_client_details(10)["telegram_username"],
            "alice-returned",
        )

    def test_client_operational_delete_requires_existing_client(self):
        self.assertIsNone(
            self.db.delete_client_operational_data(
                99, 404, deleted=0, already_missing=0
            )
        )

    def test_client_ban_is_reversible_audited_and_filters_outbound(self):
        self.db.ensure_subscription(
            10, "alice", "2099-01-01 00:00:00", "paid", "30_days", "stars"
        )
        self.assertTrue(self.db.has_active_subscription(10))
        self.assertTrue(self.db.set_client_ban(10, 99, True, "  abuse   report  "))
        self.assertTrue(self.db.is_client_banned(10))
        self.assertNotIn(10, self.db.get_client_telegram_ids())
        details = self.db.get_admin_client_details(10)
        self.assertEqual(details["ban_reason"], "abuse report")
        self.assertEqual(details["banned_by"], 99)

        self.assertTrue(self.db.set_client_ban(10, 99, False))
        self.assertFalse(self.db.is_client_banned(10))
        self.assertIn(10, self.db.get_client_telegram_ids())
        with closing(sqlite3.connect(self.path)) as conn:
            operations = [
                row[0]
                for row in conn.execute(
                    "SELECT operation FROM operation_logs WHERE peer_name='telegram:10'"
                )
            ]
        self.assertEqual(
            operations[-2:], ["admin_ban_client", "admin_unban_client"]
        )

    def test_additional_config_limit_counts_hidden_and_inactive_records(self):
        self.db.upsert_client(10, "alice")
        self.db.save_client_peer(
            10,
            "server-a",
            "if-a",
            "additional-a",
            "key-a",
            "phone",
            "additional",
            enabled=False,
            config_name="Phone",
            admin_enabled=False,
        )
        self.db.save_client_peer(
            10,
            "server-b",
            "if-b",
            "additional-b",
            "key-b",
            "tablet",
            "additional",
            enabled=True,
            config_name="Tablet",
        )
        self.assertEqual(self.db.count_additional_configs(10), 2)
        self.assertEqual(
            [item["config_name"] for item in self.db.get_client_visible_configs(10)],
            ["Tablet"],
        )

    def test_notification_windows_do_not_label_near_expiry_as_tomorrow(self):
        now = datetime.now(UTC).replace(tzinfo=None)
        subscriptions = {
            10: now + timedelta(minutes=30),
            20: now + timedelta(hours=1, minutes=5),
            30: now + timedelta(hours=23, minutes=30),
        }
        for user_id, expire_date in subscriptions.items():
            self.db.ensure_subscription(
                user_id,
                f"user-{user_id}",
                expire_date.strftime("%Y-%m-%d %H:%M:%S"),
                "paid",
            )

        hour_user_ids = {
            row["telegram_user_id"]
            for row in self.db.get_users_for_hour_notification()
        }
        day_user_ids = {
            row["telegram_user_id"]
            for row in self.db.get_users_for_notification(1)
        }

        self.assertEqual(hour_user_ids, {10})
        self.assertEqual(day_user_ids, {30})

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
        subscription = self.db.get_peer_by_telegram_id(10)
        self.assertEqual(subscription["payment_status"], "paid")
        self.assertEqual(subscription["is_active"], 1)

    def test_refund_expires_subscription_immediately(self):
        self.db.activate_new_access(10, "alice", 7, "14_days", "yookassa")
        self.db.add_payment("payment-1", 10, 15000, "yookassa", "14_days")
        self.db.claim_payment_success("payment-1")

        applied = self.db.apply_refund("payment-1", 14)

        self.assertIsNotNone(applied)
        subscription = self.db.get_peer_by_telegram_id(10)
        self.assertEqual(subscription["payment_status"], "expired")
        self.assertEqual(subscription["is_active"], 0)

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
                10,
                "server-a",
                "if-a",
                "peer-b",
                "key-b",
                "phone",
                "additional",
                config_name="Phone",
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

    def test_admin_expiry_change_updates_state_resets_flags_and_audits(self):
        self.db.ensure_subscription(
            10, "alice", "2000-01-01 00:00:00", "expired", "30_days", "stars"
        )
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(
                """
                UPDATE subscriptions SET notification_sent=1,
                    hour_notification_sent=1, expired_notification_sent=1
                WHERE telegram_user_id=10
                """
            )
            payments_before = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]

        result = self.db.set_admin_subscription_expiry(
            99, 10, "2099-01-01 00:00:00"
        )

        self.assertEqual(result["payment_status"], "paid")
        with closing(sqlite3.connect(self.path)) as conn:
            subscription = conn.execute(
                """
                SELECT expire_date, is_active, payment_status,
                       notification_sent, hour_notification_sent,
                       expired_notification_sent
                FROM subscriptions WHERE telegram_user_id=10
                """
            ).fetchone()
            payments_after = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
            operation, details = conn.execute(
                "SELECT operation, details FROM operation_logs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(
            subscription,
            ("2099-01-01 00:00:00", 1, "paid", 0, 0, 0),
        )
        self.assertEqual(payments_after, payments_before)
        self.assertEqual(operation, "admin_set_expire_date")
        self.assertIn('"admin_id": 99', details)
        self.assertIn('"old_expire_date": "2000-01-01 00:00:00"', details)

    def test_admin_expiry_change_expires_and_requires_subscription(self):
        self.db.upsert_client(10, "alice")
        self.assertIsNone(
            self.db.set_admin_subscription_expiry(
                99, 10, "2099-01-01 00:00:00"
            )
        )
        self.db.ensure_subscription(
            10, "alice", "2099-01-01 00:00:00", "paid", "30_days", "stars"
        )

        result = self.db.set_admin_subscription_expiry(
            99, 10, "2000-01-01 00:00:00"
        )

        self.assertEqual(result["payment_status"], "expired")
        subscription = self.db.get_peer_by_telegram_id(10)
        self.assertEqual(subscription["is_active"], 0)
        self.assertEqual(subscription["payment_status"], "expired")

    def test_user_peers_cannot_span_multiple_servers(self):
        self.assertTrue(
            self.db.save_client_peer(
                10, "server-a", "if-a", "peer-a", "key-a", "alice", "primary"
            )
        )
        self.assertFalse(
            self.db.save_client_peer(
                10, "server-b", "if-b", "peer-b", "key-b", "phone", "primary"
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

    def test_admin_managed_config_includes_paid_expired_and_disabled_peer(self):
        self.db.ensure_subscription(
            10,
            "alice",
            "2000-01-01 00:00:00",
            "paid",
            "30_days",
            "stars",
        )
        self.assertTrue(
            self.db.save_client_peer(
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
        )
        peer_id = self.db.get_managed_client_configs(10)[0]["id"]

        config = self.db.get_admin_managed_config(peer_id, 10)

        self.assertEqual(config["payment_status"], "paid")
        self.assertEqual(config["enabled"], 0)
        self.assertEqual(config["admin_enabled"], 0)
        self.assertIsNone(self.db.get_admin_managed_config(peer_id, 11))

    def test_permanent_config_delete_is_owner_bound_and_protects_primary(self):
        self.db.save_client_peer(
            10, "server-a", "if-a", "primary", "key-a", "alice", "primary"
        )
        self.db.save_client_peer(
            10,
            "server-a",
            "if-a",
            "additional",
            "key-b",
            "alice_phone",
            "additional",
            config_name="Phone",
        )
        primary, additional = self.db.get_managed_client_configs(10)

        self.assertFalse(self.db.delete_additional_config(additional["id"], 11))
        self.assertFalse(self.db.delete_additional_config(primary["id"], 10))
        self.assertTrue(self.db.delete_additional_config(additional["id"], 10))
        self.assertIsNone(self.db.get_client_peer(additional["id"], 10))
        self.assertIsNotNone(self.db.get_client_peer(primary["id"], 10))

    def test_schema_migration_names_existing_primary_peer(self):
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
                    role TEXT NOT NULL DEFAULT 'primary',
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
                    (10, 'server-a', 'if-a', 'primary', 'key-a', 'alice', 'primary');
                """
            )
        migrated = Database(legacy_path)
        primary = migrated.get_primary_client_peer(10)
        self.assertEqual(primary["config_name"], DEFAULT_PRIMARY_CONFIG_NAME)
        self.assertEqual(primary["admin_enabled"], 1)
        self.assertIsNone(primary["client_group"])

    def test_client_group_is_stored_per_peer_and_summarized_for_admin(self):
        self.db.save_client_peer(
            10,
            "server-a",
            "if-a",
            "primary",
            "key-a",
            "alice",
            "primary",
            client_group="Basic",
        )
        self.db.save_client_peer(
            10,
            "server-b",
            "if-b",
            "additional",
            "key-b",
            "phone",
            "additional",
            config_name="Phone",
            client_group="Premium",
        )

        details = self.db.get_admin_client_details(10)

        self.assertEqual(details["client_groups"], "Basic, Premium")
        self.assertEqual(details["unknown_group_count"], 0)
        self.assertEqual(self.db.set_client_peer_groups(10, "Premium"), 2)
        self.assertEqual(
            {peer["client_group"] for peer in self.db.get_managed_client_configs(10)},
            {"Premium"},
        )

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
