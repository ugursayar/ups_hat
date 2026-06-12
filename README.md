# Waveshare UPS HAT — Battery Monitor & Safe Shutdown
*Adapted from the UPS_HAT_E software for the INA219-based UPS HAT*

---

## Files in this package

| File | Purpose |
|---|---|
| `batteryTray.py` | Desktop system-tray battery indicator |
| `ups_shutdown.py` | Headless shutdown monitor (runs as a systemd service) |
| `ups-shutdown.service` | Systemd unit file for the shutdown monitor |
| `battery.sh` | Shell launcher for the tray app |
| `battery.desktop` | Autostart entry (placed in `~/.config/autostart/`) |
| `install.sh` | One-command installer |
| `INA219.py` | Original INA219 terminal monitor (unchanged) |

---

## Quick start

```bash
# 1. Copy this folder to your Raspberry Pi (e.g. via scp or USB)
scp -r ups_hat/ pi@raspberrypi.local:~/ups_hat/

# 2. SSH in and run the installer
ssh pi@raspberrypi.local
cd ~/ups_hat
sudo bash install.sh
```

That's it. The installer will:
- Install Python packages (`smbus`, `PyQt5`)
- Enable I²C
- Add your user to the `i2c` group
- Set up the tray app to launch at desktop login
- Install and start the shutdown monitor as a systemd service

---

## Manual installation (step by step)

### Prerequisites

```bash
sudo apt update
sudo apt install -y python3-smbus python3-pyqt5
# Or via pip:
pip3 install smbus PyQt5 --break-system-packages
```

### Enable I²C

```bash
sudo raspi-config
# → Interface Options → I2C → Yes
```

Reboot, then verify your INA219 is visible:

```bash
i2cdetect -y 1
# You should see '42' in the grid
```

### Tray app (desktop autostart)

```bash
chmod +x ~/ups_hat/battery.sh ~/ups_hat/install.sh

# Create autostart directory if needed
mkdir -p ~/.config/autostart

# Install autostart entry
sed "s|PLACEHOLDER_PATH|${HOME}/ups_hat|g" battery.desktop \
    > ~/.config/autostart/battery.desktop

# Launch immediately (without rebooting)
DISPLAY=:0.0 python3 ~/ups_hat/batteryTray.py &
```

### Shutdown service (headless / background)

```bash
# Edit the service file if your install path differs from /home/pi/ups_hat
nano ups-shutdown.service

sudo cp ups-shutdown.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ups-shutdown.service

# Check it's running
sudo systemctl status ups-shutdown.service
```

---

## Configuration

Open `batteryTray.py` **and** `ups_shutdown.py` and adjust the constants near
the top of each file:

```python
I2C_ADDR              = 0x42   # INA219 I²C address (confirm with i2cdetect -y 1)

# 2S Li-ion pack voltages
BATT_FULL_V           = 8.4    # 100% — 4.20 V/cell × 2
BATT_EMPTY_V          = 6.0    # 0%   — 3.00 V/cell × 2

LOW_VOLTAGE_V         = 6.4    # Warn at this voltage (3.20 V/cell)
SHUTDOWN_VOLTAGE_V    = 6.2    # Hard shutdown floor  (3.10 V/cell)  [ups_shutdown.py only]
SHUTDOWN_SECS         = 60     # Countdown before poweroff

CHARGING_THRESHOLD_MA = 50     # mA above this = charging
```

**If your battery pack is different** (e.g. 3S), adjust `BATT_FULL_V`,
`BATT_EMPTY_V`, `LOW_VOLTAGE_V`, and `SHUTDOWN_VOLTAGE_V` accordingly.

---

## How it works

### Charging detection

The INA219 measures current direction across the shunt resistor:

- **Current > +50 mA** → charger connected (charging)
- **Current < −50 mA** → running on battery (discharging)
- **Between ±50 mA**  → idle / float

### Battery percentage

Estimated from bus voltage using a linear approximation of the 2S Li-ion
discharge curve:

```
percent = (voltage − 6.0 V) / 2.4 V × 100
```

This is a simplification; real Li-ion cells have a non-linear curve, so
the reading will be most accurate in the middle of the range.

### Safe shutdown flow

1. `ups_shutdown.py` (or `batteryTray.py`) detects voltage below `LOW_VOLTAGE_V`
   for 5 consecutive readings while not charging.
2. A 60-second countdown begins (visible in the log / tray warning dialog).
3. If a charger is connected during the countdown, it is cancelled.
4. If the countdown expires, `sudo poweroff` is called for a clean shutdown.
5. A hard-shutdown threshold (`SHUTDOWN_VOLTAGE_V = 6.2 V`) triggers an
   immediate poweroff regardless of the countdown.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| `No such file or directory: '/dev/i2c-1'` | I²C not enabled — run `sudo raspi-config` |
| `[Errno 121] Remote I/O error` | Wrong I²C address — run `i2cdetect -y 1` and update `I2C_ADDR` |
| Tray icon doesn't appear | Make sure `DISPLAY=:0.0` is set; try running `batteryTray.py` from a terminal in your desktop session |
| `PyQt5` import error | `pip3 install PyQt5 --break-system-packages` |
| Shutdown service not starting | `sudo journalctl -u ups-shutdown -n 50` |
| Percentage reads 0% when battery is full | Voltage calibration — check `BATT_FULL_V` and `BATT_EMPTY_V` |

---

## Logs

- Tray app log:      `~/ups_hat/battery.log`
- Shutdown service:  `~/ups_hat/ups_shutdown.log`  
  and `sudo journalctl -u ups-shutdown -f`
