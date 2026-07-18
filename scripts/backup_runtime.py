#!/usr/bin/env python3
"""Create consistent runtime backups and enforce retention limits."""

import argparse
import os
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_RETENTION_DAYS = 30
DEFAULT_MAX_FILES = 20
MANAGED_BACKUP_RE = re.compile(
    r"^(?P<source>wgbot\.db|clients\.json)(?:\.[A-Za-z0-9_-]+)?\.\d{8}-\d{6}$"
)
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
    finally:
        temporary.unlink(missing_ok=True)
        Path(f"{temporary}-wal").unlink(missing_ok=True)
        Path(f"{temporary}-shm").unlink(missing_ok=True)


def prune_backups(
    backup_dir: Path,
    *,
    retention_days: int,
    max_files: int,
    now: datetime,
) -> list[Path]:
    """Delete only managed backup files that exceed age or count limits."""
    managed: dict[str, list[Path]] = {"wgbot.db": [], "clients.json": []}
    removed: list[Path] = []
    for path in backup_dir.iterdir():
        if not path.is_file():
            continue
        if TEMPORARY_SIDECAR_RE.fullmatch(path.name):
            path.unlink()
            removed.append(path)
            continue
        match = MANAGED_BACKUP_RE.fullmatch(path.name)
        if match:
            managed[match.group("source")].append(path)

    cutoff = now - timedelta(days=retention_days) if retention_days else None
    for paths in managed.values():
        remaining: list[Path] = []
        for path in paths:
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            if cutoff and modified < cutoff:
                path.unlink()
                removed.append(path)
            else:
                remaining.append(path)
        if max_files:
            remaining.sort(key=lambda item: item.stat().st_mtime, reverse=True)
            for path in remaining[max_files:]:
                path.unlink()
                removed.append(path)
    return removed


def create_runtime_backup(root: Path, label: str, now: datetime | None = None) -> list[Path]:
    """Back up runtime data and apply retention configured in the root .env file."""
    now = now or datetime.now(UTC)
    values = read_env(root / ".env")
    retention_days = parse_nonnegative_int(
        values, "BACKUP_RETENTION_DAYS", DEFAULT_RETENTION_DAYS
    )
    max_files = parse_nonnegative_int(values, "BACKUP_MAX_FILES", DEFAULT_MAX_FILES)

    backup_dir = root / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = now.strftime("%Y%m%d-%H%M%S")
    created: list[Path] = []

    database = root / "DB" / "wgbot.db"
    if database.is_file():
        destination = backup_dir / f"wgbot.db.{label}.{timestamp}"
        backup_sqlite(database, destination)
        os.utime(destination, (now.timestamp(), now.timestamp()))
        created.append(destination)

    registry = root / "clients.json"
    if registry.is_file():
        destination = backup_dir / f"clients.json.{label}.{timestamp}"
        shutil.copy2(registry, destination)
        os.utime(destination, (now.timestamp(), now.timestamp()))
        created.append(destination)

    removed = prune_backups(
        backup_dir,
        retention_days=retention_days,
        max_files=max_files,
        now=now,
    )
    print(
        f"Runtime backup complete: created={len(created)} removed={len(removed)} "
        f"retention_days={retention_days} max_files={max_files}"
    )
    return created


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Deployment root")
    parser.add_argument("--label", default="deploy", help="Backup filename label")
    args = parser.parse_args()
    create_runtime_backup(args.root.resolve(), args.label)


if __name__ == "__main__":
    main()
