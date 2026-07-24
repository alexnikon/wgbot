import asyncio
import unittest

from app import supervise_runtime


class RuntimeSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_signal_stops_both_runtimes_without_error(self):
        shutdown_requested = asyncio.Event()
        bot_stopped = asyncio.Event()
        webhook_stopped = asyncio.Event()

        async def bot_runtime():
            await bot_stopped.wait()

        async def webhook_runtime():
            await webhook_stopped.wait()

        async def request_shutdown():
            bot_stopped.set()
            webhook_stopped.set()

        task = asyncio.create_task(
            supervise_runtime(
                bot_runtime(),
                webhook_runtime(),
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

        with self.assertRaisesRegex(
            RuntimeError, "Long-running task bot-polling exited unexpectedly"
        ):
            await supervise_runtime(
                bot_runtime(),
                webhook_runtime(),
                asyncio.Event(),
                self._no_op_shutdown,
            )

    async def test_runtime_exception_is_propagated(self):
        never_finished = asyncio.Event()

        async def bot_runtime():
            raise ValueError("polling failed")

        async def webhook_runtime():
            await never_finished.wait()

        with self.assertRaisesRegex(ValueError, "polling failed"):
            await supervise_runtime(
                bot_runtime(),
                webhook_runtime(),
                asyncio.Event(),
                self._no_op_shutdown,
            )

    async def _no_op_shutdown(self):
        return None
