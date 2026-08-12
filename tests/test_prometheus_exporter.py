import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families

from database import Database
from prometheus_exporter import WGBotCollector, create_metrics_app
from runtime_metrics import RuntimeMetrics


def samples_by_name(payload: str) -> dict[str, list]:
    return {
        family.name: list(family.samples)
        for family in text_string_to_metric_families(payload)
    }


class PrometheusDatabaseSnapshotTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = Database(self.path)
        self._seed_database()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except FileNotFoundError:
                pass

    def _seed_database(self):
        with sqlite3.connect(self.path) as conn:
            conn.executemany(
                """
                INSERT INTO clients(
                    telegram_user_id, telegram_username, is_banned,
                    is_complimentary, identity_verified, telegram_reachable
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    (1, "alice", 0, 0, 1, 1),
                    (2, "bob", 0, 0, 1, 0),
                    (3, "carol", 0, 1, 1, None),
                    (4, "dave", 1, 0, 1, 1),
                    (5, "eve", 0, 0, 1, None),
                    (6, "frank", 0, 0, 0, None),
                ),
            )
            conn.executemany(
                """
                INSERT INTO subscriptions(
                    telegram_user_id, expire_date, is_active, payment_status,
                    tariff_key, payment_method
                ) VALUES (?, datetime('now', ?), ?, ?, ?, ?)
                """,
                (
                    (1, "+12 hours", 1, "paid", "14_days", "yookassa"),
                    (2, "+5 days", 1, "paid", "30_days", "stars"),
                    (4, "+10 days", 1, "paid", "30_days", "yookassa"),
                    (5, "-1 day", 0, "expired", "14_days", "stars"),
                    (6, "+20 days", 1, "paid", "90_days", "yookassa"),
                ),
            )
            conn.executemany(
                """
                INSERT INTO client_peers(
                    telegram_user_id, server_key, interface_id, cascade_peer_id,
                    public_key, role, enabled, admin_enabled
                ) VALUES (?, ?, ?, ?, ?, 'managed', ?, ?)
                """,
                (
                    (2, "server-a", "if-a", "peer-2a", "key-2a", 1, 1),
                    (2, "server-a", "if-a", "peer-2b", "key-2b", 0, 1),
                    (3, "server-a", "if-a", "peer-3a", "key-3a", 1, 1),
                    (3, "server-b", "if-b", "peer-3b", "key-3b", 1, 1),
                    (4, "server-b", "if-b", "peer-4", "key-4", 1, 1),
                    (5, "server-b", "if-b", "peer-5", "key-5", 1, 1),
                ),
            )
            conn.executemany(
                """
                INSERT INTO payments(
                    payment_id, user_id, amount, currency, status,
                    payment_method, tariff_key, refunded_amount
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ("y-1", 1, 15000, "RUB", "succeeded", "yookassa", "14_days", 0),
                    ("y-2", 4, 30000, "RUB", "refunded", "yookassa", "30_days", 0),
                    ("y-3", 6, 99900, "RUB", "pending", "yookassa", "90_days", 0),
                    ("s-1", 2, 100, "XTR", "succeeded", "stars", "14_days", 0),
                    ("s-2", 5, 200, "XTR", "refunded", "stars", "30_days", 50),
                ),
            )
            conn.execute(
                """
                INSERT INTO star_transactions(
                    transaction_id, direction, amount, occurred_at, status
                ) VALUES ('discrepancy-1', 'incoming', 10, 1, 'discrepancy')
                """
            )

    def test_snapshot_aggregates_finance_access_expiry_and_placement(self):
        snapshot = self.db.get_prometheus_metrics_snapshot()

        self.assertEqual(
            snapshot["clients"],
            {
                "paid": 4,
                "paid_blocked": 2,
                "paid_access": 2,
                "complimentary_access": 1,
                "active_without_config": 1,
            },
        )
        self.assertEqual(
            snapshot["expiry"]["windows"], {1: 1, 3: 1, 7: 2, 14: 2, 30: 2}
        )
        self.assertIsInstance(snapshot["expiry"]["nearest"], int)
        self.assertEqual(snapshot["amounts"]["yookassa_received"], 450.0)
        self.assertEqual(snapshot["amounts"]["yookassa_refunded"], 300.0)
        self.assertEqual(snapshot["amounts"]["stars_received"], 300.0)
        self.assertEqual(snapshot["amounts"]["stars_refunded"], 50.0)
        self.assertIn(
            {"server_key": "server-b", "access": "inactive", "count": 2},
            snapshot["server_clients"],
        )
        self.assertIn(
            {"server_key": "server-a", "state": "disabled", "count": 1},
            snapshot["server_configs"],
        )
        self.assertEqual(snapshot["operational"]["stars_discrepancies"], 1)


class PrometheusExporterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = Database(self.path)
        self.db.ensure_subscription(
            10, "private_username", "2099-01-01 00:00:00", "paid", "30_days", "stars"
        )
        self.metrics = RuntimeMetrics()
        self.metrics.record_cascade("server-a", 0.25, True)
        self.services = SimpleNamespace(
            db=self.db, metrics=self.metrics, runtime_ready=True
        )

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except FileNotFoundError:
                pass

    def _payload(self) -> str:
        registry = CollectorRegistry(auto_describe=False)
        registry.register(WGBotCollector(self.services))
        return generate_latest(registry).decode()

    def test_exposition_is_parseable_and_contains_no_personal_labels(self):
        payload = self._payload()
        families = samples_by_name(payload)

        self.assertIn("wgbot_paid_clients", families)
        self.assertIn("wgbot_payments_completed", families)
        self.assertIn("wgbot_metrics_collection_success", families)
        self.assertIn("# HELP wgbot_server_clients", payload)
        self.assertIn("wgbot_payments_completed_total", payload)
        self.assertIn("wgbot_yookassa_received_rubles_total", payload)
        self.assertNotIn("private_username", payload)
        self.assertNotIn("telegram_user_id", payload)
        self.assertNotIn("payment_id", payload)

    async def test_http_endpoint_uses_prometheus_content_type(self):
        transport = httpx.ASGITransport(app=create_metrics_app(self.services))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://metrics"
        ) as client:
            response = await client.get("/metrics")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/plain", response.headers["content-type"])
        self.assertIn("wgbot_paid_clients", response.text)

    def test_database_failure_keeps_valid_exposition(self):
        self.services.db = Mock()
        self.services.db.get_prometheus_metrics_snapshot.side_effect = sqlite3.Error(
            "private database detail"
        )

        payload = self._payload()
        families = samples_by_name(payload)

        success = families["wgbot_metrics_collection_success"][0]
        self.assertEqual(success.value, 0)
        self.assertNotIn("private database detail", payload)


if __name__ == "__main__":
    unittest.main()
