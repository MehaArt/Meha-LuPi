"""
core/drive.py — компонент drive (флоппи/HDD), совместимый с OC/OCEmu.

Хранилище — папка <save_dir>/drives/<address>/:
  data.bin   — сырое содержимое диска (capacity байт, zero-padded)
  label.txt  — метка (до 16 символов)

API (OC-совместимое):
  getLabel/setLabel, getCapacity, getSectorSize, getPlatterCount,
  readSector/writeSector, readByte/writeByte.

tier:
  0 — флоппи, 512 KB, 1 платтер
  1 — HDD, 1 MB, 2 платтера
  2 — HDD, 2 MB, 4 платтера
  3 — HDD, 4 MB, 8 платтеров
"""
from __future__ import annotations
import os
import logging

from core import component as component_mod

logger = logging.getLogger("lupi.drive")

SECTOR_SIZE = 512
_TIER_CAPACITY = {0: 512 * 1024, 1: 1024 * 1024, 2: 2 * 1024 * 1024, 3: 4 * 1024 * 1024}
_TIER_PLATTERS = {0: 1, 1: 2, 2: 4, 3: 8}
DEFAULT_LABEL = ""

_default_save_dir: str | None = None


def set_default_save_dir(path: str) -> None:
    """Устанавливается из init.py (--root)."""
    global _default_save_dir
    _default_save_dir = os.path.abspath(path) if path else None


class DriveComponent:
    def __init__(self, address: str, storage_dir: str, tier: int = 0,
                 label: str = DEFAULT_LABEL):
        self.address = address
        try:
            self.tier = int(tier)
        except (TypeError, ValueError):
            self.tier = 0
        if self.tier not in _TIER_CAPACITY:
            self.tier = 0
        self.capacity = _TIER_CAPACITY[self.tier]
        self.platter_count = _TIER_PLATTERS[self.tier]
        self.sector_count = self.capacity // SECTOR_SIZE
        self.sectors_per_platter = max(1, self.sector_count // self.platter_count)

        self._dir = storage_dir
        try:
            os.makedirs(self._dir, exist_ok=True)
        except OSError as e:
            logger.error("Drive: cannot create %s: %s", self._dir, e)

        self._data_path = os.path.join(self._dir, "data.bin")
        self._meta_path = os.path.join(self._dir, "label.txt")

        self._label = (label or DEFAULT_LABEL)[:16]

        self._data = bytearray()
        try:
            if os.path.isfile(self._data_path):
                with open(self._data_path, "rb") as f:
                    self._data = bytearray(f.read(self.capacity))
        except OSError as e:
            logger.error("Drive: cannot read %s: %s", self._data_path, e)
        if len(self._data) < self.capacity:
            self._data.extend(b"\x00" * (self.capacity - len(self._data)))

        try:
            if os.path.isfile(self._meta_path):
                with open(self._meta_path, "r", encoding="utf-8") as f:
                    lbl = f.read().strip()
                    if lbl:
                        self._label = lbl[:16]
        except OSError:
            pass

    def _save(self) -> None:
        try:
            with open(self._data_path, "wb") as f:
                f.write(self._data)
        except OSError as e:
            logger.error("Drive: cannot write %s: %s", self._data_path, e)

    def _save_label(self) -> None:
        try:
            with open(self._meta_path, "w", encoding="utf-8") as f:
                f.write(self._label)
        except OSError as e:
            logger.error("Drive: cannot write label: %s", e)

    def _check_sector(self, sector) -> int:
        try:
            s = int(sector) - 1
        except (TypeError, ValueError):
            raise ValueError("invalid offset, not in a usable sector")
        if s < 0 or s >= self.sector_count:
            raise ValueError("invalid offset, not in a usable sector")
        return s

    def _check_offset(self, offset) -> int:
        try:
            o = int(offset) - 1
        except (TypeError, ValueError):
            raise ValueError("invalid offset, not in a usable sector")
        if o < 0 or o >= self.capacity:
            raise ValueError("invalid offset, not in a usable sector")
        return o

    # ── API ────────────────────────────────────────────────────────
    def get_label(self, *_):
        return self._label

    def set_label(self, value, *_):
        if not isinstance(value, str):
            raise TypeError(
                f"bad argument #1 (string expected, got {type(value).__name__})")
        v = value[:16]
        if v != self._label:
            self._label = v
            self._save_label()
        return self._label

    def get_capacity(self, *_):
        return self.capacity

    def get_sector_size(self, *_):
        return SECTOR_SIZE

    def get_platter_count(self, *_):
        return self.platter_count

    def read_sector(self, sector, *_):
        s = self._check_sector(sector)
        start = s * SECTOR_SIZE
        return bytes(self._data[start:start + SECTOR_SIZE]).decode("latin-1")

    def write_sector(self, sector, value, *_):
        s = self._check_sector(sector)
        if not isinstance(value, str):
            value = str(value)
        raw = value.encode("latin-1", errors="replace")[:SECTOR_SIZE]
        if len(raw) < SECTOR_SIZE:
            raw = raw + b"\x00" * (SECTOR_SIZE - len(raw))
        start = s * SECTOR_SIZE
        self._data[start:start + SECTOR_SIZE] = raw
        self._save()
        return True

    def read_byte(self, offset, *_):
        o = self._check_offset(offset)
        b = self._data[o]
        return b - 256 if b >= 128 else b

    def write_byte(self, offset, value, *_):
        o = self._check_offset(offset)
        try:
            v = int(value) & 0xFF
        except (TypeError, ValueError):
            raise TypeError(
                f"bad argument #2 (number expected, got {type(value).__name__})")
        self._data[o] = v
        self._save()
        return True

    def as_proxy(self) -> dict:
        return {
            "getLabel":        self.get_label,
            "setLabel":        self.set_label,
            "getCapacity":     self.get_capacity,
            "getSectorSize":   self.get_sector_size,
            "getPlatterCount": self.get_platter_count,
            "readSector":      self.read_sector,
            "writeSector":     self.write_sector,
            "readByte":        self.read_byte,
            "writeByte":       self.write_byte,
        }


def register(address: str | None = None, save_dir: str | None = None,
             tier: int = 0, label: str = DEFAULT_LABEL) -> str | None:
    if address is None:
        import uuid as _uuid
        address = str(_uuid.uuid5(_uuid.NAMESPACE_OID, "lupi-drive"))

    base = save_dir or _default_save_dir or os.path.abspath("drive_storage")
    # base — это уже каталог, в котором лежат <uuid>/, без промежуточного "drives/".
    storage_dir = os.path.join(base, address)

    comp = DriveComponent(address, storage_dir, tier=tier, label=label)
    addr = component_mod.register(address, "drive", comp.as_proxy())
    logger.info("Drive registered: %s (tier %d, %d bytes, label=%r, dir=%s)",
                addr, tier, comp.capacity, comp._label, storage_dir)
    return addr