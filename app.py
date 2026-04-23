import asyncio
import logging

import uvicorn

import bot as bot_module
import webhook_server
from logging_setup import configure_logging


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

    bot_task = asyncio.create_task(bot_module.main(), name="bot-polling")
    webhook_task = asyncio.create_task(run_webhook_server(), name="webhook-server")
    tasks = {bot_task, webhook_task}

    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

        for task in done:
            exc = task.exception()
            if exc:
                logger.error(f"Task {task.get_name()} failed: {exc}", exc_info=exc)
                raise exc
            logger.warning(f"Task {task.get_name()} exited unexpectedly without error")

        for task in pending:
            task.cancel()

        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
