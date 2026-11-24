#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
batteryTray.py — Desktop Battery Tray Monitor for Waveshare UPS HAT (INA219)
Adapted from the UPS_HAT_E batteryTray.py for the INA219-based UPS HAT.

Requires:
    pip3 install smbus PyQt5

Usage:
    python3 batteryTray.py
"""

import json
import os
import sys
import time
import logging
import signal

import smbus
from PyQt5.QtGui import (
    QIcon, QPixmap, QPainter, QColor, QPen, QBrush, QFont, QImage
)
from PyQt5.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu, QAction, QMessageBox
)
from PyQt5.QtCore import QObject, QThread, pyqtSignal, QTimer, Qt

signal.signal(signal.SIGINT, signal.SIG_DFL)

# ── Logging: writes to ~/ups_hat/battery.log and stdout ──────────────────────
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "battery.log")
logging.basicConfig(
    format="%(asctime)s  %(message)s",
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

# ── UPS configuration ─────────────────────────────────────────────────────────
I2C_ADDR          = 0x42    # INA219 I²C address (check yours with: i2cdetect -y 1)
POLL_INTERVAL_S   = 2       # seconds between readings

# 2S Li-ion pack: fully charged = 8.4 V, nominal cut-off = 6.0 V
BATT_FULL_V       = 8.4
BATT_EMPTY_V      = 6.0

# Warn & start countdown when voltage drops below LOW_VOLTAGE (not charging)
LOW_VOLTAGE_V     = 6.4     # ≈ 3.20 V/cell
SHUTDOWN_SECS     = 60      # seconds of countdown before poweroff

# Current threshold to distinguish charging from idle/discharging (mA)
CHARGING_THRESHOLD_MA = 50

# ups_shutdown.py publishes its percentage here every 2 s. Used when fresh; the
# raw-voltage line above is only a fallback for when the service is down.
STATUS_FILE       = "/run/ups_hat/status.json"
STATUS_MAX_AGE_S  = 10


def read_service_status():
    """The service's latest reading, or None if missing or stale."""
    try:
        with open(STATUS_FILE) as fh:
            st = json.load(fh)
        if abs(time.time() - float(st["ts"])) <= STATUS_MAX_AGE_S:
            return st
    except Exception:
        pass
    return None


# ups-watchdog (mediapi appliance, 2026-11-24) watches STATUS_FILE and nothing
# else -- it never touches I2C. When ups_shutdown.py stops rewriting it (a wedged
# I2C bus: the low-battery protection is blind) it says so here, and after
# 30 min blind it runs a clean `systemctl poweroff`. Written every 5 s:
# {"ts", "blind": bool, "blind_for": s, "poweroff_in": s|null, "reason", ...}
WATCHDOG_FILE      = "/run/ups-watchdog/state.json"
WATCHDOG_MAX_AGE_S = 60     # an older file says nothing (the watchdog is not running)


def read_watchdog_state(path=None):
    """The watchdog's state.json as a dict, or None. Plain file read, no I2C."""
    try:
        with open(path or WATCHDOG_FILE) as fh:
            st = json.load(fh)
        return st if isinstance(st, dict) else None
    except Exception:
        return None


def watchdog_alert(state, now: float):
    """The warning to show, or None. Pure: a function of the state and the clock.

    Warns only when the watchdog says blind AND its file is under
    WATCHDOG_MAX_AGE_S old -- a stale file is a stopped watchdog, not news."""
    if not isinstance(state, dict) or state.get("blind") is not True:
        return None
    try:
        if abs(now - float(state["ts"])) >= WATCHDOG_MAX_AGE_S:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    left = state.get("poweroff_in")
    if isinstance(left, (int, float)) and not isinstance(left, bool):
        when = "now" if left <= 0 else f"in {max(1, round(left / 60))} min"
    else:
        when = "pending"
    return ("UPS monitor not reporting — the battery is unguarded — "
            f"clean shutdown {when}")


# ── INA219 driver ─────────────────────────────────────────────────────────────
class INA219:
    """Minimal INA219 driver, calibrated for the UPS HAT (0.01 Ω shunt, 5 A range)."""

    # Calibration for 16 V / 5 A range (0.01 Ω shunt)
    _CAL_VALUE   = 26868
    _CURRENT_LSB = 0.1524   # mA per bit
    _POWER_LSB   = 0.003048 # W  per bit

    # Config word: 16 V range | Gain /2 (80 mV) | 12-bit 32-sample ADC | continuous
    _CONFIG = (
        (0x00 << 13) |  # BusVoltageRange: 16 V
        (0x01 << 11) |  # Gain: /2, 80 mV
        (0x0D <<  7) |  # Bus  ADC: 12-bit, 32 samples
        (0x0D <<  3) |  # Shunt ADC: 12-bit, 32 samples
        0x07            # Mode: shunt + bus, continuous
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
        """Load-side bus voltage in Volts."""
        self._write(_REG_CALIBRATION, self._CAL_VALUE)
        return (self._read(_REG_BUSVOLTAGE) >> 3) * 0.004

    def get_shunt_voltage_mv(self) -> float:
        """Shunt voltage in mV (V+ − V−)."""
        self._write(_REG_CALIBRATION, self._CAL_VALUE)
        raw = self._read(_REG_SHUNTVOLTAGE)
        if raw > 32767:
            raw -= 65535
        return raw * 0.01

    def get_current_ma(self) -> float:
        """Current in mA. Positive = charging, negative = discharging."""
        raw = self._read(_REG_CURRENT)
        if raw > 32767:
            raw -= 65535
        return raw * self._CURRENT_LSB

    def get_power_w(self) -> float:
        """Power in Watts."""
        self._write(_REG_CALIBRATION, self._CAL_VALUE)
        raw = self._read(_REG_POWER)
        if raw > 32767:
            raw -= 65535
        return raw * self._POWER_LSB


# ── Battery icon generator (no image files needed) ───────────────────────────
def _make_battery_pixmap(percent: float, charging: bool, size: int = 22,
                         alert: bool = False) -> QPixmap:
    """Draw a colour-coded battery icon with Qt. No external image files needed.

    alert: the UPS monitor is blind -- grey fill, red outline, a red '!'."""
    img = QImage(size, size, QImage.Format_RGBA8888)
    img.fill(Qt.transparent)

    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)

    # Choose fill colour based on charge level or charging state
    if alert:
        fill_col = QColor("#9E9E9E")   # grey  – the reading is not trustworthy
    elif charging:
        fill_col = QColor("#2196F3")   # blue  – charging
    elif percent > 50:
        fill_col = QColor("#4CAF50")   # green
    elif percent > 20:
        fill_col = QColor("#FF9800")   # amber
    else:
        fill_col = QColor("#F44336")   # red   – critical

    outline_col = QColor("#F44336") if alert else QColor("#CCCCCC")

    # Body dimensions
    bw = int(size * 0.72)
    bh = int(size * 0.52)
    bx = 1
    by = (size - bh) // 2
    # Terminal nub
    tw = max(2, int(size * 0.08))
    th = int(bh * 0.5)
    tx = bx + bw
    ty = by + (bh - th) // 2

    # Outline
    p.setPen(QPen(outline_col, 1.4))
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(bx, by, bw, bh, 2, 2)

    # Nub
    p.setPen(Qt.NoPen)
    p.setBrush(outline_col)
    p.drawRect(tx, ty, tw, th)

    # Fill bar
    margin = 2
    fw = max(0, int((bw - 2 * margin) * percent / 100))
    p.setBrush(fill_col)
    if fw > 0:
        p.drawRoundedRect(bx + margin, by + margin, fw, bh - 2 * margin, 1, 1)

    # Overlay: '!' when the monitor is blind, else the charging bolt
    if alert or charging:
        font = QFont()
        font.setPixelSize(max(8, int(size * 0.52)))
        font.setBold(alert)
        p.setFont(font)
        p.setPen(QColor("#D50000") if alert else QColor(255, 255, 255, 210))
        p.drawText(bx, by, bw, bh, Qt.AlignCenter, "!" if alert else "⚡")

    p.end()
    return QPixmap.fromImage(img)


def make_battery_icon(percent: float, charging: bool, size: int = 22,
                      alert: bool = False) -> QIcon:
    return QIcon(_make_battery_pixmap(percent, charging, size, alert))


# ── Background worker thread ──────────────────────────────────────────────────
class Worker(QObject):
    """Emits readings every POLL_INTERVAL_S from a background thread.

    While ups_shutdown.py's status file is fresh it carries everything the tray
    shows (v, current_ma, power_w, pct, state), so the tray leaves the I2C bus
    alone -- before 2026-11-24 it was a second master on it, rewriting the
    INA219's calibration every 2 s (devices/mediapi/CLAUDE.md, "I2C bus --
    handle with care"). The INA219 is opened and read only while that file is
    stale, i.e. the service is down and the tray's own fallback must guard."""

    # voltage_v, current_a, power_w, percent (0–100), charging,
    # service status (dict) or None when ups_shutdown.py's file is stale
    updated = pyqtSignal(float, float, float, float, bool, object)

    _SERVICE_KEYS = ("v", "current_ma", "power_w", "pct")

    def __init__(self):
        super().__init__()
        self._ina = None
        self._ina_failed = False   # as before: one failed init, no more bus traffic

    def sample(self):
        """One reading as (v, current_a, power_w, pct, chg, st), or None."""
        st = read_service_status()
        if st is not None and all(k in st for k in self._SERVICE_KEYS):
            # The service's percentage: load-compensated and coulomb-counted.
            # Raw terminal volts jump ~60 points the moment the charger lifts
            # the bus, with no charge moved.
            return (float(st["v"]), float(st["current_ma"]) / 1000.0,
                    float(st["power_w"]), float(st["pct"]),
                    st.get("state") == "CHG", st)
        # Fallback: the service is not reporting -- read the INA219 ourselves.
        if self._ina is None:
            if self._ina_failed:
                return None
            try:
                self._ina = INA219()
            except Exception as e:
                self._ina_failed = True
                logging.error(f"Failed to initialise INA219: {e}")
                return None
        v   = self._ina.get_bus_voltage_v()
        c   = self._ina.get_current_ma()    # mA
        pw  = self._ina.get_power_w()
        pct = (v - BATT_EMPTY_V) / (BATT_FULL_V - BATT_EMPTY_V) * 100.0
        pct = max(0.0, min(100.0, pct))
        return (v, c / 1000.0, pw, pct, c > CHARGING_THRESHOLD_MA, None)

    def run(self):
        while True:
            try:
                r = self.sample()
                if r is not None:
                    self.updated.emit(*r)
            except Exception as e:
                logging.error(f"INA219 read error: {e}")
            time.sleep(POLL_INTERVAL_S)


# ── Main window / tray ────────────────────────────────────────────────────────
class BatteryTray(QMessageBox):
    """System-tray battery monitor with low-voltage warning and safe shutdown.

    Since 2026-11-06 it FOLLOWS ups_shutdown.py: while the service's status file
    is fresh, the tray shows the service's warning and countdown and never powers
    off itself. Its own raw-terminal-volts shutdown (< LOW_VOLTAGE_V, 60 s) runs
    only while that file is stale -- i.e. the service is down and nothing else is
    guarding the pack. On raw volts it fired 1-3% earlier than the service (and
    far earlier on the worn pack, whose sag was 4x larger)."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("UPS Battery Status")
        self._charging  = False
        self._percent   = 0.0
        self._voltage   = 0.0
        self._msgBox    = None
        self._counter   = 0
        self._following = False   # True while the service's status is fresh
        self._tooltip   = "UPS Monitor starting…"
        self._wd_alert  = None    # ups-watchdog's warning text while blind, else None
        self._wdBox     = None

        # ── Tray icon ──
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(make_battery_icon(0, False))
        self.tray.setToolTip("UPS Monitor starting…")

        status_act = QAction("Status", self)
        quit_act   = QAction("Exit",   self)
        status_act.triggered.connect(self.show)
        quit_act.triggered.connect(QApplication.instance().quit)
        menu = QMenu()
        menu.addAction(status_act)
        menu.addSeparator()
        menu.addAction(quit_act)
        self.tray.setContextMenu(menu)
        self.tray.show()

        # ── Background thread ──
        self._thread = QThread(self)
        self._worker = Worker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._thread.finished.connect(self._worker.deleteLater)
        self._worker.updated.connect(self._refresh)
        self._thread.start()

        # ── Countdown timer (1-second ticks) ──
        self._timer = QTimer(self, timeout=self._on_tick)

        # ── ups-watchdog check: on the GUI thread, on its own timer ──
        # Not in the worker: a wedged bus can raise there, or hang the worker in
        # an I2C read for good, and this warning is for exactly that case.
        self._wd_timer = QTimer(self, timeout=self._check_watchdog)
        self._wd_timer.start(POLL_INTERVAL_S * 1000)
        self._check_watchdog()

    # ── ups-watchdog warning (a file read only; never I2C) ────────────────────
    def _check_watchdog(self):
        try:
            alert = watchdog_alert(read_watchdog_state(), time.time())
        except Exception as e:               # never let this stop the timer
            logging.error(f"ups-watchdog state: {e}")
            return
        if alert == self._wd_alert:
            return
        was, self._wd_alert = self._wd_alert, alert
        if alert:
            if was is None:
                logging.warning("ups-watchdog: %s", alert)
                self.tray.showMessage("⚠  UPS monitor not reporting", alert,
                                      QSystemTrayIcon.Critical, 30000)
            if self._wdBox is None:
                self._wdBox = QMessageBox(
                    QMessageBox.Critical, "⚠  UPS Monitor Not Reporting", "")
                self._wdBox.setStandardButtons(QMessageBox.NoButton)  # sticky
                self._wdBox.setInformativeText(
                    "Check <tt>systemctl status ups-shutdown</tt>. A wedged I²C "
                    "bus needs a full power cycle (UPS HAT switch off).")
            self._wdBox.setText(f"<b>{alert}</b>")
            self._wdBox.show()                # non-modal: the watchdog does the poweroff
        else:
            logging.info("ups-watchdog: no longer reports blind (or its file is stale) "
                         "— warning cleared.")
            if self._wdBox is not None:
                self._wdBox.hide()
                self._wdBox.close()
                self._wdBox = None
        self._apply_icon()

    def _apply_icon(self):
        """Icon + tooltip from the last reading, tinted while the monitor is blind."""
        icon = make_battery_icon(int(self._percent), self._charging,
                                 alert=self._wd_alert is not None)
        self.tray.setIcon(icon)
        self.setIconPixmap(icon.pixmap(48, 48))
        tip = self._tooltip
        if self._wd_alert:
            tip = f"⚠ {self._wd_alert}\n{tip}"
        self.tray.setToolTip(tip)

    # ── Countdown tick ────────────────────────────────────────────────────────
    def _on_tick(self):
        if self._following:
            # The service is back and owns the decision; drop our own countdown.
            self._timer.stop()
            return
        if self._charging:
            # Charger connected — cancel shutdown
            self._dismiss_warning()
            return

        self._counter -= 1
        if self._counter <= 0:
            logging.warning("Battery critically low — initiating safe shutdown.")
            os.system("sudo poweroff")
        else:
            if self._msgBox:
                self._msgBox.setInformativeText(
                    f"Auto-shutdown in {self._counter} second(s)."
                )

    def _dismiss_warning(self, why="Charger detected — shutdown cancelled."):
        self._timer.stop()
        if self._msgBox:
            self._msgBox.hide()
            self._msgBox.close()
            self._msgBox = None
        logging.info(why)

    def _follow_service(self, svc):
        """Mirror the service's warning; the service does the poweroff."""
        if self._timer.isActive():
            self._timer.stop()            # our fallback countdown, superseded
        if svc.get("warn"):
            left = svc.get("shutdown_in")
            info = ("Shutting down now…" if left == 0 else
                    f"The UPS service will shut down in {left} second(s).")
            if self._msgBox is None:
                logging.warning("UPS service warns: battery low — %s", info)
                self._msgBox = QMessageBox(
                    QMessageBox.Warning,
                    "⚠  Battery Warning",
                    "<b>Battery is nearly empty!</b><br>"
                    "Please connect the power adapter.",
                )
                self._msgBox.setStandardButtons(QMessageBox.NoButton)
                self._msgBox.setInformativeText(info)
                self._msgBox.show()       # non-modal: the service owns the countdown
            else:
                self._msgBox.setInformativeText(info)
        elif self._msgBox is not None:
            self._dismiss_warning("UPS service cancelled the shutdown.")

    # ── Data refresh (called from worker signal) ──────────────────────────────
    def _refresh(self, voltage: float, current_a: float, power_w: float,
                 percent: float, charging: bool, svc=None):
        self._following = svc is not None
        self._charging = charging
        self._percent  = percent
        self._voltage  = voltage

        if svc is not None:
            # The service's three-way state; "not charging" is not "discharging".
            state = {"charging": "Charging ⚡", "discharging": "Discharging",
                     "ac": "On AC — full" if percent >= 95 else "On AC"}.get(
                         svc.get("power"), "Charging ⚡" if charging else "Discharging")
        else:
            state = "Charging ⚡" if charging else "Discharging"
        tooltip = f"{percent:.0f}%  {voltage:.3f} V  {current_a:+.3f} A  [{state}]"
        if svc is None:
            tooltip += "  (own reading — UPS service not running)"
        self._tooltip = tooltip
        self._apply_icon()
        logging.info(tooltip)

        self.setText(
            f"<pre>"
            f"Voltage :  {voltage:.3f} V\n"
            f"Current :  {current_a:+.3f} A\n"
            f"Power   :  {power_w:.3f} W\n"
            f"Percent :  {percent:.1f} %\n"
            f"State   :  {state}"
            f"</pre>"
        )

        # ── Low-voltage logic ──────────────────────────────────────────────
        if svc is not None:
            self._follow_service(svc)
            return
        # Fallback only: the service is down, so guard the pack ourselves.
        if voltage < LOW_VOLTAGE_V and not charging:
            if self._msgBox is None:
                self._counter = SHUTDOWN_SECS
                self._msgBox = QMessageBox(
                    QMessageBox.Warning,
                    "⚠  Battery Warning",
                    "<b>Battery voltage is critically low!</b><br>"
                    "Please connect the power adapter.",
                )
                self._msgBox.setInformativeText(
                    f"Auto-shutdown in {SHUTDOWN_SECS} second(s)."
                )
                self._msgBox.setStandardButtons(QMessageBox.NoButton)
                self._timer.start(1000)
                self._msgBox.exec()          # blocks until dismissed or shutdown
        elif charging and self._msgBox is not None:
            self._dismiss_warning()


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    tray = BatteryTray()
    sys.exit(app.exec_())
