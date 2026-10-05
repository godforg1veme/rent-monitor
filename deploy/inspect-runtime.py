"""Read operational state without disclosing Telegram credentials or owner IDs."""

import json
import sqlite3

connection = sqlite3.connect("file:/var/lib/rent-monitor/rent-monitor.sqlite3?mode=ro", uri=True)
connection.row_factory = sqlite3.Row
for table in ("owner_binding", "application_state", "source_status", "source_runtime"):
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        continue
    if table == "owner_binding":
        print(
            "owner_binding_rows",
            connection.execute("SELECT count(*) FROM owner_binding").fetchone()[0],
        )
    else:
        print(
            table, json.dumps([dict(row) for row in connection.execute(f"SELECT * FROM {table}")])
        )
connection.close()
