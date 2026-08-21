import asyncio
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import (
    create_http_server,
    next_backup_at,
    run_periodic_backups,
    supervise_runtime,
)


class HTTPServerConfigurationTests(unittest.TestCase):
    def test_http_server_preserves_application_logging(self):
        server = create_http_server(object(), 9000)

        self.assertIsNone(server.config.log_config)
        self.assertIsNone(server.config.log_level)


class RuntimeSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_signal_stops_all_runtimes_without_error(self):
        shutdown_requested = asyncio.Event()
        bot_stopped = asyncio.Event()
        webhook_stopped = asyncio.Event()
        metrics_stopped = asyncio.Event()
        backup_stopped = asyncio.Event()

        async def bot_runtime():
            await bot_stopped.wait()

        async def webhook_runtime():
            await webhook_stopped.wait()

        async def metrics_runtime():
            await metrics_stopped.wait()

        async def backup_runtime():
            await backup_stopped.wait()

        async def request_shutdown():
            bot_stopped.set()
            webhook_stopped.set()
            metrics_stopped.set()
            backup_stopped.set()

        task = asyncio.create_task(
            supervise_runtime(
                bot_runtime(),
                webhook_runtime(),
                metrics_runtime(),
                backup_runtime(),
                shutdown_requested,
                request_shutdown,
            )
        )
        await asyncio.sleep(0)
        shutdown_requested.set()

        await task

    async def test_clean_early_exit_is_fatal(self):
        never_finished = asyncio.Event()

        async def bot_runtime():
            return None

        async def webhook_runtime():
            await never_finished.wait()

        async def metrics_runtime():
            await never_finished.wait()

        with self.assertRaisesRegex(
            RuntimeError, "Long-running task bot-polling exited unexpectedly"
        ):
            await supervise_runtime(
                bot_runtime(),
                webhook_runtime(),
                metrics_runtime(),
                metrics_runtime(),
                asyncio.Event(),
                self._no_op_shutdown,
            )

    async def test_runtime_exception_is_propagated(self):
        never_finished = asyncio.Event()

        async def bot_runtime():
            raise ValueError("polling failed")

        async def webhook_runtime():
            await never_finished.wait()

        async def metrics_runtime():
            await never_finished.wait()

        with self.assertRaisesRegex(ValueError, "polling failed"):
            await supervise_runtime(
                bot_runtime(),
                webhook_runtime(),
                metrics_runtime(),
                metrics_runtime(),
                asyncio.Event(),
                self._no_op_shutdown,
            )

    async def test_clean_metrics_exit_is_fatal(self):
        never_finished = asyncio.Event()

        async def long_runtime():
            await never_finished.wait()

        async def metrics_runtime():
            return None

        with self.assertRaisesRegex(
            RuntimeError, "Long-running task metrics-server exited unexpectedly"
        ):
            await supervise_runtime(
                long_runtime(),
                long_runtime(),
                metrics_runtime(),
                long_runtime(),
                asyncio.Event(),
                self._no_op_shutdown,
            )

    async def _no_op_shutdown(self):
        return None


class PeriodicBackupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path("/runtime")
        self.paths = {
            "environment": Path("/runtime/.env"),
            "database": Path("/app/data/wgbot.db"),
            "cascade_servers": Path("/run/secrets/cascade_servers.json"),
            "backup_dir": Path("/runtime/backups"),
        }

    async def test_disabled_scheduler_waits_for_shutdown(self):
        shutdown_requested = asyncio.Event()

        with patch("app.create_runtime_backup") as create_backup:
            task = asyncio.create_task(
                run_periodic_backups(
                    self.root,
                    0,
                    shutdown_requested,
                    **self.paths,
                )
            )
            await asyncio.sleep(0)
            create_backup.assert_not_called()
            shutdown_requested.set()
            await task

    async def test_scheduler_waits_before_first_backup(self):
        shutdown_requested = asyncio.Event()

        with (
            patch("app.create_runtime_backup") as create_backup,
            patch("app._wait_for_shutdown", new=AsyncMock(return_value=True)) as wait,
        ):
            await run_periodic_backups(
                self.root,
                6,
                shutdown_requested,
                **self.paths,
            )

        create_backup.assert_not_called()
        wait.assert_awaited_once()

    async def test_scheduler_creates_backup_after_interval(self):
        shutdown_requested = asyncio.Event()

        def create_backup(root, **paths):
            self.assertEqual(root, self.root)
            self.assertFalse(paths.pop("emit_success"))
            self.assertEqual(paths, self.paths)
            shutdown_requested.set()

        with (
            patch("app.create_runtime_backup", side_effect=create_backup) as backup,
            patch("app._wait_for_shutdown", new=AsyncMock(return_value=False)),
        ):
            await run_periodic_backups(
                self.root,
                6,
                shutdown_requested,
                **self.paths,
            )

        backup.assert_called_once_with(
            self.root,
            **self.paths,
            emit_success=False,
        )

    async def test_scheduler_retries_after_backup_error(self):
        shutdown_requested = asyncio.Event()
        attempts = 0

        def create_backup(root, **paths):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("broken source")
            shutdown_requested.set()

        with (
            patch("app.create_runtime_backup", side_effect=create_backup),
            patch("app._wait_for_shutdown", new=AsyncMock(return_value=False)),
            self.assertLogs("app", level="ERROR") as logs,
        ):
            await run_periodic_backups(
                self.root,
                6,
                shutdown_requested,
                **self.paths,
            )

        self.assertEqual(attempts, 2)
        self.assertIn("Periodic runtime backup failed", "\n".join(logs.output))


class BackupScheduleTests(unittest.TestCase):
    def test_six_hour_schedule_uses_utc_calendar_boundaries(self):
        cases = (
            ((1, 23, 45), (6, 0, 0)),
            ((6, 0, 0), (12, 0, 0)),
            ((17, 59, 59), (18, 0, 0)),
            ((18, 0, 1), (0, 0, 0)),
        )
        for current_time, expected_time in cases:
            with self.subTest(current_time=current_time):
                now = datetime(2030, 1, 2, *current_time, tzinfo=UTC)
                expected_day = 3 if expected_time[0] == 0 else 2
                expected = datetime(
                    2030,
                    1,
                    expected_day,
                    *expected_time,
                    tzinfo=UTC,
                )
                self.assertEqual(next_backup_at(now, 6), expected)

    def test_supported_intervals_align_from_utc_midnight(self):
        now = datetime(2030, 1, 2, 5, 30, tzinfo=UTC)
        expected_hours = {1: 6, 2: 6, 3: 6, 4: 8, 6: 6, 8: 8, 12: 12, 24: 0}

        for interval, expected_hour in expected_hours.items():
            with self.subTest(interval=interval):
                result = next_backup_at(now, interval)
                expected_day = 3 if interval == 24 else 2
                self.assertEqual(
                    result,
                    datetime(2030, 1, expected_day, expected_hour, tzinfo=UTC),
                )

    def test_schedule_converts_input_time_to_utc(self):
        moscow_time = datetime(
            2030,
            1,
            2,
            8,
            30,
            tzinfo=timezone(timedelta(hours=3)),
        )

        self.assertEqual(
            next_backup_at(moscow_time, 6),
            datetime(2030, 1, 2, 6, 0, tzinfo=UTC),
        )

    def test_schedule_rejects_non_divisor_or_disabled_interval(self):
        now = datetime(2030, 1, 2, tzinfo=UTC)
        for interval in (0, -1, 5, 7):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                next_backup_at(now, interval)
