import sqlite3
import tempfile
from contextlib import closing
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.db import connections
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase

from .middleware import AutomaticSQLiteBackupMiddleware
from .models import SQLiteBackupSettings
from .sqlite_backups import create_sqlite_backup, list_sqlite_backups


class SQLiteBackupTests(SimpleTestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        self.database_path = self.directory / "source.sqlite3"
        with closing(sqlite3.connect(self.database_path)) as database:
            database.execute("CREATE TABLE sample (value TEXT NOT NULL)")
            database.execute("INSERT INTO sample (value) VALUES (?)", ("preserved",))
            database.commit()
        self.backup_directory = self.directory / "backups"

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_backup_is_consistent_and_contains_database_rows(self):
        backup_path = create_sqlite_backup(
            self.database_path,
            self.backup_directory,
            retention_count=5,
        )

        with closing(sqlite3.connect(backup_path)) as backup:
            self.assertEqual(
                backup.execute("SELECT value FROM sample").fetchone(),
                ("preserved",),
            )
            self.assertEqual(
                backup.execute("PRAGMA integrity_check").fetchone(),
                ("ok",),
            )

    def test_backup_retention_keeps_only_the_most_recent_successes(self):
        first_backup = create_sqlite_backup(
            self.database_path,
            self.backup_directory,
            retention_count=3,
        )
        second_backup = create_sqlite_backup(
            self.database_path,
            self.backup_directory,
            retention_count=3,
        )
        third_backup = create_sqlite_backup(
            self.database_path,
            self.backup_directory,
            retention_count=2,
        )

        self.assertEqual(
            list_sqlite_backups(self.backup_directory),
            [third_backup, second_backup],
        )
        self.assertFalse(first_backup.exists())

    def test_invalid_retention_does_not_create_a_backup(self):
        with self.assertRaises(ValueError):
            create_sqlite_backup(
                self.database_path,
                self.backup_directory,
                retention_count=0,
            )

        self.assertEqual(list_sqlite_backups(self.backup_directory), [])


class SQLiteBackupCommandTests(TestCase):
    @patch("seller.management.commands.run_sqlite_backups.create_sqlite_backup")
    def test_once_command_uses_admin_backup_settings(self, create_backup):
        backup_settings = SQLiteBackupSettings.objects.get(pk=1)
        output = StringIO()

        with tempfile.TemporaryDirectory() as backup_directory:
            with patch(
                "seller.management.commands.run_sqlite_backups.BACKUP_DIRECTORY",
                Path(backup_directory),
            ):
                call_command("run_sqlite_backups", once=True, stdout=output)

        create_backup.assert_called_once_with(
            connections["default"].settings_dict["NAME"],
            Path(backup_directory),
            backup_settings.retention_count,
        )
        self.assertIn("Created SQLite backup", output.getvalue())


class SQLiteBackupMiddlewareTests(TestCase):
    def test_web_requests_automatically_create_a_due_backup(self):
        middleware = AutomaticSQLiteBackupMiddleware(
            lambda request: HttpResponse("ok")
        )
        middleware._last_check_at = 0
        database = SimpleNamespace(
            vendor="sqlite",
            settings_dict={"NAME": "db.sqlite3"},
        )
        backup_path = Path("sqlite_backups") / "sellerhelper-test.sqlite3"

        with (
            patch(
                "seller.middleware.connections",
                {"default": database},
            ),
            patch("seller.middleware.BACKUP_DIRECTORY", Path("sqlite_backups")),
            patch("seller.middleware.list_sqlite_backups", return_value=[]),
            patch(
                "seller.middleware.create_sqlite_backup",
                return_value=backup_path,
            ) as create_backup,
        ):
            response = middleware(RequestFactory().get("/"))
            middleware(RequestFactory().get("/second-request"))

        self.assertEqual(response.content, b"ok")
        create_backup.assert_called_once_with(
            "db.sqlite3",
            Path("sqlite_backups"),
            SQLiteBackupSettings.objects.get(pk=1).retention_count,
        )
