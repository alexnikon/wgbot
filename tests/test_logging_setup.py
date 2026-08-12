import logging
import unittest
from unittest.mock import patch

from logging_setup import UvicornAccessLogFilter, configure_logging


class LoggingSetupTests(unittest.TestCase):
    def setUp(self):
        self.root = logging.getLogger()
        self.root_handlers = list(self.root.handlers)
        self.root_level = self.root.level
        self.configured = getattr(self.root, "_wgbot_logging_configured", None)
        self.dependency_state = {}
        for name in ("aiogram.event", "uvicorn.access"):
            logger = logging.getLogger(name)
            self.dependency_state[name] = (logger.level, list(logger.filters))
        self.root._wgbot_logging_configured = False

    def tearDown(self):
        self.root.handlers[:] = self.root_handlers
        self.root.setLevel(self.root_level)
        if self.configured is None:
            delattr(self.root, "_wgbot_logging_configured")
        else:
            self.root._wgbot_logging_configured = self.configured
        for name, (level, filters) in self.dependency_state.items():
            logger = logging.getLogger(name)
            logger.setLevel(level)
            logger.filters[:] = filters

    def test_info_suppresses_per_request_dependency_logs(self):
        with patch("logging_setup.LOG_LEVEL", "INFO"):
            configure_logging()

        self.assertEqual(self.root.level, logging.INFO)
        self.assertEqual(logging.getLogger("aiogram.event").level, logging.WARNING)
        self.assertEqual(logging.getLogger("uvicorn.access").level, logging.DEBUG)
        self.assertEqual(self.root.handlers[0].level, logging.INFO)

    def test_debug_enables_verbose_dependency_logs(self):
        with patch("logging_setup.LOG_LEVEL", "DEBUG"):
            configure_logging()

        self.assertEqual(self.root.level, logging.DEBUG)
        self.assertEqual(logging.getLogger("aiogram.event").level, logging.DEBUG)
        self.assertEqual(logging.getLogger("uvicorn.access").level, logging.DEBUG)
        self.assertEqual(self.root.handlers[0].level, logging.DEBUG)

    def test_successful_metrics_request_is_lowered_to_debug(self):
        record = self._access_record("/metrics", 200)

        self.assertTrue(UvicornAccessLogFilter().filter(record))

        self.assertEqual(record.levelno, logging.DEBUG)
        self.assertEqual(record.levelname, "DEBUG")

    def test_metrics_error_remains_info(self):
        record = self._access_record("/metrics", 500)

        self.assertTrue(UvicornAccessLogFilter().filter(record))

        self.assertEqual(record.levelno, logging.INFO)
        self.assertEqual(record.levelname, "INFO")

    def test_healthcheck_request_is_suppressed(self):
        record = self._access_record("/webhook/yookassa/health", 200)

        self.assertFalse(UvicornAccessLogFilter().filter(record))

    @staticmethod
    def _access_record(path: str, status_code: int) -> logging.LogRecord:
        return logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            ("10.8.2.2:38778", "GET", path, "1.1", status_code),
            None,
        )


if __name__ == "__main__":
    unittest.main()
