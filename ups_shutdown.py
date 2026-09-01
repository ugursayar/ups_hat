#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ups_shutdown.py — Headless UPS shutdown monitor (systemd service)

Runs without a desktop / GUI.  Monitors the INA219, logs readings, and
initiates a safe 'sudo poweroff' when the battery voltage drops below
LOW_VOLTAGE_V for CONFIRM_CYCLES consecutive readings while not charging.

Install as a service:
    sudo cp ups-shutdown.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now ups-shutdown.service

Or run once:
    sudo python3 ups_shutdown.py
"""

import json
import os
import sys
import time
import logging
import signal

import smbus

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ups_shutdown.log")
logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)

# ── INA219 register map ───────────────────────────────────────────────────────
_REG_CONFIG       = 0x00
_REG_SHUNTVOLTAGE = 0x01
_REG_BUSVOLTAGE   = 0x02
_REG_POWER        = 0x03
_REG_CURRENT      = 0x04
_REG_CALIBRATION  = 0x05

# ── Settings ──────────────────────────────────────────────────────────────────
I2C_ADDR              = 0x42   # INA219 address — verify with: i2cdetect -y 1
POLL_INTERVAL_S       = 2      # seconds between readings
LOW_VOLTAGE_V         = 6.4    # warn threshold  (≈ 3.20 V / cell on 2S pack)
SHUTDOWN_VOLTAGE_V    = 6.2    # hard shutdown   (≈ 3.10 V / cell)
# Both thresholds are evaluated against the LOAD-COMPENSATED voltage, which is
# what "3.20 V / cell" was always meant to describe -- a resting figure, not a
# terminal reading taken while the pack is delivering an amp. On raw terminal
# volts they fired 230-700 mV early depending on load.
#
# Compensation can only ever RAISE the apparent voltage, so a bad current
# reading delays shutdown. Two guards bound that:
MAX_COMPENSATION_V    = 0.80   # cap the correction (≈1.4 A at 579 mOhm)
RAW_FLOOR_V           = 5.60   # raw terminal volts; below this, go down regardless
SANE_CURRENT_A        = 5.0    # |I| beyond this is a bad read -- do not compensate
CHARGING_THRESHOLD_MA = 50     # mA — above this = charging (legacy, see hysteresis below)
# The raw flag flipped CHG/DIS on noise: float current measures +4.3 mA mean
# over -276..+120 mA, so it read 'charging' in 2% of samples while on mains.
# Separate on/off thresholds on a smoothed current instead.
CHG_ON_MA             = 80     # smoothed current above this -> charging
CHG_OFF_MA            = 20     # and below this -> not charging
CHG_SMOOTH_N          = 5      # samples in the moving average
# A Li-ion cell sits well above its equilibrium voltage for a while after
# charging, so a voltage-derived percentage reads high. Measured here: the pack
# showed 81% on float, and once a 1.2 A load stripped the surface charge it
# settled at ~50% having delivered only 0.12 Ah -- far too little to be capacity.
# Any reading in this window is optimistic and is marked so.
SETTLE_SECS           = 1200   # 20 min after charging stops

# ── Coulomb counting ──────────────────────────────────────────────────────────
# Voltage tells you a cell's equilibrium potential, which is not where it is
# right after charging (surface charge cost ~30 points here) nor under load.
# Integrating current measures charge directly. It drifts, so voltage is kept as
# a slow anchor: the integrator owns the short term, voltage owns the long term.
#
# SAFETY: the shutdown thresholds stay on VOLTAGE. An integrator that has drifted
# must never be able to talk the monitor out of powering off.
STATE_FILE       = "/var/lib/ups_hat/state.json"
NOMINAL_CAP_AH   = 0.80    # seed only; learned from use. A discharge on
                           # 2026-09-01 implied 0.64 Ah, biased low by surface
                           # charge at the start, against ~2-3 Ah for healthy
                           # 18650s -- so treat a low learned value as a real
                           # statement about the pack, not a bug.
REST_CURRENT_A   = 0.05    # below this the pack is at rest and voltage is usable
ANCHOR_GAIN      = 0.02    # per sample, so a voltage correction takes ~2 min
FULL_CELL_V      = 4.15    # at/above this while charging and tapering -> full
TAPER_CURRENT_A  = 0.08
CAP_LEARN_MIN_D  = 25.0    # points of anchored SoC change before trusting a
                           # capacity estimate
CAP_LEARN_GAIN   = 0.25
CONFIRM_CYCLES        = 5      # consecutive low-voltage readings before warning
SHUTDOWN_WARN_SECS    = 60     # countdown (seconds) from warning to poweroff


# ── INA219 driver (same calibration as INA219.py) ────────────────────────────
class INA219:
    _CAL_VALUE   = 26868
    _CURRENT_LSB = 0.1524    # mA per bit
    _POWER_LSB   = 0.003048  # W  per bit
    _CONFIG = (
        (0x00 << 13) |   # 16 V range
        (0x01 << 11) |   # Gain /2 (80 mV)
        (0x0D <<  7) |   # 12-bit 32-sample bus ADC
        (0x0D <<  3) |   # 12-bit 32-sample shunt ADC
        0x07             # continuous shunt + bus
    )

    def __init__(self, i2c_bus: int = 1, addr: int = I2C_ADDR):
        self.bus  = smbus.SMBus(i2c_bus)
        self.addr = addr
        self._write(_REG_CALIBRATION, self._CAL_VALUE)
        self._write(_REG_CONFIG,      self._CONFIG)

    def _read(self, reg: int) -> int:
        d = self.bus.read_i2c_block_data(self.addr, reg, 2)
        return (d[0] << 8) | d[1]

    def _write(self, reg: int, value: int) -> None:
        self.bus.write_i2c_block_data(
            self.addr, reg, [(value >> 8) & 0xFF, value & 0xFF]
        )

    def get_bus_voltage_v(self) -> float:
        self._write(_REG_CALIBRATION, self._CAL_VALUE)
        return (self._read(_REG_BUSVOLTAGE) >> 3) * 0.004

    def get_current_ma(self) -> float:
        raw = self._read(_REG_CURRENT)
        if raw > 32767:
            raw -= 65535
        return raw * self._CURRENT_LSB

    def get_power_w(self) -> float:
        self._write(_REG_CALIBRATION, self._CAL_VALUE)
        raw = self._read(_REG_POWER)
        if raw > 32767:
            raw -= 65535
        return raw * self._POWER_LSB


# ── Battery state of charge ───────────────────────────────────────────────────
# The pack's terminal voltage is not its state of charge. Under load it sags by
# I*R, so the old linear map read ~50 points lower at 2 A than at rest with the
# same charge in the pack -- unplug the mains and the number fell, plug it back
# and it rose. Two things fix that: undo the IR drop, then use a real Li-ion
# curve instead of a straight line.
#
# R measured 2026-09-01 by stepping CPU load and fitting V against I:
#   on battery, at ~1 A load     (677 mA span, residual sd 16.3 mV) -> 610 mOhm  <- used
#   on battery, lighter load     (561 mA span, residual sd 19.8 mV) -> 579 mOhm
#   on mains, load step          (210 mA span, residual sd  4.6 mV) -> 511 mOhm
#   on mains, float noise only   (396 mA span, noisy about zero)    -> 602 mOhm
# Fit R with a TIME TERM (V = a + b*t + R*I). Over anything longer than a few
# tens of seconds the pack is visibly draining, and a plain V-against-I fit
# charges that drift to the current: the same data gave 325 mOhm without the
# time term and 610 mOhm with it.
# Use the battery figure. Compensation only does anything while discharging, so
# it should be measured in that topology -- and with the charger out of the loop
# the span is nearly three times wider. The 68 mOhm gap against the mains figure
# is the charger's own regulation, which is not in circuit when it matters.
PACK_R_OHM = 0.610
CELLS      = 2

# Open-circuit volts per cell -> percent. Flat through the middle, which is what
# the linear map got wrong: at 3.70 V/cell it claimed 58%, the truth is ~17%.
_OCV_SOC = [
    (4.20, 100.0), (4.10, 90.0), (4.00, 80.0), (3.93, 70.0), (3.87, 60.0),
    (3.82, 50.0), (3.79, 40.0), (3.77, 30.0), (3.74, 20.0), (3.68, 15.0),
    (3.45, 10.0), (3.20, 5.0), (3.00, 0.0),
]

def open_circuit_v(v_terminal, current_a):
    """Undo the IR drop. current_a is positive when charging, as the INA219 reports.

    Bounded deliberately: this figure gates the shutdown, and every error mode
    of the compensation pushes it UP, which delays shutdown. An implausible
    current reading is ignored, and the correction is capped."""
    if abs(current_a) > SANE_CURRENT_A:
        return v_terminal
    corr = -PACK_R_OHM * current_a
    corr = max(-MAX_COMPENSATION_V, min(MAX_COMPENSATION_V, corr))
    return v_terminal + corr

def _load_state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {}

def _save_state(d):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(d, fh)
        os.replace(tmp, STATE_FILE)          # atomic, so a power cut cannot
    except Exception as exc:                 # leave a half-written state file
        logging.debug("state save failed: %s", exc)

def soc_percent(v_oc):
    per_cell = v_oc / CELLS
    if per_cell >= _OCV_SOC[0][0]:
        return 100.0
    if per_cell <= _OCV_SOC[-1][0]:
        return 0.0
    for (v_hi, s_hi), (v_lo, s_lo) in zip(_OCV_SOC, _OCV_SOC[1:]):
        if v_lo <= per_cell <= v_hi:
            return s_lo + (per_cell - v_lo) / (v_hi - v_lo) * (s_hi - s_lo)
    return 0.0


# ── Main monitoring loop ──────────────────────────────────────────────────────
def monitor():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT,  lambda *_: sys.exit(0))

    logging.info("UPS shutdown monitor starting (addr=0x%02X, low=%.2fV)", I2C_ADDR, LOW_VOLTAGE_V)

    try:
        ina = INA219()
    except Exception as exc:
        logging.critical("Cannot initialise INA219: %s", exc)
        sys.exit(1)

    low_count       = 0   # consecutive low-voltage readings
    warned          = False
    warn_start_time = 0.0

    recent  = []          # rolling current, for the charging hysteresis
    chg     = False
    last_charge_t = time.time()   # assume settling at start; it clears itself

    # ── coulomb counter ──────────────────────────────────────────────────────
    st          = _load_state()
    capacity_ah = float(st.get("capacity_ah", NOMINAL_CAP_AH))
    soc_ah      = st.get("soc_ah")          # None on a first run
    last_t      = time.time()
    last_save   = 0.0
    anchor_ref  = None                      # (voltage SoC %, coulombs since) for learning
    if soc_ah is None:
        logging.info("no saved charge state — seeding from voltage on the first reading")
    else:
        soc_ah = float(soc_ah)
        logging.info("resuming: %.3f Ah of %.3f Ah (%.1f%%)",
                     soc_ah, capacity_ah, 100 * soc_ah / capacity_ah)

    while True:
        try:
            v   = ina.get_bus_voltage_v()
            c   = ina.get_current_ma()
            pw  = ina.get_power_w()

            v_oc = open_circuit_v(v, c / 1000.0)

            # Hysteresis on a smoothed current, so a pack floating at a few mA
            # stops flipping CHG/DIS every other sample.
            recent.append(c)
            del recent[:-CHG_SMOOTH_N]
            c_avg = sum(recent) / len(recent)
            if chg and c_avg < CHG_OFF_MA:
                chg = False
            elif not chg and c_avg > CHG_ON_MA:
                chg = True
            state = "CHG" if chg else "DIS"

            # Mark the reading while surface charge is still dissipating.
            if chg:
                last_charge_t = time.time()
            settling = (time.time() - last_charge_t) < SETTLE_SECS

            # ── integrate charge ─────────────────────────────────────────────
            now = time.time()
            dt  = now - last_t
            last_t = now
            v_pct = soc_percent(v_oc)             # the voltage opinion

            if soc_ah is None:                    # first ever reading
                soc_ah = v_pct / 100.0 * capacity_ah

            if 0 < dt < 60:                       # ignore a suspended clock
                soc_ah += (c / 1000.0) * dt / 3600.0
            soc_ah = max(0.0, min(capacity_ah, soc_ah))

            # Voltage anchors it, but only when voltage is worth listening to:
            # at rest, and far enough past charging that surface charge is gone.
            at_rest = abs(c) < REST_CURRENT_A * 1000
            anchored = False
            if at_rest and not settling:
                soc_ah += ANCHOR_GAIN * (v_pct / 100.0 * capacity_ah - soc_ah)
                anchored = True

            # A pack that is charging, near full and tapering IS full. This is
            # the one point on the curve where voltage is unambiguous.
            if chg and (v_oc / CELLS) >= FULL_CELL_V and abs(c) < TAPER_CURRENT_A * 1000:
                soc_ah = capacity_ah

            # Learn capacity between two anchored points: charge moved divided by
            # the fraction of the pack it moved through.
            if anchored:
                if anchor_ref is None:
                    anchor_ref = (v_pct, soc_ah)
                else:
                    d_pct = v_pct - anchor_ref[0]
                    d_ah  = soc_ah - anchor_ref[1]
                    if abs(d_pct) >= CAP_LEARN_MIN_D and d_ah * d_pct > 0:
                        est = abs(d_ah) / (abs(d_pct) / 100.0)
                        if 0.1 < est < 10.0:
                            capacity_ah += CAP_LEARN_GAIN * (est - capacity_ah)
                            logging.info("capacity estimate %.3f Ah over %.0f points "
                                         "-> now %.3f Ah", est, d_pct, capacity_ah)
                        anchor_ref = (v_pct, soc_ah)

            pct = 100.0 * soc_ah / capacity_ah
            if now - last_save > 30:
                _save_state({"soc_ah": soc_ah, "capacity_ah": capacity_ah, "ts": now})
                last_save = now

            flag = " ~settling" if settling else ""

            logging.info("%.3fV (oc %.3fV)  %+7.1fmA  %.3fW  %5.1f%% (v %4.1f%%)  [%s]%s",
                         v, v_oc, c, pw, pct, v_pct, state, flag)

        except Exception as exc:
            logging.error("Read error: %s", exc)
            time.sleep(POLL_INTERVAL_S)
            continue

        # ── Backstop: raw terminal volts, no compensation, no exceptions ──
        if v < RAW_FLOOR_V:
            logging.warning("RAW floor %.3fV < %.3fV — powering off NOW "
                            "(compensation bypassed).", v, RAW_FLOOR_V)
            os.system("sudo poweroff")
            sys.exit(0)

        # ── Hard shutdown at absolute minimum ─────────────────────────────
        if v_oc < SHUTDOWN_VOLTAGE_V and not chg:
            logging.warning("Hard shutdown: open-circuit %.3fV < %.3fV (terminal %.3fV) — powering off NOW.", v_oc, SHUTDOWN_VOLTAGE_V, v)
            os.system("sudo poweroff")
            sys.exit(0)

        # ── Soft warning + countdown ──────────────────────────────────────
        if v_oc < LOW_VOLTAGE_V and not chg:
            low_count += 1
            if low_count >= CONFIRM_CYCLES and not warned:
                warned = True
                warn_start_time = time.time()
                logging.warning(
                    "Open-circuit voltage low (%.3fV, terminal %.3fV). Shutdown in %ds "
                    "unless charger connected.", v_oc, v, SHUTDOWN_WARN_SECS,
                )
        else:
            if warned:
                logging.info("Voltage recovered / charger connected — shutdown cancelled.")
            low_count = 0
            warned    = False

        if warned:
            elapsed   = time.time() - warn_start_time
            remaining = SHUTDOWN_WARN_SECS - elapsed
            if remaining <= 0:
                logging.warning("Countdown expired — initiating safe shutdown.")
                os.system("sudo poweroff")
                sys.exit(0)
            else:
                logging.info("  → shutdown in %.0f s (open-circuit=%.3fV, terminal=%.3fV)", remaining, v_oc, v)

        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    monitor()
