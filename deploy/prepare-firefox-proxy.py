"""Transfer only existing proxy credentials and Avito cookies over authenticated SSH."""

import json
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
profile = root / "data/firefox-avito-copy"
subprocess.run([
    "ssh", "jarvis-vps-new",
    "sudo test ! -f /var/lib/rent-monitor/avito-firefox-profile/prefs.js",
], check=True)
configuration = subprocess.run(
    ["ssh", "jarvis-vps", "sudo cat /etc/personal-proxy/3proxy.cfg"],
    capture_output=True,
    text=True,
    check=True,
).stdout
users = [
    entry
    for line in configuration.splitlines()
    if line.startswith("users ")
    for entry in line.split()[1:]
]
if len(users) != 1:
    raise RuntimeError("Ambiguous proxy configuration")
username, kind, password = users[0].split(":", 2)
if kind != "CL":
    raise RuntimeError("Unsupported proxy credential format")
credential = json.dumps(
    {
        "host": "87.120.187.202",
        "port": 1086,
        "username": username,
        "password": password,
    }
)
subprocess.run(
    ["ssh", "jarvis-vps-new", "sudo install -m 0600 /dev/stdin /etc/rent-monitor/browser_proxy"],
    input=credential.encode(),
    check=True,
    stdout=subprocess.DEVNULL,
)
del configuration, users, password, credential
subprocess.run(
    [
        "ssh",
        "jarvis-vps-new",
        "sudo install -d -m 0700 -o rent-monitor -g rent-monitor "
        "/var/lib/rent-monitor/avito-firefox-profile",
    ],
    check=True,
)
source = sqlite3.connect((profile / "cookies.sqlite").as_uri() + "?mode=ro", uri=True)
handle, temporary_name = tempfile.mkstemp(prefix="avito-only-", suffix=".sqlite", dir=profile)
os.close(handle)
temporary = Path(temporary_name)
try:
    destination = sqlite3.connect(temporary)
    try:
        schema = source.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='moz_cookies'"
        ).fetchone()[0]
        destination.execute(schema)
        rows = source.execute(
            "SELECT * FROM moz_cookies WHERE host='avito.ru' OR host LIKE '%.avito.ru'"
        ).fetchall()
        if rows:
            destination.executemany(
                f"INSERT INTO moz_cookies VALUES ({','.join('?' for _ in rows[0])})", rows
            )
        destination.commit()
    finally:
        destination.close()
    subprocess.run(
        [
            "ssh",
            "jarvis-vps-new",
            "sudo install -m 0600 -o rent-monitor -g rent-monitor /dev/stdin "
            "/var/lib/rent-monitor/avito-firefox-profile/cookies.sqlite",
        ],
        input=temporary.read_bytes(),
        check=True,
        stdout=subprocess.DEVNULL,
    )
    print("Proxy credential installed privately; Avito-only cookies transferred:", len(rows))
finally:
    source.close()
    temporary.unlink()
