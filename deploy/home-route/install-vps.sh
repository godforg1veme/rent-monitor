#!/usr/bin/env bash
# Run as root from an extracted, trusted release. Existing service/credentials required.
set -euo pipefail
release="$(cd "$(dirname "$0")/../.." && pwd)"
case "$release" in /opt/rent-monitor/releases/*) ;; *) echo 'Expected release under /opt/rent-monitor/releases' >&2; exit 1;; esac
runtime=/opt/rent-monitor/home-route
pin=2d09315ead7ef1b3498415474f690eea512cf571
test -f /etc/rent-monitor/telegram_bot_token
test -f /var/lib/rent-monitor/rent-monitor.sqlite3
install -d -m 0755 "$runtime"
# Each checkout is immutable; do not overwrite the active dependency in place.
vendor="$runtime/vendor-$pin"
if [ ! -d "$vendor/.git" ]; then
    git clone https://github.com/mickberrad659-sketch/Avito-Parser.git "$vendor"
    git -C "$vendor" checkout --detach "$pin"
fi
test "$(git -C "$vendor" rev-parse HEAD)" = "$pin"
python3 -m venv "$release/.venv"
"$release/.venv/bin/pip" install -r "$release/deploy/home-route/requirements-main.lock.txt"
if [ ! -x "$runtime/.venv/bin/python" ]; then python3 -m venv "$runtime/.venv"; fi
"$runtime/.venv/bin/pip" install -r "$release/deploy/home-route/requirements-worker.lock.txt"
backup="/opt/rent-monitor/backups/$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 0700 "$backup"
readlink /opt/rent-monitor/current > "$backup/previous-release.txt"
# Prepare everything before the short service interruption.
systemctl stop rent-monitor
python3 - "$backup/before.sqlite3" <<'PY'
import sqlite3, sys
with sqlite3.connect('/var/lib/rent-monitor/rent-monitor.sqlite3') as source:
    with sqlite3.connect(sys.argv[1]) as target:
        source.backup(target)
PY
chmod 0600 "$backup/before.sqlite3"
install -m 0644 "$release/deploy/home-route/home-pow-worker.py" "$runtime/"
install -m 0644 "$release/deploy/home-route/rent_adapter.py" "$runtime/"
ln -sfn "$vendor" "$runtime/vendor"
install -d /etc/systemd/system/rent-monitor.service.d
install -m 0644 "$release/deploy/home-route/home-route.conf" /etc/systemd/system/rent-monitor.service.d/home-route.conf
install -m 0644 "$release/deploy/home-route/90-rent-home-route.conf" /etc/ssh/sshd_config.d/90-rent-home-route.conf
sshd -t
systemctl reload ssh
ln -sfn "$release" /opt/rent-monitor/current
systemctl daemon-reload
systemctl start rent-monitor
systemctl is-active rent-monitor
echo "Activated $release; backup $backup. Verify health after one collection cycle."
