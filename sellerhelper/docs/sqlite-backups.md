# SQLite database backups

The application database is backed up with SQLite's online backup API, which
creates a consistent snapshot while the application is running. Successful
backups are written to `sqlite_backups/` beside `db.sqlite3`; only the configured
most recent backups are kept.

1. Apply database migrations.
2. In Django Admin, edit **SQLite backup settings** to choose the interval in
   minutes and the number of recent backups to retain. The defaults are 30
   minutes and 5 backups.
3. The web application checks for a due backup during normal requests and
   creates one if the configured interval has elapsed or no backup exists.
   The backup check is throttled to once per 30 seconds per web process.

For backups to run on schedule even when the website receives no requests,
also run `python manage.py run_sqlite_backups` as a continuously supervised
background process. This worker checks for changed settings every 30 seconds.

To create a one-time backup immediately, run
`python manage.py run_sqlite_backups --once`. This also enforces the current
retention count.

To restore a backup, open **SQLite backup settings** in Django Admin and choose
**Restore from backup**. Only a superuser can restore. Select a backup, type
`RESTORE`, and submit; the current database is backed up first, and the
pre-restore safety backup is reported after completion. Restoring replaces all
current data with the selected snapshot, so stop other application workers and
schedule a maintenance window before doing it. Sign in again afterward if the
restored database does not contain the current admin session.

The backup directory is local to the application host. For disaster recovery
from host or disk loss, copy backups to a separate storage system.
