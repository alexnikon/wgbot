#!/usr/bin/env python3
"""Create consistent runtime backup archives and enforce retention limits."""

import argparse
import json
import os
import re
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_RETENTION_DAYS = 30
MANAGED_LEGACY_BACKUP_RE = re.compile(
    r"^(?:wgbot\.db|cascade_servers\.json)"
    r"(?:\.[A-Za-z0-9_-]+)?\.\d{8}-\d{6}$"
)
MANAGED_ARCHIVE_RE = re.compile(
    r"^wgbot-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\.zip$"
)
MANAGED_DAY_DIR_RE = re.compile(r"^\d{2}-\d{2}-\d{2}$")
TEMPORARY_SIDECAR_RE = re.compile(
    r"^wgbot\.db(?:\.[A-Za-z0-9_-]+)?\.\d{8}-\d{6}\.tmp-(?:wal|shm)$"
)


def read_env(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE entries without executing the env file."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def parse_nonnegative_int(values: dict[str, str], name: str, default: int) -> int:
    """Parse a non-negative retention setting."""
    raw_value = values.get(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 0:
        raise ValueError(f"{name} must be zero or greater")
    return value


def backup_sqlite(source: Path, destination: Path) -> None:
    """Create a transactionally consistent SQLite backup, including WAL data."""
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    source_uri = f"file:{source.resolve()}?mode=ro"
    try:
        with (
            closing(sqlite3.connect(source_uri, uri=True)) as source_db,
            source_db,
            closing(sqlite3.connect(temporary)) as destination_db,
            destination_db,
        ):
            source_db.backup(destination_db)
            destination_db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            destination_db.execute("PRAGMA journal_mode=DELETE").fetchone()
            integrity = destination_db.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise sqlite3.DatabaseError("SQLite backup integrity check failed")
        temporary.replace(destination)
        destination.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
        Path(f"{temporary}-wal").unlink(missing_ok=True)
        Path(f"{temporary}-shm").unlink(missing_ok=True)


def backup_json(source: Path, destination: Path) -> None:
    """Validate and atomically copy a protected JSON runtime file."""
    content = source.read_bytes()
    registry = json.loads(content)
    if not isinstance(registry, dict) or not isinstance(registry.get("servers"), list):
        raise ValueError("Cascade registry must contain a servers list")
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        temporary.write_bytes(content)
        temporary.chmod(0o600)
        temporary.replace(destination)
        destination.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _is_expired(path: Path, cutoff: datetime | None) -> bool:
    if cutoff is None:
        return False
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    return modified < cutoff


def prune_backups(
    backup_dir: Path,
    *,
    retention_days: int,
    now: datetime,
) -> list[Path]:
    """Delete expired managed archives and legacy backup files only."""
    removed: list[Path] = []
    cutoff = now - timedelta(days=retention_days) if retention_days else None

    for path in backup_dir.iterdir():
        if path.is_file():
            if TEMPORARY_SIDECAR_RE.fullmatch(path.name) or (
                MANAGED_LEGACY_BACKUP_RE.fullmatch(path.name)
                and _is_expired(path, cutoff)
            ):
                path.unlink()
                removed.append(path)
            continue

        if not path.is_dir() or not MANAGED_DAY_DIR_RE.fullmatch(path.name):
            continue
        for archive in path.iterdir():
            if (
                archive.is_file()
                and MANAGED_ARCHIVE_RE.fullmatch(archive.name)
                and _is_expired(archive, cutoff)
            ):
                archive.unlink()
                removed.append(archive)
        try:
            path.rmdir()
        except OSError:
            pass
        else:
            removed.append(path)

    return removed


def _create_archive(
    environment: Path,
    database: Path,
    cascade_servers: Path,
    destination: Path,
    now: datetime,
) -> None:
    """Build and atomically publish a complete protected runtime archive."""
    if destination.exists():
        raise FileExistsError(f"Backup archive already exists: {destination}")

    backup_dir = destination.parent.parent
    with tempfile.TemporaryDirectory(prefix=".wgbot-backup-", dir=backup_dir) as raw:
        staging = Path(raw)
        staged_environment = staging / ".env"
        staged_database = staging / "wgbot.db"
        staged_registry = staging / "cascade_servers.json"
        temporary_archive = staging / destination.name

        staged_environment.write_bytes(environment.read_bytes())
        staged_environment.chmod(0o600)
        backup_sqlite(database, staged_database)
        backup_json(cascade_servers, staged_registry)

        with zipfile.ZipFile(
            temporary_archive,
            mode="x",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            archive.write(staged_environment, arcname=".env")
            archive.write(staged_database, arcname="wgbot.db")
            archive.write(staged_registry, arcname="cascade_servers.json")

        with zipfile.ZipFile(temporary_archive) as archive:
            invalid_entry = archive.testzip()
            if invalid_entry is not None:
                raise zipfile.BadZipFile(f"Invalid ZIP entry: {invalid_entry}")

        temporary_archive.chmod(0o600)
        os.utime(temporary_archive, (now.timestamp(), now.timestamp()))
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(temporary_archive, destination)
        except FileExistsError as exc:
            raise FileExistsError(
                f"Backup archive already exists: {destination}"
            ) from exc


def create_runtime_backup(
    root: Path,
    label: str | None = None,
    now: datetime | None = None,
) -> list[Path]:
    """Back up runtime data and apply age retention from the root .env file."""
    del label  # Kept temporarily for compatibility with older callers.
    now = (now or datetime.now(UTC)).astimezone(UTC)
    environment = root / ".env"
    database = root / "DB" / "wgbot.db"
    cascade_servers = root / "secrets" / "cascade_servers.json"

    values = read_env(environment) if environment.is_file() else {}
    retention_days = parse_nonnegative_int(
        values, "BACKUP_RETENTION_DAYS", DEFAULT_RETENTION_DAYS
    )
    backup_dir = root / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    missing = [
        str(path.relative_to(root))
        for path in (environment, database, cascade_servers)
        if not path.is_file()
    ]
    if missing:
        removed = prune_backups(
            backup_dir,
            retention_days=retention_days,
            now=now,
        )
        print(
            "INFO: Runtime backup skipped; missing required files: "
            f"{', '.join(missing)}; removed={len(removed)} "
            f"retention_days={retention_days}"
        )
        return []

    day = now.strftime("%d-%m-%y")
    timestamp = now.strftime("%d-%m-%y-%H-%M-%S")
    destination = backup_dir / day / f"wgbot-{timestamp}.zip"
    _create_archive(environment, database, cascade_servers, destination, now)

    removed = prune_backups(
        backup_dir,
        retention_days=retention_days,
        now=now,
    )
    print(
        "Runtime backup complete: created=1 "
        f"removed={len(removed)} retention_days={retention_days}"
    )
    return [destination]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Deployment root")
    parser.add_argument("--label", help=argparse.SUPPRESS)
    args = parser.parse_args()
    create_runtime_backup(args.root.resolve(), args.label)


if __name__ == "__main__":
    main()
