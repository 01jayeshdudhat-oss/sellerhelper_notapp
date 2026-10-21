from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile


BACKUP_FILENAME_PREFIX = "sellerhelper-"


def _verify_database(connection, error_prefix):
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if not integrity or integrity[0] != "ok":
        raise sqlite3.DatabaseError(f"{error_prefix}: {integrity!r}")
    has_tables = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1"
    ).fetchone()
    if not has_tables:
        raise sqlite3.DatabaseError(f"{error_prefix}: database contains no tables.")


def list_sqlite_backups(backup_directory):
    return sorted(
        Path(backup_directory).glob(f"{BACKUP_FILENAME_PREFIX}*.sqlite3"),
        key=lambda path: path.name,
        reverse=True,
    )


def create_sqlite_backup(database_path, backup_directory, retention_count):
    database_path = Path(database_path)
    backup_directory = Path(backup_directory)
    if retention_count < 1:
        raise ValueError("Backup retention count must be at least 1.")
    if not database_path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {database_path}")

    backup_directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination_path = (
        backup_directory / f"{BACKUP_FILENAME_PREFIX}{timestamp}.sqlite3"
    )
    temporary_path = destination_path.with_suffix(".sqlite3.tmp")

    try:
        with closing(sqlite3.connect(database_path)) as source:
            with closing(sqlite3.connect(temporary_path)) as destination:
                source.backup(destination)
                _verify_database(destination, "SQLite backup integrity check failed")
        temporary_path.replace(destination_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()

    for old_backup in list_sqlite_backups(backup_directory)[retention_count:]:
        old_backup.unlink()

    return destination_path


def restore_sqlite_backup(database_path, backup_path, backup_directory, retention_count):
    database_path = Path(database_path)
    backup_path = Path(backup_path)
    if retention_count < 1:
        raise ValueError("Backup retention count must be at least 1.")
    if not database_path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {database_path}")
    if not backup_path.is_file():
        raise FileNotFoundError(f"SQLite backup not found: {backup_path}")
    if database_path.resolve() == backup_path.resolve():
        raise ValueError("The backup must not be the live database.")

    backup_directory = Path(backup_directory)
    backup_directory.mkdir(parents=True, exist_ok=True)

    with closing(sqlite3.connect(backup_path)) as source:
        _verify_database(source, "SQLite backup integrity check failed")

    temporary_backup = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=Path(backup_directory),
            prefix="restore-source-",
            suffix=".sqlite3",
            delete=False,
        ) as temporary_file:
            temporary_backup = Path(temporary_file.name)

        with closing(sqlite3.connect(backup_path)) as source:
            with closing(sqlite3.connect(temporary_backup)) as copy:
                source.backup(copy)

        safety_backup = create_sqlite_backup(
            database_path,
            backup_directory,
            retention_count,
        )

        try:
            with closing(sqlite3.connect(temporary_backup)) as source:
                with closing(sqlite3.connect(database_path)) as destination:
                    source.backup(destination)
                    _verify_database(
                        destination,
                        "Restored SQLite database integrity check failed",
                    )
        except Exception:
            with closing(sqlite3.connect(safety_backup)) as source:
                with closing(sqlite3.connect(database_path)) as destination:
                    source.backup(destination)
            raise
    finally:
        if temporary_backup and temporary_backup.exists():
            temporary_backup.unlink()

    return safety_backup
