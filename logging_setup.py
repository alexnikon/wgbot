import logging
import os


def configure_logging() -> None:
    """Configure application-wide logging once for file and docker logs."""
    root_logger = logging.getLogger()
    if getattr(root_logger, "_wgbot_logging_configured", False):
        return

    os.makedirs("logs", exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    file_handler = logging.FileHandler("logs/app.log")
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()
    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)
    root_logger._wgbot_logging_configured = True
