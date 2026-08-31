# Waveshare UPS HAT — Battery Monitor & Safe Shutdown

![Waveshare UPS HAT](ups-hat.jpg)

Enhancements for the [Waveshare UPS HAT](https://www.waveshare.com/product/ups-hat.htm) for Raspberry Pi, based on the [official demo code](https://www.waveshare.com/wiki/UPS_HAT).

## Features

- **Battery monitoring tray icon** — live voltage, percentage, and charging state in the desktop system tray
- **Automatic shutdown on low battery** — configurable voltage thresholds trigger a graceful countdown shutdown
- **Auto installer** — one-command setup via `install.sh`

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

## Battery percentage: load-compensated, not raw voltage (fixed 2026-09-01)

**Symptom:** unplug the mains and the percentage falls; plug it back and it rises.
No charge moved. The reading was tracking **load**, not charge.

**Cause.** The old formula was a straight line through terminal voltage:

```python
pct = (v - 6.0) / (8.4 - 6.0) * 100
```

Two independent errors in one line.

**1. Terminal voltage sags under load.** The pack's internal resistance was measured
here by stepping CPU load and fitting V against I:

| method | current span | result |
|---|---|---|
| load step (4 busy cores) | 210 mA, residual sd 4.6 mV | **511 mΩ** |
| float noise, 10,782 samples | 396 mA about zero | 602 mΩ |

At 511 mΩ:

| load | sag | old reading fell by |
|---|---|---|
| 0.5 A | 301 mV | 12.5 points |
| 1.0 A | 602 mV | 25.1 points |
| 2.0 A | 1.20 V | **50.1 points** |

**2. Li-ion is not linear in voltage.** The curve is flat through the middle, so a
straight line over-reports badly where it matters — at 3.70 V/cell it claimed **58%**
when the pack holds about **17%**.

**Fix.** Undo the IR drop, then interpolate a real cell curve:

```python
v_oc = v - PACK_R_OHM * (current_ma / 1000)     # + is charging
pct  = soc_percent(v_oc)                        # 13-point OCV table, per cell
```

Verified in production against a 180 mA load step:

```
before   8.232V  +7.9mA   93.0%      8.128V  -182.7mA  88.7%     4.3 points of nothing
after    8.220V  -4.9mA   91.1%      8.128V  -177.9mA  90.9%     0.2 points
```

The log now prints both: `8.128V (oc 8.221V) -177.9mA 1.448W 90.9% [DIS]`.

⚠ `PACK_R_OHM` is the resistance **seen at the INA219** — pack, wiring and whatever the
charger presents. With mains unplugged the topology changes and it may differ. Re-measure
on battery to refine it.

### The charging flag was noise

`chg = current > 50 mA` against a float current of **+4.3 mA mean over −276…+120 mA**
read "charging" in 2% of samples while on mains, flipping CHG/DIS at random. Replaced with
hysteresis on a 5-sample average: on above 80 mA, off below 20 mA.

## Shutdown thresholds

| condition | action |
|---|---|
| `v < 6.2 V` and not charging | **immediate `poweroff`**, no countdown |
| `v < 6.4 V` and not charging, 5 consecutive reads (10 s) | warn, then **60 s countdown**; cancelled if voltage recovers or charging starts |

**These stayed on raw terminal voltage, deliberately.** Compensating them would let the
pack run further down before shutdown — more runtime, less margin. Under a 1 A load the
pack trips at 6.4 V terminal ≈ 7.0 V resting, so it shuts down **earlier** than strictly
needed. That is the conservative direction, and after a deep discharge destroyed quali's
eMMC on 2026-08-04 it is the right one. Changing it is a decision, not a cleanup.
