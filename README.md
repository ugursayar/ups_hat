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
| **on battery, load step** | **561 mA**, residual sd 19.8 mV | **579 mΩ ← used** |
| on mains, load step | 210 mA, residual sd 4.6 mV | 511 mΩ |
| on mains, float noise, 10,782 samples | 396 mA about zero | 602 mΩ |

The battery figure is the one to use: compensation only does anything while
discharging, so it belongs in that topology, and with the charger out of the loop the
current span is nearly three times wider. The 68 mΩ gap against the mains figure is the
charger's own regulation, which is not in circuit when it matters.

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

Verified in production twice — on mains against a 180 mA step, then on battery against a
527 mA step, which is the case that matters:

```
on mains,  before   8.232V  +7.9mA  93.0%   8.128V  -182.7mA  88.7%   4.3 points of nothing
on mains,  after    8.220V  -4.9mA  91.1%   8.128V  -177.9mA  90.9%   0.2 points

on battery, old formula would read       7.712V  71.3%   7.428V  59.5%   11.8 points
on battery, actual reading               7.712V  87.6%   7.428V  88.7%    1.1 points
```

That second block is the real test: 284 mV of terminal sag across the step, and the
reported charge barely moves.

The log now prints both: `8.128V (oc 8.221V) -177.9mA 1.448W 90.9% [DIS]`.

### Validated by a current reversal

The strongest check was accidental. Unplugging and later reconnecting the mains reverses
the current through the pack, and the compensated voltage should not notice:

```
before mains   6.912 V @  -759.6 mA  ->  V_oc 7.352 V   14.9%
after  mains   7.892 V @  +898.7 mA  ->  V_oc 7.372 V   15.5%
```

**Terminal voltage jumped 980 mV across a 1658 mA reversal. Compensated V_oc moved 20 mV.**
A wrong `R` of δ would show as a jump of δ × 1.658 A, so 20 mV puts δ at **12 mΩ** — the
579 mΩ figure is good to about 2%.

The old formula across the same instant would have read **38.0% → 78.8%**, jumping 40 points
at the moment a plug went in.

⚠ `PACK_R_OHM` is the resistance **seen at the INA219** — pack, wiring and whatever the
charger presents. With mains unplugged the topology changes and it may differ. Re-measure
on battery to refine it.

### The charging flag was noise

`chg = current > 50 mA` against a float current of **+4.3 mA mean over −276…+120 mA**
read "charging" in 2% of samples while on mains, flipping CHG/DIS at random. Replaced with
hysteresis on a 5-sample average: on above 80 mA, off below 20 mA.

## Shutdown thresholds — on compensated voltage (changed 2026-09-01)

| condition | action |
|---|---|
| `v_oc < 6.2 V` and not charging | **immediate `poweroff`** |
| `v_oc < 6.4 V` and not charging, 5 reads (10 s) | warn, then **60 s countdown**; cancelled if voltage recovers or charging starts |
| **`v` (raw) < 5.6 V** | **immediate `poweroff`, compensation bypassed** |

**`6.4` and `6.2` were always meant as resting per-cell figures** — 3.20 and 3.10 V/cell.
Applied to a terminal reading taken while the pack delivers an amp they fired early:

| load | fired at (terminal) before | now |
|---|---|---|
| 0.40 A | 6.40 V | 6.17 V |
| 0.76 A | 6.40 V | 5.96 V |
| 1.20 A | 6.40 V | 5.71 V |

The gain in charge is modest — 3.42 V/cell is ~9% and 3.20 V/cell ~5%, because the curve
is steep there — but the behaviour is now what the constants always claimed.

### Three guards, because every error here delays shutdown

Compensation can only **raise** the apparent voltage, so every way it can go wrong makes
the Pi run longer on a flatter battery. Bounded three ways:

| guard | value | catches |
|---|---|---|
| `SANE_CURRENT_A` | 5.0 A | a bad I²C read; beyond this, no compensation at all |
| `MAX_COMPENSATION_V` | 0.80 V | a plausible-but-large current inflating V_oc (≈1.4 A at 579 mΩ) |
| `RAW_FLOOR_V` | 5.60 V | everything else — raw terminal volts, no compensation, no `not charging` exemption |

Tested:

```
6.40 V @  -0.76 A  ->  6.840   normal, +0.440 V
6.40 V @ -20    A  ->  6.400   absurd current ignored
6.40 V @  -4    A  ->  7.200   capped (uncapped would be 8.716)
7.90 V @  +0.90 A  ->  7.379   charging, correction inverts
```

Worst case, if the compensation misbehaves entirely, the raw floor at 5.60 V — 2.80 V/cell
under load — still fires.


## The supply is at its limit — the HackRF stops the pack charging

Observed 2026-09-01: plugging in the HackRF made the reported charge fall several points
immediately. It is real, and it is not the compensation.

The PortaPack re-enumerated as `1d50:6018` (Mayhem UI mode, which is when it charges its
own battery) and took the charger's entire surplus:

| | mean pack current | percentage | terminal |
|---|---|---|---|
| before HackRF | **+227.5 mA** charging | 82.4 → 84.3 ↑ | 8.196 V |
| after HackRF | **−21.4 mA** discharging | 84.7 → 81.5 ↓ | 8.024 V |
| settled | +10 mA | flat | 8.03 V |

**Not a compensation artifact.** At ~0 mA the correction is **6 mV** — the displayed figure
is effectively raw terminal voltage, so the load-compensation work is not involved in this
drop in either direction.

Two real effects make it up:

1. **Charging stopped.** The pack no longer gains from charge current, and briefly supplied.
2. **Surface-charge relaxation.** A Li-ion cell reads high for 10–30 minutes after charging
   and settles as the surface charge equalises. Part of that 84.7% was never really there.
   Any percentage read within half an hour of charging is optimistic.

### What it means operationally

- **The Pi's pack will not recharge while the HackRF charges.** It sits balanced near 0 mA
  and stays where it is.
- **Extra load tips it negative.** Four busy cores draw 1.2 A here; with the HackRF also
  charging, that comes out of the pack — **while plugged into mains**.

Nothing unsafe: the protection works and the pack is at ~81%. But "on mains" no longer
implies "charging", and for a long session the HackRF wants its own supply.


## Surface charge: readings are optimistic for ~20 min after charging

Unplugging the mains dropped the reported charge from **81% to ~50%** within six minutes.
That looked like the compensation failing again. It was not — the 81% was the wrong number.

**The compensation is working.** Two samples 30 s apart under very different load:

```
terminal 6.860 V @ -1324 mA  ->  V_oc 7.627
terminal 7.196 V @  -847 mA  ->  V_oc 7.686
         336 mV of terminal swing became 59 mV of V_oc  -- 82% of the load effect removed
```

At steady ~800 mA, V_oc held flat within **16 mV** over two minutes.

**And the fall cannot be capacity.** V_oc went 8.13 → 7.61 V in six minutes at ~1.2 A, which
is **0.12 Ah**. On the curve 4.06 → 3.81 V/cell is ~84% → ~50%; a 34-point fall from 0.12 Ah
would need a **0.4 Ah pack**. This is 2× 18650, about 3 Ah. The arithmetic rules it out.

What actually happened is **surface charge**. The pack had been charging until the HackRF took
the charger's surplus, and a recently-charged cell sits well above its equilibrium voltage.
A 1.2 A load strips that in minutes, and the sequence shows it settling rather than falling:

```
8.126 -> 7.987 -> 7.893 -> 7.797 -> 7.664 -> 7.627  then flat at 7.61-7.69
```

Real discharge does not level off like that.

**This is inherent to any voltage-based state of charge**, not a flaw in this formula.
Voltage reports the cell's equilibrium potential, and after charging it takes tens of minutes
at rest — or minutes under load — to actually get there. The cure is **coulomb counting**:
integrate the INA219's current, which measures charge directly, and demote voltage to a slow
correction. Not yet done.

Until then the log marks the window: `... 62.7% [CHG] ~settling`, for `SETTLE_SECS` (20 min)
after charging stops.

### R re-measured: 610 mΩ

Fitted at ~1 A load, and **with a time term** — `V = a + b·t + R·I`. Over anything longer
than a few tens of seconds the pack visibly drains, and a plain V-against-I fit charges that
drift to the current instead. The same data gave **325 mΩ without the time term and 610 mΩ
with it**; the residual fell from 126 mV to 16 mV.

| measurement | span | R |
|---|---|---|
| **on battery, ~1 A load, time term** | 677 mA | **610 mΩ ← used** |
| on battery, lighter load | 561 mA | 579 mΩ |
| on mains, load step | 210 mA | 511 mΩ |
| on mains, float noise | 396 mA | 602 mΩ |

The spread is real: R depends on current, charge and temperature, so a single constant is an
approximation. It is a good one — 610 vs 579 is 24 mV at 0.8 A — but it is not exact.

## Coulomb counting (2026-09-01)

Voltage reports a cell's *equilibrium* potential. Right after charging it is nowhere near
equilibrium — surface charge was worth **~30 points** here — and under load it is masked by
IR drop. Integrating current measures charge directly instead of inferring it.

```
soc_ah += I · dt                      every poll, I positive = charging
```

The integrator drifts (INA219 offset accumulates), so voltage is kept as a **slow anchor**:
**the counter owns the short term, voltage owns the long term.**

| mechanism | when | what it does |
|---|---|---|
| integrate | always | tracks charge in and out |
| voltage anchor | `\|I\| < 50 mA` **and** ≥20 min since charging | pulls toward the voltage estimate at 2%/sample (~2 min) |
| full reset | charging, ≥4.15 V/cell, current tapered | sets 100% — the one point where voltage is unambiguous |
| capacity learning | between two anchored points ≥25 points apart | Ah moved ÷ fraction moved |

State persists to `/var/lib/ups_hat/state.json`, written atomically every 30 s so a power
cut cannot leave it half-written. The log now shows both opinions:

```
8.112V (oc 7.931V)  +296.0mA  2.033W  75.6% (v 75.1%)  [CHG] ~settling
                                       ^counted  ^voltage
```

### ⚠ Safety is deliberately NOT on the counter

All three shutdown paths still read voltage only — `v` for the raw floor, `v_oc` for the two
thresholds. **An integrator that has drifted must never be able to talk the monitor out of
powering off.** Verified after each change:

```
if v     < RAW_FLOOR_V           # raw terminal, bypasses everything
if v_oc  < SHUTDOWN_VOLTAGE_V    # hard
if v_oc  < LOW_VOLTAGE_V         # warn + countdown
```

### Capacity, and what it says about the pack

Seeded at **0.80 Ah** and learned from use. That seed comes from a measurement, not a
datasheet: a 38-minute discharge on 2026-09-01 delivered **0.445 Ah** across a voltage-implied
69.7 points, giving **0.64 Ah** — and that under-states it, because the start was
surface-charge inflated.

Healthy 18650s in 2S would be 2–3 Ah. **If the learned figure settles near 1 Ah, that is a
statement about the cells, not a bug in the counter.** Watch the `capacity estimate` lines.

### Tested

Simulated against a pack of known capacity:

- tracks a 10-minute 1 A discharge to **0.1 points**
- with the capacity guess **44% too small**, drifts 14.9 points during discharge and the
  anchor pulls it back to **0.0** after 30 minutes at rest

⚠ The simulation derives voltage from true state of charge, so it contains **no surface
charge** — it cannot demonstrate the thing this change exists for. That needs a real
charge/discharge cycle to confirm.
