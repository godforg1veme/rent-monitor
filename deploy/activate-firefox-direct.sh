#!/usr/bin/env bash
set -euo pipefail
APP=/opt/rent-monitor
RELEASE=$APP/releases/20260930-firefox-recovery
STATE=/var/lib/rent-monitor
[[ "$EUID" -eq 0 && -f "$APP/.rent-monitor-managed" ]]
[[ -d "$RELEASE/.venv" && -f "$STATE/avito-firefox-direct-profile/cookies.sqlite" ]]
[[ ! -e "$STATE/avito-firefox-proxy-backup" ]]
[[ ! -L "$STATE/avito-firefox-profile" && ! -L "$STATE/avito-firefox-direct-profile" ]]
grep -Fq '# managed-by: rent-monitor' /etc/systemd/system/rent-monitor.service
"$RELEASE/.venv/bin/python" "$RELEASE/deploy/backup-before-firefox.py"
systemctl stop rent-monitor.service
mv "$STATE/avito-firefox-profile" "$STATE/avito-firefox-proxy-backup"
mv "$STATE/avito-firefox-direct-profile" "$STATE/avito-firefox-profile"
install -d -m 0755 /etc/systemd/system/rent-monitor.service.d
install -m 0644 "$RELEASE/deploy/firefox-direct-backend.conf" \
    /etc/systemd/system/rent-monitor.service.d/browser.conf
chmod 0755 "$RELEASE/deploy/run-captcha-stack.sh"
ln -s "$RELEASE" "$APP/.current-firefox-direct"
mv -Tf "$APP/.current-firefox-direct" "$APP/current"
RENT_MONITOR_DATABASE="$STATE/rent-monitor.sqlite3" \
    "$RELEASE/.venv/bin/rent-monitor" resume-source avito --config "$RELEASE/config/search.toml"
systemctl daemon-reload
systemctl restart rent-monitor.service
systemctl is-active rent-monitor.service
