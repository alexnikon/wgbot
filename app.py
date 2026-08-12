import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager, suppress

import uvicorn

import bot as bot_module
import webhook_server
from config import METRICS_PORT
from logging_setup import configure_logging
from prometheus_exporter import create_metrics_app
from services import AppServices, create_services

configure_logging()
logger = logging.getLogger(__name__)


class NoSignalServer(uvicorn.Server):
    """Run Uvicorn under the application's shared signal supervisor."""

    @contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


def create_http_server(application: object, port: int) -> NoSignalServer:
    """Create one HTTP server without installing competing signal handlers."""
    config = uvicorn.Config(
        application,
        host="0.0.0.0",
        port=port,
        log_level="info",
        lifespan="on",
    )
    return NoSignalServer(config)


def create_webhook_server() -> NoSignalServer:
    """Create the YooKassa webhook HTTP server."""
    return create_http_server(webhook_server.app, 8001)


def create_metrics_server(services: AppServices) -> NoSignalServer:
    """Create the private Prometheus exposition HTTP server."""
    return create_http_server(create_metrics_app(services), METRICS_PORT)


async def supervise_runtime(
    bot_runtime: Awaitable[None],
    webhook_runtime: Awaitable[None],
    metrics_runtime: Awaitable[None],
    shutdown_requested: asyncio.Event,
    request_shutdown: Callable[[], Awaitable[None]],
) -> None:
    """Keep all runtimes alive and coordinate an explicit graceful shutdown."""
    bot_task = asyncio.create_task(bot_runtime, name="bot-polling")
    webhook_task = asyncio.create_task(webhook_runtime, name="webhook-server")
    metrics_task = asyncio.create_task(metrics_runtime, name="metrics-server")
    shutdown_task = asyncio.create_task(
        shutdown_requested.wait(), name="shutdown-signal"
    )
    runtime_tasks = (bot_task, webhook_task, metrics_task)
    all_tasks = (*runtime_tasks, shutdown_task)
    try:
        done, _ = await asyncio.wait(all_tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in runtime_tasks:
            if task in done and not task.cancelled() and task.exception() is not None:
                raise task.exception()  # type: ignore[misc]

        if shutdown_task not in done:
            finished = next(task for task in runtime_tasks if task in done)
            raise RuntimeError(f"Long-running task {finished.get_name()} exited unexpectedly")

        logger.info("Shutdown signal received")
        await request_shutdown()
        try:
            await asyncio.wait_for(
                asyncio.gather(*runtime_tasks),
                timeout=25,
            )
        except TimeoutError:
            logger.warning("Graceful shutdown timed out; cancelling runtime tasks")
            for task in runtime_tasks:
                task.cancel()
            await asyncio.gather(*runtime_tasks, return_exceptions=True)
    finally:
        for task in all_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)


def install_shutdown_handlers(shutdown_requested: asyncio.Event) -> Callable[[], None]:
    """Install process signal handlers and return a cleanup callback."""
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for handled_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(handled_signal, shutdown_requested.set)
            installed.append(handled_signal)
        except NotImplementedError:
            continue

    def cleanup() -> None:
        for handled_signal in installed:
            loop.remove_signal_handler(handled_signal)

    return cleanup


async def main() -> None:
    """Run the bot polling loop and webhook server in a single process."""
    logger.info("Starting combined application: bot polling + webhook server")
    services: AppServices = create_services()
    webhook_server.configure_runtime(services)
    server = create_webhook_server()
    metrics_server = create_metrics_server(services)
    shutdown_requested = asyncio.Event()
    remove_signal_handlers = install_shutdown_handlers(shutdown_requested)

    async def request_shutdown() -> None:
        server.should_exit = True
        metrics_server.should_exit = True
        with suppress(RuntimeError):
            await bot_module.dp.stop_polling()

    try:
        await supervise_runtime(
            bot_module.main(services),
            server.serve(),
            metrics_server.serve(),
            shutdown_requested,
            request_shutdown,
        )
    finally:
        remove_signal_handlers()
        await services.close()


if __name__ == "__main__":
    asyncio.run(main())
