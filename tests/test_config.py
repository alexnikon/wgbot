import os
import unittest
from pathlib import Path
from unittest.mock import patch

from config import _get_int, _get_interval_seconds


class ConfigTests(unittest.TestCase):
    def test_backup_interval_accepts_zero_and_enabled_interval(self):
        for raw_value, expected in (("0", 0), ("21600", 21600)):
            with self.subTest(raw_value=raw_value), patch.dict(
                os.environ, {"TEST_BACKUP_INTERVAL": raw_value}
            ):
                self.assertEqual(
                    _get_interval_seconds("TEST_BACKUP_INTERVAL", 21600),
                    expected,
                )

    def test_backup_interval_rejects_invalid_enabled_intervals(self):
        for raw_value in ("-1", "1", "59", "invalid"):
            with self.subTest(raw_value=raw_value), patch.dict(
                os.environ, {"TEST_BACKUP_INTERVAL": raw_value}
            ), self.assertRaises(ValueError):
                _get_interval_seconds("TEST_BACKUP_INTERVAL", 21600)

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

    def test_compose_mounts_runtime_backup_sources(self):
        compose = (Path(__file__).parent.parent / "docker-compose.yml").read_text()

        self.assertIn(
            "BACKUP_INTERVAL_SECONDS: ${BACKUP_INTERVAL_SECONDS:-21600}", compose
        )
        self.assertIn("- ./.env:/runtime/.env:ro", compose)
        self.assertIn("- ./DB:/runtime/DB:ro", compose)
        self.assertIn(
            "- ./secrets/cascade_servers.json:/runtime/secrets/cascade_servers.json:ro",
            compose,
        )
        self.assertIn("- ./backups:/runtime/backups", compose)


if __name__ == "__main__":
    unittest.main()
