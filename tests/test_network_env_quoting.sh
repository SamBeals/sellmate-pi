#!/usr/bin/env bash
# Unit test: network.env quoting round-trip (no nmcli required).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=provisioning/network/network_env.sh
source "$ROOT/provisioning/network/network_env.sh"

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

PREFERRED="Wired connection 1"
WIFI="Shop Guest WiFi"
PM="100"
WM="700"

write_network_env "$TMP" "$PREFERRED" "$WIFI" "$PM" "$WM"
validate_network_env "$TMP" "$PREFERRED" "$WIFI" "$PM" "$WM"

# Broken pattern: unquoted spaces would fail bash -n / source.
if grep -Eq "^SELLMATE_PREFERRED_CONNECTIONS=[^'\"\\]*[[:space:]]" "$TMP"; then
  echo "FAIL: SELLMATE_PREFERRED_CONNECTIONS has unescaped spaces" >&2
  exit 1
fi
if grep -Eq "^SELLMATE_WIFI_CONNECTIONS=[^'\"\\]*[[:space:]]" "$TMP"; then
  echo "FAIL: SELLMATE_WIFI_CONNECTIONS has unescaped spaces" >&2
  exit 1
fi

echo "OK: network.env quoting"
