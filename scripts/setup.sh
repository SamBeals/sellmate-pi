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

echo "==> Reloading systemd"
sudo systemctl daemon-reload

echo "==> Enabling services"
sudo systemctl enable vend-api.service
sudo systemctl enable sellmate-poller.service

echo "==> Done"
echo "Next steps:"
echo "  1. sudo systemctl start vend-api"
echo "  2. sudo systemctl start sellmate-poller"
echo "  3. systemctl status vend-api --no-pager"
echo "  4. systemctl status sellmate-poller --no-pager"
