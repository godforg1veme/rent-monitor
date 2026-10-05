import json
import sqlite3

with sqlite3.connect("file:/var/lib/rent-monitor/rent-monitor.sqlite3?mode=ro", uri=True) as db:
    db.row_factory = sqlite3.Row
    print("database_check", db.execute("PRAGMA quick_check").fetchone()[0])
    print(
        "application_state",
        json.dumps([dict(row) for row in db.execute("SELECT * FROM application_state")]),
    )
    print(
        "avito_runtime",
        json.dumps(
            [dict(row) for row in db.execute("SELECT * FROM source_runtime WHERE source='avito' ")]
        ),
    )
    print(
        "avito_status",
        json.dumps(
            [dict(row) for row in db.execute("SELECT * FROM source_status WHERE source='avito' ")]
        ),
    )
    print(
        "pending_notifications",
        db.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE status <> 'delivered'"
        ).fetchone()[0],
    )
    print(
        "baseline_complete",
        db.execute("SELECT complete FROM source_baselines WHERE source='avito'").fetchone()[0],
    )
    print(
        "source_counts",
        json.dumps(
            [
                dict(row)
                for row in db.execute(
                    "SELECT source,COUNT(*) AS count FROM listings GROUP BY source"
                )
            ]
        ),
    )
