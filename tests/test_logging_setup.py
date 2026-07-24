import logging
import unittest
from unittest.mock import patch

from logging_setup import configure_logging


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
        self.assertEqual(logging.getLogger("uvicorn.access").level, logging.WARNING)

    def test_debug_enables_verbose_dependency_logs(self):
        with patch("logging_setup.LOG_LEVEL", "DEBUG"):
            configure_logging()

        self.assertEqual(self.root.level, logging.DEBUG)
        self.assertEqual(logging.getLogger("aiogram.event").level, logging.DEBUG)
        self.assertEqual(logging.getLogger("uvicorn.access").level, logging.DEBUG)


if __name__ == "__main__":
    unittest.main()
