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
CHARGING_THRESHOLD_MA = 50     # mA — above this = charging
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

    BATT_EMPTY_V = 6.0
    BATT_FULL_V  = 8.4

    while True:
        try:
            v   = ina.get_bus_voltage_v()
            c   = ina.get_current_ma()
            pw  = ina.get_power_w()
            pct = max(0.0, min(100.0, (v - BATT_EMPTY_V) / (BATT_FULL_V - BATT_EMPTY_V) * 100))
            chg = c > CHARGING_THRESHOLD_MA
            state = "CHG" if chg else "DIS"
            logging.info("%.3fV  %+7.1fmA  %.3fW  %5.1f%%  [%s]", v, c, pw, pct, state)

        except Exception as exc:
            logging.error("Read error: %s", exc)
            time.sleep(POLL_INTERVAL_S)
            continue

        # ── Hard shutdown at absolute minimum ─────────────────────────────
        if v < SHUTDOWN_VOLTAGE_V and not chg:
            logging.warning("Hard shutdown voltage %.3fV < %.3fV — powering off NOW.", v, SHUTDOWN_VOLTAGE_V)
            os.system("sudo poweroff")
            sys.exit(0)

        # ── Soft warning + countdown ──────────────────────────────────────
        if v < LOW_VOLTAGE_V and not chg:
            low_count += 1
            if low_count >= CONFIRM_CYCLES and not warned:
                warned = True
                warn_start_time = time.time()
                logging.warning(
                    "Voltage low (%.3fV). Shutdown in %ds unless charger connected.",
                    v, SHUTDOWN_WARN_SECS,
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
                logging.info("  → shutdown in %.0f s (voltage=%.3fV)", remaining, v)

        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    monitor()
