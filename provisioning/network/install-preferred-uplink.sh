#!/usr/bin/env bash
# Provision NetworkManager so the preferred uplink (Ethernet/router/LTE
# gateway) beats Wi-Fi via ipv4.route-metric. Failover/failback is automatic.
#
# Usage (on the Pi, from the sellmate-pi repo):
#   sudo ./provisioning/network/install-preferred-uplink.sh
#   sudo ./provisioning/network/install-preferred-uplink.sh \
#        --preferred "Wired connection 1" --wifi "SellMate-Shop"
#   sudo ./provisioning/network/install-preferred-uplink.sh --dry-run
#
# Does not hard-code modem vendors. Empty preferred/wifi lists mean:
#   preferred → all 802-3-ethernet profiles
#   wifi      → all 802-11-wireless profiles
#
# After install:
#   ./provisioning/network/verify-preferred-uplink.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=provisioning/network/network_env.sh
source "$ROOT/provisioning/network/network_env.sh"

ENV_DST="/etc/sellmate/network.env"

PREFERRED_CONNECTIONS=""
WIFI_CONNECTIONS=""
PREFERRED_METRIC="100"
WIFI_METRIC="700"
DRY_RUN=0
PREFERRED_FROM_CLI=0
WIFI_FROM_CLI=0
PREFERRED_METRIC_FROM_CLI=0
WIFI_METRIC_FROM_CLI=0

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --preferred)
      PREFERRED_CONNECTIONS="${2:-}"
      PREFERRED_FROM_CLI=1
      shift 2
      ;;
    --wifi)
      WIFI_CONNECTIONS="${2:-}"
      WIFI_FROM_CLI=1
      shift 2
      ;;
    --preferred-metric)
      PREFERRED_METRIC="${2:-}"
      PREFERRED_METRIC_FROM_CLI=1
      shift 2
      ;;
    --wifi-metric)
      WIFI_METRIC="${2:-}"
      WIFI_METRIC_FROM_CLI=1
      shift 2
      ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage 0 ;;
    *) echo "Unknown argument: $1" >&2; usage 1 ;;
  esac
done

if [[ "$(id -u)" -ne 0 && "$DRY_RUN" -ne 1 ]]; then
  echo "Run as root (sudo) so NetworkManager profiles and $ENV_DST can be updated." >&2
  exit 1
fi

if ! command -v nmcli >/dev/null 2>&1; then
  echo "nmcli not found. Install/enable NetworkManager first (Raspberry Pi OS default)." >&2
  exit 1
fi

if ! systemctl is-active --quiet NetworkManager 2>/dev/null; then
  echo "Warning: NetworkManager does not appear active; nmcli may still work." >&2
fi

if [[ -f "$ENV_DST" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_DST"
  if [[ "$PREFERRED_FROM_CLI" -eq 0 && -n "${SELLMATE_PREFERRED_CONNECTIONS+x}" ]]; then
    PREFERRED_CONNECTIONS="${SELLMATE_PREFERRED_CONNECTIONS}"
  fi
  if [[ "$WIFI_FROM_CLI" -eq 0 && -n "${SELLMATE_WIFI_CONNECTIONS+x}" ]]; then
    WIFI_CONNECTIONS="${SELLMATE_WIFI_CONNECTIONS}"
  fi
  if [[ "$PREFERRED_METRIC_FROM_CLI" -eq 0 && -n "${SELLMATE_PREFERRED_METRIC:-}" ]]; then
    PREFERRED_METRIC="${SELLMATE_PREFERRED_METRIC}"
  fi
  if [[ "$WIFI_METRIC_FROM_CLI" -eq 0 && -n "${SELLMATE_WIFI_METRIC:-}" ]]; then
    WIFI_METRIC="${SELLMATE_WIFI_METRIC}"
  fi
fi

if ! [[ "$PREFERRED_METRIC" =~ ^[0-9]+$ ]]; then
  echo "Invalid --preferred-metric '$PREFERRED_METRIC' (need integer)." >&2
  exit 1
fi
if ! [[ "$WIFI_METRIC" =~ ^[0-9]+$ ]]; then
  echo "Invalid --wifi-metric '$WIFI_METRIC' (need integer)." >&2
  exit 1
fi
if [[ "$PREFERRED_METRIC" -ge "$WIFI_METRIC" ]]; then
  echo "Preferred metric ($PREFERRED_METRIC) must be strictly less than Wi-Fi metric ($WIFI_METRIC)." >&2
  exit 1
fi

# --- Profile resolution -------------------------------------------------------

connections_of_type() {
  # Print NM connection names matching TYPE (one per line).
  local want_type="$1"
  nmcli -t -f NAME,TYPE connection show 2>/dev/null | while IFS=':' read -r name ctype; do
    [[ -z "$name" ]] && continue
    if [[ "$ctype" == "$want_type" ]]; then
      printf '%s\n' "$name"
    fi
  done
}

split_csv() {
  # split_csv "a, b,c" → lines a / b / c (trim spaces)
  local raw="$1"
  local part
  if [[ -z "$raw" ]]; then
    return 0
  fi
  IFS=',' read -r -a parts <<< "$raw"
  for part in "${parts[@]}"; do
    part="${part#"${part%%[![:space:]]*}"}"
    part="${part%"${part##*[![:space:]]}"}"
    [[ -n "$part" ]] && printf '%s\n' "$part"
  done
}

connection_exists() {
  local name="$1"
  nmcli -t -f NAME connection show 2>/dev/null | grep -Fxq "$name"
}

resolve_list() {
  # resolve_list <csv_or_empty> <nm_type_fallback>
  # Prints connection names, one per line. Errors if explicit name missing.
  local csv="$1"
  local fallback_type="$2"
  local name
  if [[ -n "$csv" ]]; then
    while IFS= read -r name; do
      [[ -z "$name" ]] && continue
      if ! connection_exists "$name"; then
        echo "NetworkManager connection not found: '$name'" >&2
        return 1
      fi
      printf '%s\n' "$name"
    done < <(split_csv "$csv")
  else
    connections_of_type "$fallback_type"
  fi
}

mapfile -t PREFERRED_LIST < <(resolve_list "$PREFERRED_CONNECTIONS" "802-3-ethernet")
mapfile -t WIFI_LIST < <(resolve_list "$WIFI_CONNECTIONS" "802-11-wireless")

if [[ ${#PREFERRED_LIST[@]} -eq 0 ]]; then
  echo "No preferred uplink profiles resolved." >&2
  echo "Plug in the router Ethernet (or pass --preferred \"Exact NM Name\")." >&2
  echo "Available connections:" >&2
  nmcli -t -f NAME,TYPE connection show >&2 || true
  exit 1
fi

echo "SellMate preferred-uplink provisioning:"
echo "  preferred metric: $PREFERRED_METRIC"
echo "  wifi metric:      $WIFI_METRIC"
echo "  preferred profiles:"
for name in "${PREFERRED_LIST[@]}"; do
  echo "    - $name"
done
if [[ ${#WIFI_LIST[@]} -eq 0 ]]; then
  echo "  wifi profiles:    (none found — preferred-only until Wi-Fi is configured)"
else
  echo "  wifi profiles:"
  for name in "${WIFI_LIST[@]}"; do
    echo "    - $name"
  done
fi
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "  mode:             dry-run (no changes)"
fi

apply_profile() {
  local name="$1"
  local metric="$2"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "  [dry-run] nmcli connection modify \"$name\" ipv4.route-metric $metric connection.autoconnect yes"
    return 0
  fi
  nmcli connection modify "$name" \
    ipv4.route-metric "$metric" \
    connection.autoconnect yes
  echo "  updated: $name (metric=$metric, autoconnect=yes)"

  # Re-apply metric on already-active connections without forcing down/up of
  # offline links (avoids flapping).
  local state
  state="$(nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null | awk -F: -v n="$name" '$1==n {print $2; exit}')"
  if [[ -n "$state" ]]; then
    nmcli device reapply "$state" 2>/dev/null \
      || nmcli connection up "$name" ifname "$state" 2>/dev/null \
      || true
  fi
}

echo
echo "Applying metrics…"
for name in "${PREFERRED_LIST[@]}"; do
  [[ -z "$name" ]] && continue
  apply_profile "$name" "$PREFERRED_METRIC"
done
for name in "${WIFI_LIST[@]}"; do
  [[ -z "$name" ]] && continue
  apply_profile "$name" "$WIFI_METRIC"
done

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo
  echo "Dry-run complete. Re-run without --dry-run to apply."
  exit 0
fi

install -d -m 755 /etc/sellmate
write_network_env \
  "$ENV_DST" \
  "$PREFERRED_CONNECTIONS" \
  "$WIFI_CONNECTIONS" \
  "$PREFERRED_METRIC" \
  "$WIFI_METRIC"
chmod 644 "$ENV_DST"
if ! validate_network_env \
  "$ENV_DST" \
  "$PREFERRED_CONNECTIONS" \
  "$WIFI_CONNECTIONS" \
  "$PREFERRED_METRIC" \
  "$WIFI_METRIC"; then
  echo "Refusing to continue: $ENV_DST is not safely sourceable." >&2
  exit 1
fi

echo
echo "Installed: $ENV_DST"
nmcli networking connectivity check 2>/dev/null || true

echo
echo "Next steps:"
echo "  1. $ROOT/provisioning/network/verify-preferred-uplink.sh"
echo "  2. Confirm default route uses the preferred device (Ethernet/router)."
echo "  3. Failover test: unplug preferred uplink, re-run verify, confirm Wi-Fi;"
echo "     replug and confirm preferred returns."
echo
echo "Docs: $ROOT/docs/NETWORK.md"
