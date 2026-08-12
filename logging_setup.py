import logging

from config import LOG_LEVEL


class UvicornAccessLogFilter(logging.Filter):
    """Drop healthchecks and lower successful access records to DEBUG."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if (
            'GET /health HTTP/1.1' in message
            or 'GET /webhook/yookassa/health HTTP/1.1' in message
        ):
            return False

        args = record.args
        if isinstance(args, tuple) and len(args) >= 5:
            try:
                status_code = int(args[4])
            except (TypeError, ValueError):
                return True
            if status_code < 400:
                record.levelno = logging.DEBUG
                record.levelname = logging.getLevelName(logging.DEBUG)
        return True


def configure_logging() -> None:
    """Configure application logging for the container's stdout stream."""
    root_logger = logging.getLogger()
    if getattr(root_logger, "_wgbot_logging_configured", False):
        return

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    level = getattr(logging, LOG_LEVEL, logging.INFO)
    stream_handler.setLevel(level)
    root_logger.setLevel(level)
    root_logger.handlers.clear()
    root_logger.addHandler(stream_handler)

    verbose_dependency_level = logging.DEBUG if level <= logging.DEBUG else logging.WARNING
    logging.getLogger("aiogram.event").setLevel(verbose_dependency_level)
    uvicorn_access_logger = logging.getLogger("uvicorn.access")
    uvicorn_access_logger.setLevel(logging.DEBUG)
    uvicorn_access_logger.addFilter(UvicornAccessLogFilter())

    # HTTPX request logs include full URLs. Telegram API URLs contain the bot
    # token, so keep transport-level logs out of normal application output.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    root_logger._wgbot_logging_configured = True
