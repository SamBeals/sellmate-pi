#!/bin/bash
set -e

REPO_DIR="$HOME/sellmate-pi"
VENV_DIR="$REPO_DIR/.venv"
MACHINE_ENV_DIR="/etc/sellmate"
MACHINE_ENV_FILE="$MACHINE_ENV_DIR/machine.env"
MACHINE_ENV_EXAMPLE="$REPO_DIR/services/machine.env.example"

echo "==> Updating apt package list"
sudo apt update

echo "==> Installing python venv tools and i2c utilities"
sudo apt install -y python3-venv i2c-tools

echo "==> Creating virtual environment"
python3 -m venv "$VENV_DIR"

echo "==> Installing Python dependencies"
source "$VENV_DIR/bin/activate"
pip install --upgrade pip
pip install -r "$REPO_DIR/requirements.txt"

echo "==> Installing per-machine environment file"
sudo mkdir -p "$MACHINE_ENV_DIR"
if [ ! -f "$MACHINE_ENV_FILE" ]; then
  sudo cp "$MACHINE_ENV_EXAMPLE" "$MACHINE_ENV_FILE"
  echo "    Created $MACHINE_ENV_FILE from example."
  echo "    IMPORTANT: Edit MACHINE_ID to a unique value for this Pi before starting services."
else
  echo "    Keeping existing $MACHINE_ENV_FILE (not overwritten)."
fi
sudo chown root:root "$MACHINE_ENV_FILE"
sudo chmod 600 "$MACHINE_ENV_FILE"

echo "==> Installing systemd services"
sudo cp "$REPO_DIR/services/vend-api.service" /etc/systemd/system/
sudo cp "$REPO_DIR/services/sellmate-poller.service" /etc/systemd/system/
sudo cp "$REPO_DIR/services/sellmate-health.service" /etc/systemd/system/

echo "==> Reloading systemd"
sudo systemctl daemon-reload

echo "==> Enabling services"
sudo systemctl enable vend-api.service
sudo systemctl enable sellmate-poller.service
sudo systemctl enable sellmate-health.service

echo "==> Done"
echo ""
echo "Machine identity (single source of truth for poller + health):"
echo "  $MACHINE_ENV_FILE"
echo "  Contains MACHINE_ID, CLOUD_BASE, MACHINE_SHARED_TOKEN"
echo "  Both sellmate-poller and sellmate-health load this file."
echo ""
echo "Next steps:"
echo "  1. sudo nano $MACHINE_ENV_FILE"
echo "       - Set a UNIQUE MACHINE_ID for this Pi (required; no default)"
echo "       - Set MACHINE_SHARED_TOKEN to match Cloud Run"
echo "       - Confirm CLOUD_BASE"
echo "  2. sudo chmod 600 $MACHINE_ENV_FILE"
echo "  3. sudo systemctl daemon-reload"
echo "  4. sudo systemctl restart sellmate-poller sellmate-health"
echo "     (or start for first install:)"
echo "       sudo systemctl start vend-api"
echo "       sudo systemctl start sellmate-poller"
echo "       sudo systemctl start sellmate-health"
echo "  5. systemctl status vend-api sellmate-poller sellmate-health --no-pager"
echo "  6. Confirm both services see the same identity:"
echo "       systemctl show sellmate-poller -p EnvironmentFiles --no-pager"
echo "       journalctl -u sellmate-poller -u sellmate-health -n 40 --no-pager"
echo "  7. Manual health snapshot:"
echo "       cd ~/sellmate-pi && set -a && source $MACHINE_ENV_FILE && set +a"
echo "       python3 -m app.health_reporter --once"
echo "       python3 -m app.health_reporter --once --submit"
echo "  8. (optional) python app/tof_sensor_test.py  # live VL53L1X feed"
echo "     Unit tests: MACHINE_ID=machine_test python3 -m unittest discover -s tests -v"
echo "  9. Enable ToF after calibration by setting TOF_VERIFICATION_ENABLED=true"
echo "     in both vend-api.service and sellmate-poller.service, then:"
echo "       sudo systemctl daemon-reload"
echo "       sudo systemctl restart vend-api sellmate-poller"
echo " 10. Poller uses short-poll (CLAIM_WAIT_SECONDS=0, POLL_INTERVAL_SECONDS=5)."
echo "     Do not set LONG_POLL_SECONDS; it is removed."
echo ""
echo "NOTE: Missing MACHINE_ID causes poller/health to fail at startup on purpose."
echo "Future hardening: bind Cloud credentials per machine_id (not in this change)."
