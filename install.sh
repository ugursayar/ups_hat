#!/usr/bin/env bash
# install.sh — Installer for the Waveshare UPS HAT battery monitor
# Run once:  bash install.sh
# ─────────────────────────────────────────────────────────────────────────────
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="${SUDO_USER:-$USER}"
HOME_DIR="$(eval echo "~${USER_NAME}")"
AUTOSTART_DIR="${HOME_DIR}/.config/autostart"

echo "==========================================="
echo " Waveshare UPS HAT — Installer"
echo " Install dir : ${SCRIPT_DIR}"
echo " User        : ${USER_NAME}"
echo "==========================================="

# ── 1. Install Python dependencies ───────────────────────────────────────────
echo "[1/4] Installing Python dependencies…"
pip3 install smbus PyQt5 --break-system-packages 2>/dev/null \
  || pip3 install smbus PyQt5 \
  || echo "      (pip install skipped — packages may already be present)"

# ── 2. Enable I²C if not already enabled ─────────────────────────────────────
echo "[2/4] Checking I²C…"
if ! lsmod | grep -q i2c_dev; then
    echo "      Enabling I²C via raspi-config…"
    raspi-config nonint do_i2c 0 || true
fi
if ! groups "${USER_NAME}" | grep -q i2c; then
    echo "      Adding ${USER_NAME} to i2c group…"
    usermod -aG i2c "${USER_NAME}"
fi

# ── 3. Desktop autostart (tray app) ──────────────────────────────────────────
echo "[3/4] Setting up desktop autostart…"
mkdir -p "${AUTOSTART_DIR}"
sed "s|PLACEHOLDER_PATH|${SCRIPT_DIR}|g" \
    "${SCRIPT_DIR}/battery.desktop" \
    > "${AUTOSTART_DIR}/battery.desktop"
chmod 644 "${AUTOSTART_DIR}/battery.desktop"
chmod +x   "${SCRIPT_DIR}/battery.sh"
echo "      → ${AUTOSTART_DIR}/battery.desktop"

# ── 4. Systemd shutdown service ───────────────────────────────────────────────
echo "[4/4] Installing systemd shutdown service…"
# Patch the service file to use the real install path
sed "s|/home/pi/ups_hat|${SCRIPT_DIR}|g" \
    "${SCRIPT_DIR}/ups-shutdown.service" \
    | sudo tee /etc/systemd/system/ups-shutdown.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now ups-shutdown.service
echo "      → /etc/systemd/system/ups-shutdown.service (enabled & started)"

echo ""
echo "✓  Installation complete!"
echo ""
echo "  Tray app   : starts automatically at next desktop login"
echo "               (or run now: bash ${SCRIPT_DIR}/battery.sh)"
echo ""
echo "  Shutdown service status:"
sudo systemctl status ups-shutdown.service --no-pager -l | head -20
echo ""
echo "  Verify your INA219 is detected:"
echo "  → i2cdetect -y 1   (look for '42')"
