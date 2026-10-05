#!/usr/bin/env bash
set -euo pipefail
if [[ "$EUID" -ne 0 ]]; then
    echo "Run with sudo after tailscale up." >&2
    exit 1
fi
phone_ip="$(tailscale status --json | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("BackendState")=="Running", "Run tailscale up first"; print(next(ip for ip in d["Self"]["TailscaleIPs"] if ":" not in ip))')"
umask 077
temporary_file="$(mktemp /etc/rent-monitor/.environment.XXXXXX)"
trap 'rm -f "$temporary_file"' EXIT
if [[ -f /etc/rent-monitor/environment ]]; then
    awk '!/^RENT_MONITOR_CAPTCHA_BASE_URL=/' /etc/rent-monitor/environment >"$temporary_file"
fi
printf 'RENT_MONITOR_CAPTCHA_BASE_URL=http://%s:10001\n' "$phone_ip" >>"$temporary_file"
install -o root -g root -m 0600 "$temporary_file" /etc/rent-monitor/environment
printf 'RENT_MONITOR_PHONE_BIND=%s\n' "$phone_ip" > /etc/rent-monitor/phone-address
install -m 0644 /opt/rent-monitor/current/deploy/systemd/rent-monitor-phone-ip.service \
    /etc/systemd/system/rent-monitor-phone-ip.service
systemctl daemon-reload
systemctl enable --now rent-monitor-phone-ip
systemctl restart rent-monitor
echo "Phone CAPTCHA access configured. Connect the iPhone to the same tailnet."
