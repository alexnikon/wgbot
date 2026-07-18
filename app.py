import asyncio
import logging

import uvicorn

import bot as bot_module
import webhook_server
from logging_setup import configure_logging
from services import AppServices, create_services

configure_logging()
logger = logging.getLogger(__name__)


async def run_webhook_server() -> None:
    """Run the FastAPI webhook server inside the shared event loop."""
    config = uvicorn.Config(
        webhook_server.app,
        host="0.0.0.0",
        port=8001,
        log_level="info",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    await server.serve()


async def main() -> None:
    """Run the bot polling loop and webhook server in a single process."""
    logger.info("Starting combined application: bot polling + webhook server")
    services: AppServices = create_services()
    webhook_server.configure_runtime(services)

    async def require_long_running(name: str, coroutine) -> None:
        await coroutine
        raise RuntimeError(f"Long-running task {name} exited unexpectedly")

    try:
        async with asyncio.TaskGroup() as task_group:
            task_group.create_task(
                require_long_running("bot-polling", bot_module.main(services)),
                name="bot-polling",
            )
            task_group.create_task(
                require_long_running("webhook-server", run_webhook_server()),
                name="webhook-server",
            )
    finally:
        await services.close()


if __name__ == "__main__":
    asyncio.run(main())
