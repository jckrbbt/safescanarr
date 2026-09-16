#!/usr/bin/env bash
# install.sh – sets up Safe Scanarr on a Linux server
# Run as root:  sudo bash install.sh

set -euo pipefail

INSTALL_DIR="/opt/safescanarr"
VENV_DIR="$INSTALL_DIR/venv"
SERVICE_DIR="/etc/systemd/system"
DATA_DIR="$INSTALL_DIR/data"
SERVICE_USER="safescanarr"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Safe Scanarr installer ==="

# 1. Create installation directories
echo "[1/7] Creating $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR" "$DATA_DIR/vcs"

# 2. Copy application files
echo "[2/7] Copying application files ..."
for f in scanner.py config.py database.py pathutil.py poller.py entrypoint.py requirements.txt VERSION; do
    cp "$SCRIPT_DIR/$f" "$INSTALL_DIR/$f"
done
mkdir -p "$INSTALL_DIR/web/static" "$INSTALL_DIR/web/templates"
cp -r "$SCRIPT_DIR/web/." "$INSTALL_DIR/web/"

# 3. Create Python virtual environment
echo "[3/7] Creating Python virtual environment ..."
python3 -m venv "$VENV_DIR"

# 4. Install Python dependencies
echo "[4/7] Installing Python dependencies ..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"

# 5. Create the dedicated service user and set ownership
echo "[5/7] Creating service user '$SERVICE_USER' ..."
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
chmod 750 "$DATA_DIR"

# 6. Install systemd units
echo "[6/7] Installing systemd units ..."
cp "$SCRIPT_DIR/safescanarr.service"        "$SERVICE_DIR/safescanarr.service"
cp "$SCRIPT_DIR/safescanarr.timer"          "$SERVICE_DIR/safescanarr.timer"
cp "$SCRIPT_DIR/safescanarr-poller.service" "$SERVICE_DIR/safescanarr-poller.service"

systemctl daemon-reload
systemctl enable --now safescanarr.timer
systemctl enable --now safescanarr-poller.service

# 7. Ensure the data/output directories exist and are writable
echo "[7/7] Ensuring data directories exist ..."
mkdir -p "$DATA_DIR/vcs"
chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"

echo ""
echo "=== Installation complete ==="
echo ""
echo "  Nightly scan    : daily at midnight (systemd timer)"
echo "  API poller      : systemd service safescanarr-poller"
echo "  Web UI          : run entrypoint.py manually or via Docker; listens on"
echo "                    http://127.0.0.1:8666 by default (set WEB_HOST=0.0.0.0 for LAN)"
echo "  Service user    : $SERVICE_USER (non-root)"
echo "  Config file     : $DATA_DIR/config.json (mode 0600)"
echo "  Database        : $DATA_DIR/safescanarr.db"
echo "  Log file        : $DATA_DIR/safescanarr.log"
echo "  Output sheets   : $DATA_DIR/vcs/"
echo ""
echo "First run: open http://<host>:8666 and complete the web setup to choose"
echo "  password or token authentication."
echo ""
echo "Recovery (run as the service user):"
echo "  python3 -m web.authtool reset"
echo "  python3 -m web.authtool set-password"
echo "  python3 -m web.authtool set-token --show"
echo ""
echo "Useful commands:"
echo "  Run a scan now        : sudo -u $SERVICE_USER $VENV_DIR/bin/python $INSTALL_DIR/scanner.py --scan"
echo "  Process one file      : sudo -u $SERVICE_USER $VENV_DIR/bin/python $INSTALL_DIR/scanner.py --file '/path/to/file.mkv'"
echo "  List tracked files    : sudo -u $SERVICE_USER $VENV_DIR/bin/python $INSTALL_DIR/scanner.py --list"
echo "  Poller status         : systemctl status safescanarr-poller.service"
echo "  Timer status          : systemctl status safescanarr.timer"
echo "  View logs             : tail -f $DATA_DIR/safescanarr.log"
echo "  Errors only           : grep ERROR $DATA_DIR/safescanarr.log"
