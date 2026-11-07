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
RAW_FLOOR_V           = 6.10   # raw terminal volts; below this, go down regardless
# Was 5.60, which sat BELOW where this box actually dies: on 2026-09-21 the Pi
# lost power at 6.00 V terminal / 1.7 A with neither guard firing -- the capped
# compensation held v_oc at 6.80 V, well above SHUTDOWN_VOLTAGE_V. Every sample
# below 6.10 V since 2026-09-01 came from a near-empty pack (<= 9.5% by voltage),
# so this costs no runtime; on 09-21 it would have fired 16 s before the cut.
SANE_CURRENT_A        = 5.0    # |I| beyond this is a bad read -- do not compensate
CHARGING_THRESHOLD_MA = 50     # mA — above this = charging (legacy, see hysteresis below)
# The raw flag flipped CHG/DIS on noise: float current measures +4.3 mA mean
# over -276..+120 mA, so it read 'charging' in 2% of samples while on mains.
# Separate on/off thresholds on a smoothed current instead.
CHG_ON_MA             = 80     # smoothed current above this -> charging
CHG_OFF_MA            = 20     # and below this -> not charging
CHG_SMOOTH_N          = 5      # samples in the moving average
# A third state, for the displays: neither charging nor draining = on AC.
# 'DIS' only ever meant "not charging", and the tray printed it as
# "Discharging" while the pack sat on mains topping off at +30..+60 mA.
# Over 2026-11-05..06 the 10 s average sat above -81 mA 99% of the time on
# mains and never above -401 mA on battery. A pack that genuinely drains on
# mains (supply at its limit, see README) still reads 'discharging' -- true.
DRAIN_ON_MA           = -250   # smoothed current below this -> discharging
DRAIN_OFF_MA          = -150   # and above this -> not
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
# Published every sample for the tray and Fleet Command, so they show THIS
# percentage instead of each re-deriving one from raw terminal volts (which
# jumped 24% -> 84% in 4 s when the charger lifted the bus on 2026-11-05).
# tmpfs, so a write every 2 s costs the SSD nothing.
STATUS_FILE      = "/run/ups_hat/status.json"
NOMINAL_CAP_AH   = 2.80    # seed only. Rated capacity of the cells fitted
                           # 2026-11-06: 2x Aspilsan INR18650A28 (2800 mAh, 25 A)
                           # in series, so 2.8 Ah for the pack. The previous
                           # PROFUSE 2600 mAh pair had worn to ~0.13 Ah usable;
                           # 0.80 was the seed measured on that worn pack.
REST_CURRENT_A   = 0.05    # below this the pack is at rest and voltage is usable
ANCHOR_GAIN      = 0.02    # per sample, so a voltage correction takes ~2 min
FULL_CELL_V      = 4.12    # V_oc/cell at/above this while charging and tapering -> full.
                           # Was 4.15, which this charger never reaches under taper:
                           # on 2026-11-07 it finished at 8.264 V / +41 mA (V_oc 4.13
                           # per cell), so the counter sat at 85.7% until the anchor.
TAPER_CURRENT_A  = 0.08
CAP_LEARN_GAIN   = 0.25
# ── Capacity learning (rewritten 2026-11-06) ──────────────────────────────────
# The old learner needed two AT-REST readings >= 25 points apart. On this box
# the pack is only ever at rest on mains, at the charger's float (~92%), so in
# two months it never fired: capacity sat at its 0.80 Ah seed while the real
# pack wore from ~0.7 Ah to ~0.13 Ah. It also measured charge with soc_ah,
# which the voltage anchor itself moves and which clamps at 0 and full.
#
# Now: REFERENCE = the last anchored reading (at rest, settled -- here, the pack
# sitting at float for 20+ min, a genuine equilibrium). END POINT = a reading
# late in the following discharge, IR-compensated and averaged over a minute,
# once V_oc is below LEARN_END_CELL_V. Down there the curve runs ~22 %/V, so the
# polarisation the IR correction leaves behind -- which always reads LOW on
# discharge -- costs ~2 points even at 100 mV/cell. Higher up it does not:
# replaying the old pack's 09-04 and 09-09 discharges with the end point at the
# 3.74 V/cell boundary (20%) gave 0.63 / 0.56 Ah against ~0.86-0.90 Ah delivered
# between trusted points, because ~95 mV of polarisation had the pack reading
# 15% with a third of its charge still in it. So a discharge has to get near
# shutdown before it teaches anything -- which is when capacity matters.
# Charge is a separate, unclamped coulomb total. One estimate per discharge.
LEARN_END_CELL_V = 3.55    # V_oc per cell (~12%) at or below which to measure
LEARN_MIN_D      = 40.0    # SoC points between reference and end point
LEARN_SMOOTH_N   = 30      # samples (60 s) of V_oc averaged for the end point
LEARN_MIN_DIS_A  = 0.15    # end point must be a real discharge, not float noise
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
#
# NEW CELLS 2026-11-06 (Aspilsan INR18650A28): 140 mOhm, from the two mains
# steps of a plug test -- unplug 8.264 V/-0.028 A -> 8.168 V/-0.729 A = 137-139,
# re-plug 8.128 V/-0.753 A -> 8.252 V/+0.156 A = 136-143. Two-second steps, so
# no time term is needed. With the old 0.610 left in, V_oc read 8.60 V under a
# 0.73 A load (100% by voltage) and every shutdown threshold fired ~0.35 V late.
# The worn pack's 610 above was itself a symptom of its failing cell.
PACK_R_OHM = 0.140
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

def _write_status(d):
    try:
        os.makedirs(os.path.dirname(STATUS_FILE), mode=0o755, exist_ok=True)
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(d, fh)
        os.chmod(tmp, 0o644)                 # readers run as ugur
        os.replace(tmp, STATUS_FILE)         # readers never see half a file
    except Exception as exc:
        logging.debug("status write failed: %s", exc)

# The HAT's charger stops short of 4.20 V/cell. A full pack rests at 8.28-8.30 V
# (4.14-4.15 V/cell), which the cell curve above calls ~94%, so "full" showed
# 94.4% forever (2026-11-07). The published % is therefore a share of what THIS
# charger fills: the chemistry curve divided by its value at CHARGER_FULL_CELL_V
# and capped at 100. A uniform scale keeps the learner consistent (it measures
# capacity in the same points it then divides by). Shutdown never reads a percent.
CHARGER_FULL_CELL_V = 4.14

def _cell_soc(per_cell):
    """Chemistry state of charge, 0-100 against a 4.20 V/cell full."""
    if per_cell >= _OCV_SOC[0][0]:
        return 100.0
    if per_cell <= _OCV_SOC[-1][0]:
        return 0.0
    for (v_hi, s_hi), (v_lo, s_lo) in zip(_OCV_SOC, _OCV_SOC[1:]):
        if v_lo <= per_cell <= v_hi:
            return s_lo + (per_cell - v_lo) / (v_hi - v_lo) * (s_hi - s_lo)
    return 0.0

FULL_FRACTION = _cell_soc(CHARGER_FULL_CELL_V) / 100.0   # 0.94

def soc_percent(v_oc):
    """Percent of what this charger fills, 0-100."""
    return min(100.0, _cell_soc(v_oc / CELLS) / FULL_FRACTION)

class CapacityLearner:
    """Capacity from (charge moved) / (fraction of the pack it moved through),
    between a rest reading and a late-discharge reading where voltage is precise.
    Pure logic, no I/O -- see the comment block at LEARN_END_CELL_V."""

    def __init__(self):
        self.coulombs = 0.0   # Ah, signed (+ = charging), never clamped or anchored
        self.ref = None       # (v_pct, coulombs) at the last anchored reading
        self.win = []         # recent V_oc during the current discharge

    def integrate(self, c_ma, dt):
        self.coulombs += (c_ma / 1000.0) * dt / 3600.0

    def update(self, v_oc, c_ma, v_pct, anchored, chg):
        """Feed one sample. Returns (est_ah, d_pct, d_ah, end_pct) or None."""
        if anchored:
            self.ref = (v_pct, self.coulombs)
            self.win.clear()
            return None
        if chg:                      # charge between the two points would add
            self.ref = None          # surface charge to the end reading
            self.win.clear()
            return None
        if self.ref is None or c_ma > -LEARN_MIN_DIS_A * 1000:
            self.win.clear()
            return None
        self.win.append(v_oc)
        del self.win[:-LEARN_SMOOTH_N]
        if len(self.win) < LEARN_SMOOTH_N:
            return None
        v_avg = sum(self.win) / len(self.win)
        if v_avg / CELLS > LEARN_END_CELL_V:
            return None
        end_pct = soc_percent(v_avg)
        d_pct = self.ref[0] - end_pct
        d_ah = self.ref[1] - self.coulombs
        if d_pct < LEARN_MIN_D or d_ah <= 0:
            return None
        self.ref = None              # one estimate per discharge
        self.win.clear()
        est = d_ah / (d_pct / 100.0)
        if not 0.1 < est < 10.0:
            return None
        return est, d_pct, d_ah, end_pct


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
    draining = False
    last_charge_t = time.time()   # assume settling at start; it clears itself

    # ── coulomb counter ──────────────────────────────────────────────────────
    st          = _load_state()
    capacity_ah = float(st.get("capacity_ah", NOMINAL_CAP_AH * FULL_FRACTION))
    # Capacity is stored on the scale it was learned on (1.0 = the 4.20 V/cell
    # chemistry full, before 2026-11-07). Convert it once to the charger-full scale.
    old_frac = float(st.get("full_fraction", 1.0))
    if "capacity_ah" in st and abs(old_frac - FULL_FRACTION) > 1e-6:
        new_cap = capacity_ah * FULL_FRACTION / old_frac
        logging.info("capacity rescaled to charger-full: %.3f Ah -> %.3f Ah",
                     capacity_ah, new_cap)
        capacity_ah = new_cap
    soc_ah      = st.get("soc_ah")          # None on a first run
    last_t      = time.time()
    last_save   = 0.0
    learner     = CapacityLearner()
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
            if draining and c_avg > DRAIN_OFF_MA:
                draining = False
            elif not draining and c_avg < DRAIN_ON_MA:
                draining = True
            power = "charging" if chg else ("discharging" if draining else "ac")

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
                learner.integrate(c, dt)
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

            # Learn capacity: rest reading -> precise point of the next discharge.
            learned = learner.update(v_oc, c, v_pct, anchored, chg)
            if learned:
                est, d_pct, d_ah, end_pct = learned
                old_cap = capacity_ah
                capacity_ah += CAP_LEARN_GAIN * (est - capacity_ah)
                # The end point is a trusted reading too: re-set the counter on it,
                # so a capacity that was wrong does not leave the % wrong as well.
                soc_ah = end_pct / 100.0 * capacity_ah
                logging.info("capacity estimate %.3f Ah (%.3f Ah over %.0f points, "
                             "ending at %.1f%%) -> %.3f Ah, was %.3f",
                             est, d_ah, d_pct, end_pct, capacity_ah, old_cap)
                last_save = 0.0                   # persist now, before any shutdown

            pct = 100.0 * soc_ah / capacity_ah
            if now - last_save > 30:
                _save_state({"soc_ah": soc_ah, "capacity_ah": capacity_ah,
                             "full_fraction": FULL_FRACTION, "ts": now})
                last_save = now

            # Published after the shutdown checks below, so it carries this
            # sample's warning state; the tray shows that rather than deciding
            # for itself (since 2026-11-06).
            status = {
                "ts": now, "pct": round(pct, 1), "v_pct": round(v_pct, 1),
                "v": round(v, 3), "v_oc": round(v_oc, 3),
                "current_ma": round(c, 1),        # positive = charging
                "power_w": round(pw, 3), "state": state,
                "power": power,                   # charging | discharging | ac
                "settling": settling, "capacity_ah": round(capacity_ah, 3),
                "warn": False, "shutdown_in": None,
            }

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
            _write_status(dict(status, warn=True, shutdown_in=0))
            os.system("sudo poweroff")
            sys.exit(0)

        # ── Hard shutdown at absolute minimum ─────────────────────────────
        if v_oc < SHUTDOWN_VOLTAGE_V and not chg:
            logging.warning("Hard shutdown: open-circuit %.3fV < %.3fV (terminal %.3fV) — powering off NOW.", v_oc, SHUTDOWN_VOLTAGE_V, v)
            _write_status(dict(status, warn=True, shutdown_in=0))
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
                _write_status(dict(status, warn=True, shutdown_in=0))
                os.system("sudo poweroff")
                sys.exit(0)
            else:
                logging.info("  → shutdown in %.0f s (open-circuit=%.3fV, terminal=%.3fV)", remaining, v_oc, v)
                status.update(warn=True, shutdown_in=int(remaining))

        _write_status(status)
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    monitor()
