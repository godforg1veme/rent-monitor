"""Consistent, private SQLite backup before activating the new browser backend."""

import os
import sqlite3
from pathlib import Path

os.umask(0o077)
database = Path("/var/lib/rent-monitor/rent-monitor.sqlite3")
backup = Path("/var/lib/rent-monitor/backup-before-firefox-20260930.sqlite3")
if backup.exists():
    raise RuntimeError("Backup already exists; refusing to replace it")
source = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
destination = sqlite3.connect(backup)
try:
    source.backup(destination)
    if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise RuntimeError("Backup validation failed")
finally:
    destination.close()
    source.close()
print("Consistent SQLite backup created privately")
