#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this script as root." >&2
    exit 1
fi
CONFIG_ROOT=/etc/rent-monitor
if [[ -L "$CONFIG_ROOT" || ! -f "$CONFIG_ROOT/.rent-monitor-managed" ]]; then
    echo "Rent Monitor has not created its credential directory. Install the release first." >&2
    exit 1
fi

IFS= read -r -s -p "Telegram bot token (hidden): " token </dev/tty
printf '\n'
if [[ ! "$token" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]]; then
    unset token
    echo "The value does not look like a Telegram bot token." >&2
    exit 1
fi

umask 077
temporary_file="$(mktemp "$CONFIG_ROOT/.telegram-token.XXXXXX")"
printf '%s\n' "$token" >"$temporary_file"
unset token
chown root:root "$temporary_file"
chmod 0600 "$temporary_file"
mv -f "$temporary_file" "$CONFIG_ROOT/telegram_bot_token"
echo "Credential saved with root-only access. Start the service with: systemctl enable --now rent-monitor"
