#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 0 ]]; then
    echo "run-captcha-stack.sh takes no arguments" >&2
    exit 2
fi

TOKEN_DIRECTORY=/run/rent-monitor-captcha/tokens
NOVNC_ROOT=/usr/share/novnc
pids=()

cleanup() {
    local pid
    for pid in "${pids[@]:-}"; do
        kill "$pid" 2>/dev/null || true
    done
    wait "${pids[@]:-}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

install -d -m 0700 "$TOKEN_DIRECTORY"

Xvfb :99 -screen 0 1440x1000x24 -nolisten tcp &
pids+=("$!")

for _ in {1..100}; do
    if [[ -S /tmp/.X11-unix/X99 ]]; then
        break
    fi
    if ! kill -0 "${pids[0]}" 2>/dev/null; then
        echo "Xvfb exited before creating display :99" >&2
        exit 1
    fi
    sleep 0.05
done
if [[ ! -S /tmp/.X11-unix/X99 ]]; then
    echo "Timed out waiting for display :99" >&2
    exit 1
fi

x11vnc -display :99 -rfbport 5900 -listen 127.0.0.1 -forever -shared -nopw &
pids+=("$!")

websockify \
    --web "$NOVNC_ROOT" \
    --token-plugin TokenFileName \
    --token-source "$TOKEN_DIRECTORY" \
    127.0.0.1:6080 &
pids+=("$!")

set +e
wait -n "${pids[@]}"
status=$?
set -e
exit "$status"
