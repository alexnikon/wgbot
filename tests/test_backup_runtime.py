import importlib.util
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "backup_runtime.py"
SPEC = importlib.util.spec_from_file_location("backup_runtime", SCRIPT_PATH)
backup_runtime = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(backup_runtime)


class RuntimeBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        (self.root / "DB").mkdir()
        self.database = self.root / "DB" / "wgbot.db"
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("CREATE TABLE values_table(value TEXT)")
            connection.execute("INSERT INTO values_table VALUES ('saved')")

    def test_creates_consistent_database_backup(self):
        now = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)

        created = backup_runtime.create_runtime_backup(self.root, "dev", now)

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].name, "wgbot.db.dev.20300102-030405")
        with closing(sqlite3.connect(created[0])) as connection, connection:
            value = connection.execute("SELECT value FROM values_table").fetchone()[0]
        self.assertEqual(value, "saved")
        self.assertEqual(list((self.root / "backups").glob("*.tmp-*")), [])

    def test_prunes_by_age_and_count_without_deleting_unmanaged_files(self):
        backup_dir = self.root / "backups"
        backup_dir.mkdir()
        now = datetime(2030, 1, 10, tzinfo=UTC)
        unmanaged = backup_dir / "notes.txt"
        unmanaged.write_text("keep", encoding="utf-8")
        temporary_sidecar = backup_dir / "wgbot.db.dev.20300109-000000.tmp-shm"
        temporary_sidecar.write_text("temporary", encoding="utf-8")
        paths = []
        for index in range(4):
            path = backup_dir / f"wgbot.db.dev.2030010{index + 1}-000000"
            path.write_text(str(index), encoding="utf-8")
            modified = (now - timedelta(days=9 - index)).timestamp()
            os.utime(path, (modified, modified))
            paths.append(path)

        removed = backup_runtime.prune_backups(
            backup_dir,
            retention_days=7,
            max_files=2,
            now=now,
        )

        self.assertEqual(len(removed), 3)
        self.assertTrue(unmanaged.exists())
        self.assertFalse(temporary_sidecar.exists())
        self.assertEqual(len(list(backup_dir.glob("wgbot.db.*"))), 2)

    def test_zero_disables_retention_rule(self):
        values = {
            "BACKUP_RETENTION_DAYS": "0",
            "BACKUP_MAX_FILES": "0",
        }

        self.assertEqual(
            backup_runtime.parse_nonnegative_int(values, "BACKUP_RETENTION_DAYS", 30),
            0,
        )
