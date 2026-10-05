#!/usr/bin/env bash
set -euo pipefail
install -d -m 0755 /etc/apt/keyrings
curl --fail --silent --show-error https://packages.mozilla.org/apt/repo-signing-key.gpg \
    -o /etc/apt/keyrings/packages.mozilla.org.asc
fingerprint=$(gpg --show-keys --with-colons /etc/apt/keyrings/packages.mozilla.org.asc \
    | awk -F: '$1 == "fpr" {print $10; exit}')
if [[ "$fingerprint" != 35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3 ]]; then
    echo "Mozilla signing key verification failed" >&2
    exit 1
fi
install -m 0644 /tmp/rent-monitor-mozilla.list /etc/apt/sources.list.d/rent-monitor-mozilla.list
install -m 0644 /tmp/rent-monitor-mozilla.pref /etc/apt/preferences.d/rent-monitor-mozilla
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends firefox firefox-l10n-ru
firefox --version
