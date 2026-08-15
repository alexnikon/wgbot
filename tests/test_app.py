import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_http_server, run_periodic_backups, supervise_runtime


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
    async def test_disabled_scheduler_waits_for_shutdown(self):
        shutdown_requested = asyncio.Event()

        with patch("app.create_runtime_backup") as create_backup:
            task = asyncio.create_task(
                run_periodic_backups(Path("/runtime"), 0, shutdown_requested)
            )
            await asyncio.sleep(0)
            create_backup.assert_not_called()
            shutdown_requested.set()
            await task

    async def test_scheduler_waits_before_first_backup(self):
        shutdown_requested = asyncio.Event()

        with patch("app.create_runtime_backup") as create_backup:
            task = asyncio.create_task(
                run_periodic_backups(Path("/runtime"), 3600, shutdown_requested)
            )
            await asyncio.sleep(0)
            create_backup.assert_not_called()
            shutdown_requested.set()
            await task

    async def test_scheduler_creates_backup_after_interval(self):
        shutdown_requested = asyncio.Event()

        def create_backup(root):
            self.assertEqual(root, Path("/runtime"))
            shutdown_requested.set()

        with patch("app.create_runtime_backup", side_effect=create_backup) as backup:
            await asyncio.wait_for(
                run_periodic_backups(Path("/runtime"), 0.001, shutdown_requested),
                timeout=1,
            )

        backup.assert_called_once_with(Path("/runtime"))

    async def test_scheduler_retries_after_backup_error(self):
        shutdown_requested = asyncio.Event()
        attempts = 0

        def create_backup(root):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("broken source")
            shutdown_requested.set()

        with (
            patch("app.create_runtime_backup", side_effect=create_backup),
            self.assertLogs("app", level="ERROR") as logs,
        ):
            await asyncio.wait_for(
                run_periodic_backups(Path("/runtime"), 0.001, shutdown_requested),
                timeout=1,
            )

        self.assertEqual(attempts, 2)
        self.assertIn("Periodic runtime backup failed", "\n".join(logs.output))
