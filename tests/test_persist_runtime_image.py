import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "persist_runtime_image.py"
SPEC = importlib.util.spec_from_file_location("persist_runtime_image", SCRIPT_PATH)
persist_runtime_image = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(persist_runtime_image)


class PersistRuntimeImageTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.env_path = Path(self.temporary_directory.name) / ".env"

    def test_appends_image_without_changing_other_values_or_mode(self):
        self.env_path.write_text(
            "# Runtime\nTOKEN=secret-value\nDOMAIN=example.com\n",
            encoding="utf-8",
        )
        self.env_path.chmod(0o600)

        persist_runtime_image.persist_runtime_image(
            self.env_path, "ghcr.io/example/wgbot:" + "a" * 40
        )

        content = self.env_path.read_text(encoding="utf-8")
        self.assertIn("TOKEN=secret-value", content)
        self.assertIn("DOMAIN=example.com", content)
        self.assertIn("WGBOT_IMAGE=ghcr.io/example/wgbot:" + "a" * 40, content)
        self.assertEqual(self.env_path.stat().st_mode & 0o777, 0o600)

    def test_replaces_duplicate_active_image_entries(self):
        self.env_path.write_text(
            "WGBOT_IMAGE=old:first\n# WGBOT_IMAGE=commented\nWGBOT_IMAGE=old:second\n",
            encoding="utf-8",
        )

        persist_runtime_image.persist_runtime_image(self.env_path, "new:image")

        content = self.env_path.read_text(encoding="utf-8")
        self.assertEqual(content.count("WGBOT_IMAGE=new:image"), 1)
        self.assertIn("# WGBOT_IMAGE=commented", content)
        self.assertNotIn("old:first", content)
        self.assertNotIn("old:second", content)

    def test_rejects_whitespace_in_image_reference(self):
        self.env_path.write_text("TOKEN=secret\n", encoding="utf-8")

        with self.assertRaises(ValueError):
            persist_runtime_image.persist_runtime_image(
                self.env_path, "ghcr.io/example/image:bad tag"
            )

        self.assertEqual(self.env_path.read_text(encoding="utf-8"), "TOKEN=secret\n")
