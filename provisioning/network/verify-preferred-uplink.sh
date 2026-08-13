#!/usr/bin/env bash
# Verify SellMate preferred-uplink NetworkManager configuration.
#
# Usage:
#   ./provisioning/network/verify-preferred-uplink.sh
#   SELLMATE_NETWORK_ENV=/etc/sellmate/network.env ./provisioning/network/verify-preferred-uplink.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=provisioning/network/network_env.sh
source "$ROOT/provisioning/network/network_env.sh"

ENV_FILE="${SELLMATE_NETWORK_ENV:-/etc/sellmate/network.env}"
PREFERRED_METRIC="100"
WIFI_METRIC="700"
PREFERRED_CONNECTIONS=""
WIFI_CONNECTIONS=""
FAIL=0

if ! command -v nmcli >/dev/null 2>&1; then
  echo "FAIL: nmcli not found" >&2
  exit 1
fi

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  PREFERRED_CONNECTIONS="${SELLMATE_PREFERRED_CONNECTIONS:-}"
  WIFI_CONNECTIONS="${SELLMATE_WIFI_CONNECTIONS:-}"
  PREFERRED_METRIC="${SELLMATE_PREFERRED_METRIC:-100}"
  WIFI_METRIC="${SELLMATE_WIFI_METRIC:-700}"
  echo "Loaded config: $ENV_FILE"
else
  echo "Note: $ENV_FILE not found; using defaults (ethernet metric $PREFERRED_METRIC, wifi $WIFI_METRIC)."
fi

echo
echo "=== Active NetworkManager connections ==="
nmcli -f NAME,TYPE,DEVICE,STATE connection show --active || true

echo
echo "=== All connection profiles (name / type / autoconnect / ipv4.route-metric) ==="
while IFS=':' read -r name ctype; do
  [[ -z "$name" ]] && continue
  autoconnect="$(nmcli -g connection.autoconnect connection show "$name" 2>/dev/null || echo "?")"
  metric="$(nmcli -g ipv4.route-metric connection show "$name" 2>/dev/null || echo "?")"
  # NM prints -1 when unset (default).
  printf '  %-40s  type=%-18s  autoconnect=%-5s  metric=%s\n' \
    "$name" "$ctype" "$autoconnect" "$metric"
done < <(nmcli -t -f NAME,TYPE connection show 2>/dev/null)

echo
echo "=== Default IPv4 routes ==="
if ! ip -4 route show default; then
  echo "  (no default route)"
  FAIL=1
fi

echo
echo "=== Interface owning the current default route ==="
DEFAULT_DEV="$(ip -4 route show default 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="dev") {print $(i+1); exit}}')"
DEFAULT_METRIC="$(ip -4 route show default 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="metric") {print $(i+1); exit}}')"
if [[ -n "$DEFAULT_DEV" ]]; then
  echo "  device: $DEFAULT_DEV"
  echo "  metric: ${DEFAULT_METRIC:-"(kernel/NM default)"}"
  # Map device → connection name when possible.
  CONN="$(nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null | awk -F: -v d="$DEFAULT_DEV" '$2==d {print $1; exit}')"
  if [[ -n "$CONN" ]]; then
    echo "  connection: $CONN"
    CTYPE="$(nmcli -t -f CONNECTION.TYPE connection show "$CONN" 2>/dev/null || true)"
    [[ -z "$CTYPE" ]] && CTYPE="$(nmcli -t -f NAME,TYPE connection show 2>/dev/null | awk -F: -v n="$CONN" '$1==n {print $2; exit}')"
    echo "  type: ${CTYPE:-unknown}"
  fi
else
  echo "  (could not determine default device)"
  FAIL=1
fi

connections_of_type() {
  local want_type="$1"
  nmcli -t -f NAME,TYPE connection show 2>/dev/null | while IFS=':' read -r name ctype; do
    [[ -z "$name" ]] && continue
    if [[ "$ctype" == "$want_type" ]]; then
      printf '%s\n' "$name"
    fi
  done
}

split_csv() {
  local raw="$1"
  local part
  [[ -z "$raw" ]] && return 0
  IFS=',' read -r -a parts <<< "$raw"
  for part in "${parts[@]}"; do
    part="${part#"${part%%[![:space:]]*}"}"
    part="${part%"${part##*[![:space:]]}"}"
    [[ -n "$part" ]] && printf '%s\n' "$part"
  done
}

resolve_names() {
  local csv="$1"
  local fallback_type="$2"
  if [[ -n "$csv" ]]; then
    split_csv "$csv"
  else
    connections_of_type "$fallback_type"
  fi
}

mapfile -t PREFERRED_LIST < <(resolve_names "$PREFERRED_CONNECTIONS" "802-3-ethernet")
mapfile -t WIFI_LIST < <(resolve_names "$WIFI_CONNECTIONS" "802-11-wireless")

echo
echo "=== Preference check ==="
if [[ ${#PREFERRED_LIST[@]} -eq 0 ]]; then
  echo "FAIL: no preferred (ethernet) profiles resolved"
  FAIL=1
else
  echo "Preferred profiles (expect metric=$PREFERRED_METRIC, autoconnect=yes):"
  for name in "${PREFERRED_LIST[@]}"; do
    metric="$(nmcli -g ipv4.route-metric connection show "$name" 2>/dev/null || echo missing)"
    autoconnect="$(nmcli -g connection.autoconnect connection show "$name" 2>/dev/null || echo missing)"
    echo "  $name  metric=$metric  autoconnect=$autoconnect"
    if [[ "$metric" != "$PREFERRED_METRIC" ]]; then
      echo "    FAIL: expected metric $PREFERRED_METRIC"
      FAIL=1
    fi
    if [[ "$autoconnect" != "yes" ]]; then
      echo "    FAIL: expected autoconnect=yes"
      FAIL=1
    fi
  done
fi

if [[ ${#WIFI_LIST[@]} -eq 0 ]]; then
  echo "Wi-Fi profiles: none (OK if Wi-Fi not configured yet)"
else
  echo "Wi-Fi profiles (expect metric=$WIFI_METRIC, autoconnect=yes):"
  for name in "${WIFI_LIST[@]}"; do
    metric="$(nmcli -g ipv4.route-metric connection show "$name" 2>/dev/null || echo missing)"
    autoconnect="$(nmcli -g connection.autoconnect connection show "$name" 2>/dev/null || echo missing)"
    echo "  $name  metric=$metric  autoconnect=$autoconnect"
    if [[ "$metric" != "$WIFI_METRIC" ]]; then
      echo "    FAIL: expected metric $WIFI_METRIC"
      FAIL=1
    fi
    if [[ "$autoconnect" != "yes" ]]; then
      echo "    FAIL: expected autoconnect=yes"
      FAIL=1
    fi
    if [[ "$metric" =~ ^[0-9]+$ && "$PREFERRED_METRIC" =~ ^[0-9]+$ ]]; then
      if [[ "$metric" -le "$PREFERRED_METRIC" ]]; then
        echo "    FAIL: Wi-Fi metric ($metric) must be greater than preferred ($PREFERRED_METRIC)"
        FAIL=1
      fi
    fi
  done
fi

echo
if [[ "$FAIL" -eq 0 ]]; then
  echo "OK: preferred-uplink metrics look correct."
  echo "Failover check (manual): unplug preferred uplink, re-run this script,"
  echo "confirm default route moves to Wi-Fi; replug and confirm preferred returns."
  exit 0
fi

echo "FAIL: preferred-uplink verification found problems." >&2
echo "Re-run: sudo $ROOT/provisioning/network/install-preferred-uplink.sh" >&2
exit 1
