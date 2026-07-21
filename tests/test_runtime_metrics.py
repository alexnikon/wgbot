import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from provisioning import ProvisioningWorker
from runtime_metrics import RuntimeMetrics


class RuntimeMetricsTests(unittest.TestCase):
    def test_snapshot_aggregates_cascade_and_provisioning_metrics(self):
        metrics = RuntimeMetrics()
        metrics.record_cascade("server-a", 0.25, True)
        metrics.record_cascade("server-a", 0.75, False)
        metrics.provisioning_claimed(2)
        metrics.provisioning_completed()
        metrics.provisioning_failed("task-1", ValueError("secret details"))

        snapshot = metrics.snapshot()

        self.assertEqual(snapshot["cascade"]["server-a"]["requests"], 2)
        self.assertEqual(snapshot["cascade"]["server-a"]["errors"], 1)
        self.assertEqual(
            snapshot["cascade"]["server-a"]["average_duration_seconds"], 0.5
        )
        self.assertEqual(snapshot["provisioning"]["claimed"], 2)
        self.assertEqual(snapshot["provisioning"]["completed"], 1)
        self.assertEqual(snapshot["provisioning"]["failed"], 1)
        self.assertEqual(snapshot["last_provisioning_error"]["error_type"], "ValueError")
        self.assertNotIn("secret details", str(snapshot))

    def test_telegram_concurrency_and_lock_gauges(self):
        metrics = RuntimeMetrics()
        metrics.set_telegram_gauge_provider(
            lambda: {"locked_users": 2, "lock_participants": 3}
        )
        metrics.telegram_handler_started(2)
        metrics.telegram_handler_started(2)
        metrics.telegram_handler_finished()

        telegram = metrics.snapshot()["telegram"]
        self.assertEqual(telegram["active_handlers"], 1)
        self.assertEqual(telegram["peak_handlers"], 2)
        self.assertEqual(telegram["saturation_events"], 1)
        self.assertEqual(telegram["locked_users"], 2)

    def test_persisted_telegram_events_are_recorded(self):
        database = SimpleNamespace(record_telegram_daily_metric=unittest.mock.Mock())
        metrics = RuntimeMetrics(database)
        metrics.telegram_event("legacy_callbacks")
        database.record_telegram_daily_metric.assert_called_once_with(
            "legacy_callbacks"
        )


class _ProvisioningDatabase:
    def __init__(self):
        self.completed = []
        self.failed = []

    def get_primary_client_peer(self, user_id):
        return {"telegram_user_id": user_id}

    def complete_provisioning_task(self, task_id, worker_id):
        self.completed.append((task_id, worker_id))
        return True

    def fail_provisioning_task(self, task_id, error, worker_id):
        self.failed.append((task_id, error, worker_id))
        return True

    def renew_provisioning_lease(self, task_id, worker_id, lease_seconds):
        return True


class ProvisioningMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_task_updates_metrics(self):
        database = _ProvisioningDatabase()
        router = AsyncMock()
        router.get_primary_config.return_value = b"config"
        metrics = RuntimeMetrics()
        worker = ProvisioningWorker(
            database,
            router,
            AsyncMock(return_value=True),
            AsyncMock(),
            interval_seconds=60,
            lease_seconds=30,
            metrics=metrics,
        )

        await worker._process(
            {
                "id": "task-1",
                "telegram_user_id": 10,
                "operation": "create_peer",
                "payload": {},
            }
        )

        self.assertEqual(metrics.snapshot()["provisioning"]["completed"], 1)
        self.assertEqual(database.completed[0][0], "task-1")

    async def test_failed_task_updates_metrics_without_exposing_error_message(self):
        database = _ProvisioningDatabase()
        router = AsyncMock()
        router.sync_user_access.side_effect = RuntimeError("sensitive upstream body")
        metrics = RuntimeMetrics()
        worker = ProvisioningWorker(
            database,
            router,
            AsyncMock(),
            AsyncMock(),
            interval_seconds=60,
            lease_seconds=30,
            metrics=metrics,
        )

        await worker._process(
            {
                "id": "task-2",
                "telegram_user_id": 10,
                "operation": "sync_access",
                "payload": {"expire_date": "2030-01-01T00:00:00+00:00"},
            }
        )

        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["provisioning"]["failed"], 1)
        self.assertEqual(snapshot["last_provisioning_error"]["error_type"], "RuntimeError")
        self.assertNotIn("sensitive upstream body", str(snapshot))
        self.assertEqual(database.failed[0][0], "task-2")
