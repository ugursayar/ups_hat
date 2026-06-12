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
def _make_battery_pixmap(percent: float, charging: bool, size: int = 22) -> QPixmap:
    """Draw a colour-coded battery icon with Qt. No external image files needed."""
    img = QImage(size, size, QImage.Format_RGBA8888)
    img.fill(Qt.transparent)

    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)

    # Choose fill colour based on charge level or charging state
    if charging:
        fill_col = QColor("#2196F3")   # blue  – charging
    elif percent > 50:
        fill_col = QColor("#4CAF50")   # green
    elif percent > 20:
        fill_col = QColor("#FF9800")   # amber
    else:
        fill_col = QColor("#F44336")   # red   – critical

    outline_col = QColor("#CCCCCC")

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

    # Charging bolt overlay
    if charging:
        font = QFont()
        font.setPixelSize(max(8, int(size * 0.52)))
        p.setFont(font)
        p.setPen(QColor(255, 255, 255, 210))
        p.drawText(bx, by, bw, bh, Qt.AlignCenter, "⚡")

    p.end()
    return QPixmap.fromImage(img)


def make_battery_icon(percent: float, charging: bool, size: int = 22) -> QIcon:
    return QIcon(_make_battery_pixmap(percent, charging, size))


# ── Background worker thread ──────────────────────────────────────────────────
class Worker(QObject):
    """Polls the INA219 in a background thread and emits readings."""

    # voltage_v, current_a, power_w, percent (0–100), charging
    updated = pyqtSignal(float, float, float, float, bool)

    def run(self):
        try:
            ina = INA219()
        except Exception as e:
            logging.error(f"Failed to initialise INA219: {e}")
            return

        while True:
            try:
                v   = ina.get_bus_voltage_v()
                c   = ina.get_current_ma()    # mA
                pw  = ina.get_power_w()
                pct = (v - BATT_EMPTY_V) / (BATT_FULL_V - BATT_EMPTY_V) * 100.0
                pct = max(0.0, min(100.0, pct))
                chg = c > CHARGING_THRESHOLD_MA
                self.updated.emit(v, c / 1000.0, pw, pct, chg)
            except Exception as e:
                logging.error(f"INA219 read error: {e}")
            time.sleep(POLL_INTERVAL_S)


# ── Main window / tray ────────────────────────────────────────────────────────
class BatteryTray(QMessageBox):
    """System-tray battery monitor with low-voltage warning and safe shutdown."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("UPS Battery Status")
        self._charging  = False
        self._percent   = 0.0
        self._voltage   = 0.0
        self._msgBox    = None
        self._counter   = 0

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

    # ── Countdown tick ────────────────────────────────────────────────────────
    def _on_tick(self):
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

    def _dismiss_warning(self):
        self._timer.stop()
        if self._msgBox:
            self._msgBox.hide()
            self._msgBox.close()
            self._msgBox = None
        logging.info("Charger detected — shutdown cancelled.")

    # ── Data refresh (called from worker signal) ──────────────────────────────
    def _refresh(self, voltage: float, current_a: float, power_w: float,
                 percent: float, charging: bool):
        self._charging = charging
        self._percent  = percent
        self._voltage  = voltage

        icon = make_battery_icon(int(percent), charging)
        self.tray.setIcon(icon)
        self.setIconPixmap(icon.pixmap(48, 48))

        state = "Charging ⚡" if charging else "Discharging"
        tooltip = f"{percent:.0f}%  {voltage:.3f} V  {current_a:+.3f} A  [{state}]"
        self.tray.setToolTip(tooltip)
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
