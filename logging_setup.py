import logging

from config import LOG_LEVEL


class HealthcheckAccessLogFilter(logging.Filter):
    """Drop noisy healthcheck access log records from uvicorn."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return (
            'GET /health HTTP/1.1' not in message
            and 'GET /webhook/yookassa/health HTTP/1.1' not in message
        )


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
    root_logger.setLevel(level)
    root_logger.handlers.clear()
    root_logger.addHandler(stream_handler)

    healthcheck_filter = HealthcheckAccessLogFilter()
    logging.getLogger("uvicorn.access").addFilter(healthcheck_filter)

    # HTTPX request logs include full URLs. Telegram API URLs contain the bot
    # token, so keep transport-level logs out of normal application output.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    root_logger._wgbot_logging_configured = True
