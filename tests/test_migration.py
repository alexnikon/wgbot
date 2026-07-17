import json
import os
import tempfile
import unittest
from pathlib import Path

from migrate_to_cascade import load_client_registry


class ClientRegistryMigrationTests(unittest.TestCase):
    def test_unified_registry_preserves_promo_and_peer_roles(self):
        handle, path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        try:
            Path(path).write_text(
                json.dumps(
                    {
                        "clients": [
                            {
                                "telegramId": 10,
                                "username": "alice",
                                "promo": 25,
                                "peers": [
                                    {
                                        "role": "bot",
                                        "clientId": "alice",
                                        "publicKey": "primary-key",
                                    },
                                    {
                                        "role": "manual",
                                        "clientId": "phone",
                                        "publicKey": "manual-key",
                                    },
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            entries = load_client_registry(Path(path))
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0]["promo"], 25)
            self.assertEqual(entries[0]["role"], "primary")
            self.assertEqual(entries[1]["role"], "manual")
        finally:
            os.remove(path)

    def test_invalid_registry_has_actionable_location(self):
        handle, path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        try:
            Path(path).write_text('{"clients": [', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "line 1, column"):
                load_client_registry(Path(path))
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
