#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="${1:-}"
REVISION="${2:-}"
if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this installer as root." >&2
    exit 1
fi
if [[ -z "$SOURCE_ROOT" || -z "$REVISION" || ! -d "$SOURCE_ROOT" ]]; then
    echo "Usage: install.sh SOURCE_DIRECTORY COMMIT_SHA" >&2
    exit 2
fi
if [[ ! "$REVISION" =~ ^[0-9a-f]{7,40}$ ]]; then
    echo "COMMIT_SHA must contain 7 to 40 lowercase hexadecimal characters." >&2
    exit 2
fi

SOURCE_ROOT="$(realpath "$SOURCE_ROOT")"
APP_ROOT=/opt/rent-monitor
RELEASE_ROOT="$APP_ROOT/releases"
RELEASE="$RELEASE_ROOT/$REVISION"
APP_MARKER="$APP_ROOT/.rent-monitor-managed"
CONFIG_ROOT=/etc/rent-monitor
CONFIG_MARKER="$CONFIG_ROOT/.rent-monitor-managed"
TOKEN_FILE="$CONFIG_ROOT/telegram_bot_token"
UNIT_SOURCE="$SOURCE_ROOT/deploy/systemd/rent-monitor.service"
UNIT_TARGET=/etc/systemd/system/rent-monitor.service
UNIT_MARKER='# managed-by: rent-monitor'

if [[ ! -f "$UNIT_SOURCE" ]]; then
    echo "The source directory does not contain the Rent Monitor unit." >&2
    exit 2
fi
if [[ -L "$APP_ROOT" || ( -e "$APP_ROOT" && ! -f "$APP_MARKER" ) ]]; then
    echo "Refusing to use an existing unmanaged path: $APP_ROOT" >&2
    exit 1
fi
if [[ -L "$CONFIG_ROOT" || ( -e "$CONFIG_ROOT" && ! -f "$CONFIG_MARKER" ) ]]; then
    echo "Refusing to use an existing unmanaged path: $CONFIG_ROOT" >&2
    exit 1
fi
if [[ -L "$RELEASE_ROOT" ]]; then
    echo "Refusing to use a symbolic-link release directory: $RELEASE_ROOT" >&2
    exit 1
fi
if [[ -e "$UNIT_TARGET" ]] && ! grep -Fq "$UNIT_MARKER" "$UNIT_TARGET"; then
    echo "Refusing to replace an unmanaged systemd unit: $UNIT_TARGET" >&2
    exit 1
fi

if getent passwd rent-monitor >/dev/null; then
    account_record="$(getent passwd rent-monitor)"
    IFS=: read -r _ _ _ _ _ account_home account_shell <<<"$account_record"
    if [[ "$account_home" != /nonexistent || "$account_shell" != /usr/sbin/nologin ]]; then
        echo "The rent-monitor account already exists with unexpected settings; inspect it manually." >&2
        exit 1
    fi
else
    useradd --system --user-group --home-dir /nonexistent --shell /usr/sbin/nologin rent-monitor
fi

install -d -o root -g root -m 0755 "$APP_ROOT" "$RELEASE_ROOT"
install -o root -g root -m 0644 /dev/null "$APP_MARKER"
install -d -o root -g root -m 0700 "$CONFIG_ROOT"
install -o root -g root -m 0600 /dev/null "$CONFIG_MARKER"

if [[ -e "$RELEASE" ]]; then
    if [[ ! -f "$RELEASE/.rent-monitor-release" || "$(cat "$RELEASE/.rent-monitor-release")" != "$REVISION" ]]; then
        echo "Refusing to replace an unmarked release directory: $RELEASE" >&2
        exit 1
    fi
else
    install -d -o root -g root -m 0755 "$RELEASE_ROOT"
    stage="$(mktemp -d "$RELEASE_ROOT/.staging.XXXXXX")"
    cp -a "$SOURCE_ROOT/." "$stage/"
    printf '%s\n' "$REVISION" >"$stage/.rent-monitor-release"
    chmod 0644 "$stage/.rent-monitor-release"
    python3 -m venv "$stage/.venv"
    "$stage/.venv/bin/python" -m pip install --disable-pip-version-check "$stage"
    mv "$stage" "$RELEASE"
    chown -hR root:root "$RELEASE"
fi

temporary_link="$APP_ROOT/.current-$REVISION"
ln -sfn "$RELEASE" "$temporary_link"
mv -Tf "$temporary_link" "$APP_ROOT/current"

install -o root -g root -m 0644 "$UNIT_SOURCE" "$UNIT_TARGET"
systemctl daemon-reload

if [[ -f "$TOKEN_FILE" ]]; then
    token_uid="$(stat -c '%u' "$TOKEN_FILE")"
    token_mode="$(stat -c '%a' "$TOKEN_FILE")"
    if [[ "$token_uid" != 0 ]] || (( (8#$token_mode & 077) != 0 )); then
        echo "The Telegram credential must be root-owned and inaccessible to group/others." >&2
        exit 1
    fi
    if systemctl is-active --quiet rent-monitor.service; then
        systemctl restart rent-monitor.service
    else
        systemctl enable --now rent-monitor.service
    fi
    echo "Rent Monitor release $REVISION installed and started."
else
    echo "Release $REVISION staged. Add a Telegram token with deploy/set-token.sh before enabling the service."
fi
