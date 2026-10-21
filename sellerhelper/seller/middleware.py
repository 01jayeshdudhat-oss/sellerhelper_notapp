import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone as datetime_timezone
from pathlib import Path

from django.conf import settings
from django.db import DatabaseError, connections
from django.utils import timezone

from .models import SQLiteBackupSettings
from .sqlite_backups import create_sqlite_backup, list_sqlite_backups


logger = logging.getLogger(__name__)
BACKUP_DIRECTORY = Path(settings.BASE_DIR) / "sqlite_backups"
BACKUP_CHECK_SECONDS = 30


class AutomaticSQLiteBackupMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self._last_check_at = 0
        self._check_lock = threading.Lock()

    def __call__(self, request):
        response = self.get_response(request)
        self._backup_if_due()
        return response

    def _backup_if_due(self):
        now_monotonic = time.monotonic()
        if now_monotonic - self._last_check_at < BACKUP_CHECK_SECONDS:
            return

        with self._check_lock:
            now_monotonic = time.monotonic()
            if now_monotonic - self._last_check_at < BACKUP_CHECK_SECONDS:
                return
            self._last_check_at = now_monotonic

            database = connections["default"]
            database_path = database.settings_dict["NAME"]
            if (
                database.vendor != "sqlite"
                or not isinstance(database_path, (str, Path))
                or str(database_path) == ":memory:"
            ):
                return

            try:
                backup_settings = SQLiteBackupSettings.objects.get(pk=1)
                backups = list_sqlite_backups(BACKUP_DIRECTORY)
                last_backup_at = (
                    datetime.fromtimestamp(
                        backups[0].stat().st_mtime,
                        tz=datetime_timezone.utc,
                    )
                    if backups
                    else None
                )
                if last_backup_at and timezone.now() < (
                    last_backup_at
                    + timedelta(minutes=backup_settings.interval_minutes)
                ):
                    return

                backup_path = create_sqlite_backup(
                    database_path,
                    BACKUP_DIRECTORY,
                    backup_settings.retention_count,
                )
            except (DatabaseError, OSError, ValueError, sqlite3.Error):
                logger.exception("Automatic SQLite database backup failed.")
                return

            logger.info("Created automatic SQLite backup: %s", backup_path)
