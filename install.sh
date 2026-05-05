#!/usr/bin/env bash
# install.sh – sets up Safe Scanarr on a Linux server
# Run as root:  sudo bash install.sh

set -euo pipefail

INSTALL_DIR="/opt/safescanarr"
VENV_DIR="$INSTALL_DIR/venv"
HOOKS_DIR="$INSTALL_DIR/hooks"
SERVICE_DIR="/etc/systemd/system"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Safe Scanarr installer ==="

# 1. Create installation directories
echo "[1/7] Creating $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR" "$HOOKS_DIR"

# 2. Copy application files
echo "[2/7] Copying application files ..."
for f in scanner.py config.py database.py webhook_listener.py poller.py; do
    cp "$SCRIPT_DIR/$f" "$INSTALL_DIR/$f"
done

# 3. Copy hook scripts
echo "[3/7] Copying hook scripts ..."
cp "$SCRIPT_DIR/hooks/sonarr_hook.sh" "$HOOKS_DIR/sonarr_hook.sh"
cp "$SCRIPT_DIR/hooks/radarr_hook.sh" "$HOOKS_DIR/radarr_hook.sh"
chmod +x "$HOOKS_DIR/sonarr_hook.sh" "$HOOKS_DIR/radarr_hook.sh"

# 4. Create Python virtual environment
echo "[4/7] Creating Python virtual environment ..."
python3 -m venv "$VENV_DIR"

# 5. Install vcsi
echo "[5/7] Installing vcsi ..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet vcsi

# 6. Install systemd units
echo "[6/7] Installing systemd units ..."
cp "$SCRIPT_DIR/safescanarr.service"         "$SERVICE_DIR/safescanarr.service"
cp "$SCRIPT_DIR/safescanarr.timer"           "$SERVICE_DIR/safescanarr.timer"
cp "$SCRIPT_DIR/safescanarr-webhook.service" "$SERVICE_DIR/safescanarr-webhook.service"
cp "$SCRIPT_DIR/safescanarr-poller.service"  "$SERVICE_DIR/safescanarr-poller.service"

systemctl daemon-reload
systemctl enable --now safescanarr.timer
systemctl enable --now safescanarr-webhook.service
systemctl enable --now safescanarr-poller.service

# 7. Create output directory
echo "[7/7] Ensuring output directory exists ..."
mkdir -p "/opt/safescanarr/data/vcs"

echo ""
echo "=== Installation complete ==="
echo ""
echo "  Nightly scan    : daily at midnight"
echo "  API poller      : every 10 minutes (Sonarr + Radarr)"
echo "  Webhook listener: http://0.0.0.0:8585"
echo "  Config file     : $INSTALL_DIR/config.py"
echo "  Database        : $INSTALL_DIR/safescanarr.db"
echo "  Log file        : $INSTALL_DIR/safescanarr.log"
echo "  Output sheets   : /opt/safescanarr/data/vcs/"
echo ""
echo "Useful commands:"
echo "  Run a scan now        : sudo $VENV_DIR/bin/python $INSTALL_DIR/scanner.py --scan"
echo "  Process one file      : sudo $VENV_DIR/bin/python $INSTALL_DIR/scanner.py --file '/path/to/file.mkv'"
echo "  List tracked files    : sudo $VENV_DIR/bin/python $INSTALL_DIR/scanner.py --list"
echo "  Poller status         : systemctl status safescanarr-poller.service"
echo "  Webhook status        : systemctl status safescanarr-webhook.service"
echo "  View logs             : tail -f $INSTALL_DIR/safescanarr.log"
echo "  Errors only           : grep ERROR $INSTALL_DIR/safescanarr.log"
