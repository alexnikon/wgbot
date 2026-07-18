import stat
import tempfile
import unittest
from pathlib import Path

from scripts.install_cascade_caddy_webhook import (
    END_MARKER,
    START_MARKER,
    CaddyPatchError,
    atomic_write,
    patch_caddyfile,
)


class CascadeCaddyPatchTests(unittest.TestCase):
    def test_inserts_webhook_before_decoy_handler(self):
        original = "https://:443 {\n    # ── Decoy site\n    handle {\n    }\n}\n"
        patched, changed = patch_caddyfile(original)
        self.assertTrue(changed)
        self.assertLess(patched.index(START_MARKER), patched.index("# ── Decoy site"))
        self.assertIn("reverse_proxy 127.0.0.1:8001", patched)

    def test_is_idempotent(self):
        original = "https://:443 {\n    # ── Decoy site\n}\n"
        patched, _ = patch_caddyfile(original)
        repeated, changed = patch_caddyfile(patched)
        self.assertFalse(changed)
        self.assertEqual(repeated, patched)

    def test_accepts_existing_dedicated_webhook_site(self):
        original = """https://pay.example.test {
    @webhook {
        path /webhook/yookassa /webhook/yookassa/*
    }
    handle @webhook {
        reverse_proxy 127.0.0.1:8001
    }
}

https://:443 {
    # ── Decoy site
}
"""
        patched, changed = patch_caddyfile(original)
        self.assertFalse(changed)
        self.assertEqual(patched, original)
        self.assertNotIn(START_MARKER, patched)

    def test_rejects_partial_managed_block(self):
        with self.assertRaises(CaddyPatchError):
            patch_caddyfile(f"{START_MARKER}\n    # ── Decoy site\n")

    def test_rejects_unknown_upstream_layout(self):
        with self.assertRaises(CaddyPatchError):
            patch_caddyfile("https://:443 {}\n")

    def test_markers_remain_distinct(self):
        self.assertNotEqual(START_MARKER, END_MARKER)

    def test_atomic_write_preserves_original_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Caddyfile"
            path.write_text("before\n", encoding="utf-8")
            path.chmod(0o644)

            atomic_write(path, "after\n")

            self.assertEqual(path.read_text(encoding="utf-8"), "after\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)


if __name__ == "__main__":
    unittest.main()
