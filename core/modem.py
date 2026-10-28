"""
core/modem.py — сетевой модем (внутрипроцессный message board).

Все модемы в текущем процессе эмулятора видят друг друга.
Межпроцессная связь (несколько инстансов эмулятора) не поддерживается.
"""
from __future__ import annotations
import logging
import threading

from core import component as component_mod
from core import computer as computer_mod

logger = logging.getLogger("lupi.modem")

MAX_PACKET_SIZE_DEFAULT = 8192
MAX_WIRELESS_RANGE = 400

# Глобальный реестр модемов: address -> ModemComponent
_registry: dict[str, "ModemComponent"] = {}
_registry_lock = threading.Lock()


def _find_target(address: str):
    with _registry_lock:
        return _registry.get(address)


class ModemComponent:
    def __init__(self, address: str, wireless: bool = False,
                 max_packet_size: int = MAX_PACKET_SIZE_DEFAULT):
        self.address = address
        self.wireless = bool(wireless)
        self.max_packet_size = int(max_packet_size)
        self._open_ports: set[int] = set()
        self._strength = MAX_WIRELESS_RANGE
        self._wake_message = None

    # ── helpers ────────────────────────────────────────────────────────
    def _check_packet_size(self, data):
        """OC-совместимая проверка: превышение maxPacketSize → ошибка."""
        total = 0
        for d in data:
            if isinstance(d, str):
                total += len(d)
            elif isinstance(d, (bytes, bytearray)):
                total += len(d)
            else:
                total += len(str(d))
        if total > self.max_packet_size:
            raise ValueError("packet too big")

    # ── API ────────────────────────────────────────────────────────────
    def open(self, port, *_):
        p = int(port)
        if p < 1 or p > 65535:
            raise ValueError("invalid port number")
        if p in self._open_ports:
            return False, "port already open"
        self._open_ports.add(p)
        return True

    def close(self, port=None, *_):
        if port is None:
            if not self._open_ports:
                return False, "no open ports"
            self._open_ports.clear()
            return True
        p = int(port)
        if p not in self._open_ports:
            return False, "port not open"
        self._open_ports.discard(p)
        return True

    def is_open(self, port, *_):
        return int(port) in self._open_ports

    def is_wireless(self, *_):
        return self.wireless

    def get_strength(self, *_):
        if not self.wireless:
            return None
        return self._strength

    def set_strength(self, value, *_):
        if not self.wireless:
            return None
        self._strength = int(value)
        return self._strength

    def get_wake_message(self, *_):
        return self._wake_message

    def set_wake_message(self, msg, *_):
        self._wake_message = msg
        return msg

    def max_packet_size_getter(self, *_):
        return self.max_packet_size

    def send(self, target, port, *data):
        tgt = str(target)
        p = int(port)
        if p < 1 or p > 65535:
            raise ValueError("invalid port number")
        self._check_packet_size(data)

        target_comp = _find_target(tgt)
        if target_comp is None:
            return False
        if p not in target_comp._open_ports:
            return False

        distance = 0 if (self.wireless and target_comp.wireless) else 1
        # Сигнал: modem_message, target_addr, sender_addr, port, distance, ...
        computer_mod.push_signal(
            "modem_message", tgt, self.address, p, distance, *data
        )
        return True

    def broadcast(self, port, *data):
        p = int(port)
        if p < 1 or p > 65535:
            raise ValueError("invalid port number")
        self._check_packet_size(data)
        with _registry_lock:
            targets = list(_registry.keys())
        for addr in targets:
            if addr == self.address:
                continue
            try:
                self.send(addr, p, *data)
            except Exception:
                logger.exception("modem.broadcast(%s) failed", addr)
        return True

    def as_proxy(self) -> dict:
        p = {
            "open":           self.open,
            "close":          self.close,
            "isOpen":         self.is_open,
            "isWireless":     self.is_wireless,
            "send":           self.send,
            "broadcast":      self.broadcast,
            "getWakeMessage": self.get_wake_message,
            "setWakeMessage": self.set_wake_message,
            "maxPacketSize":  self.max_packet_size_getter,
        }
        if self.wireless:
            p["getStrength"] = self.get_strength
            p["setStrength"] = self.set_strength
        return p


def register(uuid: str | None = None, wireless: bool = False,
             max_packet_size: int = MAX_PACKET_SIZE_DEFAULT) -> str:
    if uuid is None:
        from util import random as random_mod
        uuid = random_mod.uuid()

    m = ModemComponent(uuid, wireless=wireless,
                       max_packet_size=max_packet_size)
    component_mod.register(uuid, "modem", m.as_proxy())
    with _registry_lock:
        _registry[uuid] = m
    logger.info("Modem registered: %s (wireless=%s)", uuid, wireless)
    return uuid


def unregister(uuid: str) -> bool:
    with _registry_lock:
        m = _registry.pop(uuid, None)
    if m is None:
        return False
    return bool(component_mod.unregister(uuid))