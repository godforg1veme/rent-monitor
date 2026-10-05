"""Preserve live state and existing bot identity; hand off to one verified VPS instance."""

import hmac
import subprocess


def remote(host, command, *, data=None, capture=False):
    return subprocess.run(
        ["ssh", host, command],
        input=data,
        check=True,
        stdout=subprocess.PIPE if capture else None,
    )


source_host, target_host = "jarvis-vps-new", "jarvis-vps"
source_token = remote(
    source_host, "sudo cat /etc/rent-monitor/telegram_bot_token", capture=True
).stdout.strip()
target_token = remote(
    target_host, "sudo cat /etc/rent-monitor/telegram_bot_token", capture=True
).stdout.strip()
if not hmac.compare_digest(source_token, target_token):
    raise RuntimeError("Bot identities differ; refusing to hand off")
del source_token, target_token
print("Existing bot identity matches", flush=True)
remote(target_host, "sudo test ! -e /var/lib/rent-monitor/rent-monitor.before-handoff.sqlite3")
remote(target_host, "! sudo systemctl is-active --quiet rent-monitor.service")
remote(
    target_host,
    "sudo /opt/rent-monitor/releases/20260930-firefox-recovery/.venv/bin/python "
    "/opt/rent-monitor/releases/20260930-firefox-recovery/deploy/backup-before-firefox.py",
)
remote(source_host, "sudo systemctl stop rent-monitor.service")
try:
    remote(
        source_host,
        "sudo /opt/rent-monitor/releases/20260930-firefox-recovery/.venv/bin/python "
        "/opt/rent-monitor/releases/20260930-firefox-recovery/deploy/backup-before-firefox.py",
    )
    snapshot = remote(
        source_host,
        "sudo cat /var/lib/rent-monitor/backup-before-firefox-20260930.sqlite3",
        capture=True,
    ).stdout
    if not snapshot.startswith(b"SQLite format 3\x00"):
        raise RuntimeError("Invalid source database snapshot")
    remote(
        target_host,
        "sudo install -m 0600 -o rent-monitor -g rent-monitor /dev/stdin "
        "/var/lib/rent-monitor/rent-monitor.incoming.sqlite3",
        data=snapshot,
    )
    del snapshot
    remote(
        target_host,
        "sudo mv /var/lib/rent-monitor/rent-monitor.sqlite3 "
        "/var/lib/rent-monitor/rent-monitor.before-handoff.sqlite3 && "
        "sudo mv /var/lib/rent-monitor/rent-monitor.incoming.sqlite3 "
        "/var/lib/rent-monitor/rent-monitor.sqlite3",
    )
    remote(
        target_host,
        "sudo bash /opt/rent-monitor/releases/20260930-firefox-recovery/"
        "deploy/activate-firefox-germany.sh",
    )
except Exception:
    remote(target_host, "sudo systemctl stop rent-monitor.service")
    remote(source_host, "sudo systemctl start rent-monitor.service")
    raise
else:
    remote(source_host, "sudo systemctl disable rent-monitor.service")
    print("Single active bot moved with current SQLite and preserved backups", flush=True)
