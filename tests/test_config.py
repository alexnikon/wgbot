import os
import unittest
from pathlib import Path
from unittest.mock import patch

from config import _get_int


class ConfigTests(unittest.TestCase):
    def test_metrics_port_accepts_valid_port(self):
        with patch.dict(os.environ, {"TEST_METRICS_PORT": "19100"}):
            self.assertEqual(
                _get_int("TEST_METRICS_PORT", 9100, minimum=1, maximum=65535),
                19100,
            )

    def test_metrics_port_rejects_out_of_range_port(self):
        for value in ("0", "65536"):
            with self.subTest(value=value), patch.dict(
                os.environ, {"TEST_METRICS_PORT": value}
            ), self.assertRaises(ValueError):
                _get_int("TEST_METRICS_PORT", 9100, minimum=1, maximum=65535)

    def test_compose_publishes_configured_metrics_address_and_port(self):
        compose = (Path(__file__).parent.parent / "docker-compose.yml").read_text()

        self.assertIn("METRICS_PORT: ${METRICS_PORT:-9100}", compose)
        self.assertIn(
            '"${METRICS_BIND_ADDRESS:-127.0.0.1}:${METRICS_PORT:-9100}:${METRICS_PORT:-9100}"',
            compose,
        )


if __name__ == "__main__":
    unittest.main()
