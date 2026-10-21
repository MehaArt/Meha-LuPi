"""
gpio.py — порт src/lua/core/gpio.lua

GPIO через sysfs (/sys/class/gpio/...) — Linux/Raspberry Pi.
На платформах без GPIO регистрация просто пропускается.

Если установлена библиотека RPi.GPIO — используется она как альтернативный бэкенд.
"""
from __future__ import annotations
import os
import logging
from core import component as component_module

logger = logging.getLogger("lupi.gpio")

_GPIO_ROOT = "/sys/class/gpio"


def _read_file(path: str) -> str | None:
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except OSError:
        return None


def _write_file(path: str, value: str) -> bool:
    try:
        with open(path, "w") as f:
            f.write(value)
        return True
    except OSError:
        return False


# ── Sysfs backend ─────────────────────────────────────────────────────────────

class SysfsGPIO:
    def _pin_path(self, pin: int) -> str:
        return os.path.join(_GPIO_ROOT, f"gpio{pin}")

    def _export(self, pin: int):
        if not os.path.exists(self._pin_path(pin)):
            _write_file(os.path.join(_GPIO_ROOT, "export"), str(pin))

    def pin_mode(self, pin: int, mode: str):
        """mode: 'in' | 'out'"""
        if not isinstance(pin, int):
            return False, "pin must be int"
        if mode not in ("in", "out"):
            return False, "Invalid mode string"
        self._export(pin)
        ok = _write_file(os.path.join(self._pin_path(pin), "direction"), mode)
        if not ok:
            return False, "Couldn't set pin mode"
        return mode

    def write(self, pin: int, value) -> bool | tuple:
        self._export(pin)
        if isinstance(value, bool):
            v = "1" if value else "0"
        elif isinstance(value, int):
            v = "1" if (value & 1) else "0"
        else:
            v = "0"
        if not _write_file(os.path.join(self._pin_path(pin), "value"), v):
            return False, "Couldn't set pin value"
        return True

    def read(self, pin: int) -> str | tuple:
        if not os.path.exists(self._pin_path(pin)):
            return False, "Set pin mode first"
        result = _read_file(os.path.join(self._pin_path(pin), "value"))
        return result

    def toggle_pin(self, pin: int):
        current = self.read(pin)
        if isinstance(current, tuple):
            return current
        self.write(pin, "0" if current == "1" else "1")

    def as_proxy(self) -> dict:
        return {
            "pinMode":   self.pin_mode,
            "write":     self.write,
            "read":      self.read,
            "togglePin": self.toggle_pin,
        }


# ── RPi.GPIO backend (опциональный) ─────────────────────────────────────────

class RPiGPIO:
    def __init__(self):
        import RPi.GPIO as GPIO
        self._gpio = GPIO
        GPIO.setmode(GPIO.BCM)

    def pin_mode(self, pin: int, mode: str):
        m = self._gpio.IN if mode == "in" else self._gpio.OUT
        self._gpio.setup(pin, m)
        return mode

    def write(self, pin: int, value):
        self._gpio.output(pin, bool(value))
        return True

    def read(self, pin: int):
        return str(int(self._gpio.input(pin)))

    def toggle_pin(self, pin: int):
        current = self._gpio.input(pin)
        self._gpio.output(pin, not current)

    def as_proxy(self) -> dict:
        return {
            "pinMode":   self.pin_mode,
            "write":     self.write,
            "read":      self.read,
            "togglePin": self.toggle_pin,
        }


# ── Регистрация ───────────────────────────────────────────────────────────────

def register(uuid: str | None = None) -> str | None:
    """Зарегистрировать GPIO компонент. Возвращает адрес или None."""
    # Попробовать RPi.GPIO
    try:
        gpio = RPiGPIO()
        addr = component_module.register(uuid, "gpio", gpio.as_proxy())
        logger.info("GPIO registered via RPi.GPIO: %s", addr)
        return addr
    except ImportError:
        pass

    # Попробовать sysfs
    if os.path.exists(_GPIO_ROOT):
        gpio = SysfsGPIO()
        addr = component_module.register(uuid, "gpio", gpio.as_proxy())
        logger.info("GPIO registered via sysfs: %s", addr)
        return addr

    logger.info("GPIO not available on this platform — skipping")
    return None
