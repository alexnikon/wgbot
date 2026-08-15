import contextlib
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
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
        self.environment = self.root / ".env"
        self.environment.write_text(
            "BOT_TOKEN=secret\nBACKUP_RETENTION_DAYS=30\n",
            encoding="utf-8",
        )

        (self.root / "DB").mkdir()
        self.database = self.root / "DB" / "wgbot.db"
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("CREATE TABLE values_table(value TEXT)")
            connection.execute("INSERT INTO values_table VALUES ('saved')")

        secrets = self.root / "secrets"
        secrets.mkdir()
        self.registry = secrets / "cascade_servers.json"
        self.registry.write_text(
            json.dumps({"servers": [{"server_key": "server-a"}]}),
            encoding="utf-8",
        )

    def _create_backup(self, now=None):
        return backup_runtime.create_runtime_backup(
            self.root,
            "ignored-label",
            now or datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC),
        )

    def test_creates_protected_archive_with_complete_runtime_snapshot(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("INSERT INTO values_table VALUES ('from-wal')")
            connection.commit()
            self.assertTrue(Path(f"{self.database}-wal").exists())

            created = self._create_backup()

        expected = (
            self.root
            / "backups"
            / "02-01-30"
            / "wgbot-02-01-30-03-04-05.zip"
        )
        self.assertEqual(created, [expected])
        self.assertEqual(expected.stat().st_mode & 0o777, 0o600)

        with zipfile.ZipFile(expected) as archive:
            self.assertEqual(
                archive.namelist(),
                [".env", "wgbot.db", "cascade_servers.json"],
            )
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.read(".env"), self.environment.read_bytes())
            self.assertEqual(archive.read("cascade_servers.json"), self.registry.read_bytes())
            extracted_database = self.root / "extracted.db"
            extracted_database.write_bytes(archive.read("wgbot.db"))

        with closing(sqlite3.connect(extracted_database)) as connection:
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
            values = connection.execute(
                "SELECT value FROM values_table ORDER BY rowid"
            ).fetchall()
        self.assertEqual(values, [("saved",), ("from-wal",)])
        self.assertEqual(list((self.root / "backups").glob(".wgbot-backup-*")), [])

    def test_rejects_invalid_cascade_registry_without_partial_archive(self):
        self.registry.write_text("{invalid", encoding="utf-8")

        with self.assertRaises(json.JSONDecodeError):
            self._create_backup()

        self.assertEqual(list((self.root / "backups").rglob("*.zip")), [])
        self.assertEqual(list((self.root / "backups").glob(".wgbot-backup-*")), [])
        self.assertFalse((self.root / "backups" / "02-01-30").exists())

    def test_rejects_registry_without_servers_list(self):
        self.registry.write_text("{}", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "servers list"):
            self._create_backup()

        self.assertEqual(list((self.root / "backups").rglob("*.zip")), [])

    def test_rejects_corrupt_available_database_without_partial_archive(self):
        self.database.write_bytes(b"not a sqlite database")
        self.registry.unlink()

        with self.assertRaises(sqlite3.DatabaseError):
            self._create_backup()

        self.assertEqual(list((self.root / "backups").rglob("*.zip")), [])
        self.assertEqual(list((self.root / "backups").glob(".wgbot-backup-*")), [])

    def test_creates_partial_archive_for_every_available_source_combination(self):
        source_names = (".env", "wgbot.db", "cascade_servers.json")
        for mask in range(8):
            with self.subTest(mask=mask):
                case_root = self.root / f"case-{mask}"
                case_root.mkdir()
                if mask & 1:
                    shutil.copy2(self.environment, case_root / ".env")
                if mask & 2:
                    (case_root / "DB").mkdir()
                    shutil.copy2(self.database, case_root / "DB" / "wgbot.db")
                if mask & 4:
                    (case_root / "secrets").mkdir()
                    shutil.copy2(
                        self.registry,
                        case_root / "secrets" / "cascade_servers.json",
                    )
                expected_names = [
                    name for index, name in enumerate(source_names) if mask & (1 << index)
                ]
                output = io.StringIO()

                with contextlib.redirect_stdout(output):
                    created = backup_runtime.create_runtime_backup(
                        case_root,
                        now=datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC),
                    )

                if not expected_names:
                    self.assertEqual(created, [])
                    self.assertIn("INFO: Runtime backup skipped", output.getvalue())
                    self.assertEqual(list((case_root / "backups").rglob("*.zip")), [])
                    continue
                self.assertEqual(len(created), 1)
                with zipfile.ZipFile(created[0]) as archive:
                    self.assertEqual(archive.namelist(), expected_names)
                    self.assertIsNone(archive.testzip())
                if len(expected_names) < len(source_names):
                    self.assertIn("INFO: Runtime backup is incomplete", output.getvalue())
                    self.assertIn(f"entries={len(expected_names)}", output.getvalue())

    def test_does_not_overwrite_archive_with_same_timestamp(self):
        created = self._create_backup()
        original = created[0].read_bytes()

        with self.assertRaisesRegex(FileExistsError, "already exists"):
            self._create_backup()

        self.assertEqual(created[0].read_bytes(), original)
        self.assertEqual(list((self.root / "backups").rglob("*.zip")), created)
        self.assertEqual(list((self.root / "backups").glob(".wgbot-backup-*")), [])

    def test_converts_supplied_time_to_utc_for_archive_name(self):
        moscow_time = datetime(
            2030,
            1,
            2,
            1,
            4,
            5,
            tzinfo=timezone(timedelta(hours=3)),
        )

        created = self._create_backup(moscow_time)

        self.assertEqual(created[0].parent.name, "01-01-30")
        self.assertEqual(created[0].name, "wgbot-01-01-30-22-04-05.zip")

    def test_prunes_expired_archives_and_legacy_files_only(self):
        backup_dir = self.root / "backups"
        old_day = backup_dir / "01-01-30"
        fresh_day = backup_dir / "09-01-30"
        old_day.mkdir(parents=True)
        fresh_day.mkdir()
        now = datetime(2030, 1, 10, tzinfo=UTC)

        old_archive = old_day / "wgbot-01-01-30-00-00-00.zip"
        fresh_archive = fresh_day / "wgbot-09-01-30-00-00-00.zip"
        old_legacy = backup_dir / "wgbot.db.production.20300101-000000"
        fresh_legacy = backup_dir / "cascade_servers.json.rollback.20300109-000000"
        unmanaged = backup_dir / "notes.txt"
        unmanaged_in_day = old_day / "keep.txt"
        unmanaged_dir = backup_dir / "manual"
        unmanaged_dir.mkdir()
        unmanaged_dir_file = unmanaged_dir / "wgbot-01-01-30-00-00-00.zip"
        temporary_sidecar = backup_dir / "wgbot.db.dev.20300109-000000.tmp-shm"

        for path in (
            old_archive,
            fresh_archive,
            old_legacy,
            fresh_legacy,
            unmanaged,
            unmanaged_in_day,
            unmanaged_dir_file,
            temporary_sidecar,
        ):
            path.write_text("data", encoding="utf-8")
        old_timestamp = (now - timedelta(days=9)).timestamp()
        fresh_timestamp = (now - timedelta(days=1)).timestamp()
        for path in (old_archive, old_legacy):
            os.utime(path, (old_timestamp, old_timestamp))
        for path in (fresh_archive, fresh_legacy):
            os.utime(path, (fresh_timestamp, fresh_timestamp))

        removed = backup_runtime.prune_backups(
            backup_dir,
            retention_days=7,
            now=now,
        )

        self.assertIn(old_archive, removed)
        self.assertIn(old_legacy, removed)
        self.assertIn(temporary_sidecar, removed)
        self.assertFalse(old_archive.exists())
        self.assertFalse(old_legacy.exists())
        self.assertFalse(temporary_sidecar.exists())
        self.assertTrue(old_day.exists())
        self.assertTrue(unmanaged_in_day.exists())
        for path in (fresh_archive, fresh_legacy, unmanaged, unmanaged_dir_file):
            self.assertTrue(path.exists())

    def test_removes_empty_managed_day_directory(self):
        backup_dir = self.root / "backups"
        day = backup_dir / "01-01-30"
        day.mkdir(parents=True)
        archive = day / "wgbot-01-01-30-00-00-00.zip"
        archive.write_text("old", encoding="utf-8")
        now = datetime(2030, 1, 10, tzinfo=UTC)
        old_timestamp = (now - timedelta(days=9)).timestamp()
        os.utime(archive, (old_timestamp, old_timestamp))

        removed = backup_runtime.prune_backups(
            backup_dir,
            retention_days=7,
            now=now,
        )

        self.assertIn(archive, removed)
        self.assertIn(day, removed)
        self.assertFalse(day.exists())

    def test_zero_disables_age_retention_and_max_files_is_ignored(self):
        self.environment.write_text(
            "BACKUP_RETENTION_DAYS=0\nBACKUP_MAX_FILES=invalid\n",
            encoding="utf-8",
        )
        now = datetime(2030, 1, 10, tzinfo=UTC)
        created = self._create_backup(now)
        archive = created[0]
        old_timestamp = (now - timedelta(days=100)).timestamp()
        os.utime(archive, (old_timestamp, old_timestamp))

        removed = backup_runtime.prune_backups(
            self.root / "backups",
            retention_days=0,
            now=now,
        )

        self.assertEqual(removed, [])
        self.assertTrue(archive.exists())


if __name__ == "__main__":
    unittest.main()
