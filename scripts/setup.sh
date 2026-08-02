#!/bin/bash
set -e

REPO_DIR="$HOME/sellmate-pi"
VENV_DIR="$REPO_DIR/.venv"

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
echo "Next steps:"
echo "  1. Edit /etc/systemd/system/sellmate-health.service:"
echo "       MACHINE_ID, CLOUD_BASE, MACHINE_SHARED_TOKEN (must match Cloud Run)"
echo "  2. sudo systemctl daemon-reload"
echo "  3. sudo systemctl start vend-api"
echo "  4. sudo systemctl start sellmate-poller"
echo "  5. sudo systemctl start sellmate-health"
echo "  6. systemctl status vend-api sellmate-poller sellmate-health --no-pager"
echo "  7. Manual health snapshot:"
echo "       cd ~/sellmate-pi && python3 -m app.health_reporter --once"
echo "       cd ~/sellmate-pi && python3 -m app.health_reporter --once --submit"
echo "  8. (optional) python app/tof_sensor_test.py  # live VL53L1X feed"
echo "     Unit tests: python3 -m unittest discover -s tests -v"
echo "  9. Enable ToF after calibration by setting TOF_VERIFICATION_ENABLED=true"
echo "     in both vend-api.service and sellmate-poller.service, then:"
echo "       sudo systemctl daemon-reload"
echo "       sudo systemctl restart vend-api sellmate-poller"
echo " 10. Poller uses short-poll (CLAIM_WAIT_SECONDS=0, POLL_INTERVAL_SECONDS=5)."
echo "     Do not set LONG_POLL_SECONDS; it is removed."
