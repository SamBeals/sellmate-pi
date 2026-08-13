# Shared helpers for writing / validating SellMate network.env files.
# Sourced by install-preferred-uplink.sh and verify-preferred-uplink.sh.

shell_quote() {
  # Prefer readable single quotes when the value has no apostrophes; otherwise
  # fall back to bash printf %q.
  local s="$1"
  if [[ "$s" != *"'"* ]]; then
    printf "'%s'" "$s"
  else
    printf '%q' "$s"
  fi
}

write_network_env() {
  # write_network_env <path> <preferred> <wifi> <preferred_metric> <wifi_metric>
  local path="$1"
  local preferred="$2"
  local wifi="$3"
  local preferred_metric="$4"
  local wifi_metric="$5"

  cat > "$path" <<EOF
# Managed by sellmate-pi provisioning/network/install-preferred-uplink.sh
# Values are shell-quoted so connection names with spaces source correctly.
SELLMATE_PREFERRED_CONNECTIONS=$(shell_quote "$preferred")
SELLMATE_WIFI_CONNECTIONS=$(shell_quote "$wifi")
SELLMATE_PREFERRED_METRIC=$(shell_quote "$preferred_metric")
SELLMATE_WIFI_METRIC=$(shell_quote "$wifi_metric")
EOF
}

validate_network_env() {
  # validate_network_env <path> <expect_pref> <expect_wifi> <expect_pm> <expect_wm>
  local path="$1"
  local expect_pref="$2"
  local expect_wifi="$3"
  local expect_pm="$4"
  local expect_wm="$5"

  if [[ ! -f "$path" ]]; then
    echo "network.env missing: $path" >&2
    return 1
  fi

  if ! bash -n "$path"; then
    echo "network.env failed bash -n syntax check: $path" >&2
    return 1
  fi

  # shellcheck disable=SC1090
  if ! (
    set -euo pipefail
    unset SELLMATE_PREFERRED_CONNECTIONS SELLMATE_WIFI_CONNECTIONS
    unset SELLMATE_PREFERRED_METRIC SELLMATE_WIFI_METRIC
    # shellcheck disable=SC1090
    source "$path"
    if [[ "${SELLMATE_PREFERRED_CONNECTIONS:-}" != "$expect_pref" ]]; then
      echo "SELLMATE_PREFERRED_CONNECTIONS mismatch: got '${SELLMATE_PREFERRED_CONNECTIONS:-}' want '$expect_pref'" >&2
      exit 1
    fi
    if [[ "${SELLMATE_WIFI_CONNECTIONS:-}" != "$expect_wifi" ]]; then
      echo "SELLMATE_WIFI_CONNECTIONS mismatch: got '${SELLMATE_WIFI_CONNECTIONS:-}' want '$expect_wifi'" >&2
      exit 1
    fi
    if [[ "${SELLMATE_PREFERRED_METRIC:-}" != "$expect_pm" ]]; then
      echo "SELLMATE_PREFERRED_METRIC mismatch: got '${SELLMATE_PREFERRED_METRIC:-}' want '$expect_pm'" >&2
      exit 1
    fi
    if [[ "${SELLMATE_WIFI_METRIC:-}" != "$expect_wm" ]]; then
      echo "SELLMATE_WIFI_METRIC mismatch: got '${SELLMATE_WIFI_METRIC:-}' want '$expect_wm'" >&2
      exit 1
    fi
  ); then
    echo "network.env failed source round-trip validation: $path" >&2
    return 1
  fi

  return 0
}
