from datetime import datetime, timedelta, timezone as datetime_timezone
from pathlib import Path
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from django.utils import timezone

from seller.models import SQLiteBackupSettings
from seller.sqlite_backups import create_sqlite_backup, list_sqlite_backups


BACKUP_DIRECTORY = Path(settings.BASE_DIR) / "sqlite_backups"
SCHEDULER_POLL_SECONDS = 30


class Command(BaseCommand):
    help = "Run the SQLite backup worker, using the interval and retention set in Django Admin."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Create one backup immediately, enforce retention, and exit.",
        )

    def handle(self, *args, **options):
        database = connections["default"]
        if database.vendor != "sqlite":
            raise CommandError("Automatic database backups require SQLite.")

        database_path = database.settings_dict["NAME"]
        if not isinstance(database_path, (str, Path)) or str(database_path) == ":memory:":
            raise CommandError("Automatic backups require a file-based SQLite database.")

        backup_settings = SQLiteBackupSettings.objects.get(pk=1)
        if options["once"]:
            backup_path = create_sqlite_backup(
                database_path,
                BACKUP_DIRECTORY,
                backup_settings.retention_count,
            )
            self.stdout.write(self.style.SUCCESS(f"Created SQLite backup: {backup_path}"))
            return

        self.stdout.write(
            f"SQLite backup worker running. Backup interval: "
            f"{backup_settings.interval_minutes} minutes; retention: "
            f"{backup_settings.retention_count} backups."
        )
        while True:
            backup_settings.refresh_from_db()
            backups = list_sqlite_backups(BACKUP_DIRECTORY)
            if backups:
                last_backup_time = datetime.fromtimestamp(
                    backups[0].stat().st_mtime,
                    tz=datetime_timezone.utc,
                )
                next_backup_in = (
                    last_backup_time
                    + timedelta(minutes=backup_settings.interval_minutes)
                    - timezone.now()
                ).total_seconds()
            else:
                next_backup_in = 0

            if next_backup_in <= 0:
                backup_path = create_sqlite_backup(
                    database_path,
                    BACKUP_DIRECTORY,
                    backup_settings.retention_count,
                )
                self.stdout.write(
                    self.style.SUCCESS(f"Created SQLite backup: {backup_path}")
                )
                continue

            time.sleep(min(next_backup_in, SCHEDULER_POLL_SECONDS))
