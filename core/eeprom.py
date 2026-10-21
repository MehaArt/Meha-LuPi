"""
eeprom.py — порт src/lua/core/eeprom.lua + src/component/eeprom.lua (OCEmu)

EEPROM хранит загрузочный код и небольшие данные. У нас на машине РОВНО
ОДИН EEPROM, поэтому хранилище — сам файл BIOS + один JSON-сайдкар:

    bios/code.lua          ← код (это путь --bios; читается/пишется напрямую)
    bios/code.lua.meta     ← {"uuid": "...", "data": "...", "label": "...",
                             "readonly": bool}

get()/set(code)       → файл BIOS
getData()/setData()   → meta["data"]
getLabel()/setLabel() → meta["label"]
makeReadonly()        → meta["readonly"]

UUID EEPROM стабилен: генерируется один раз и сохраняется в meta["uuid"].
При последующих запусках читается оттуда. Явный --eeprom-uuid переопределяет
и записывает новое значение в meta.

Семантика — OC/OCEmu-совместимая:
  * set(code) на readonly                         → nil, "storage is readonly"
  * setLabel(value) на readonly                   → nil, "storage is readonly"
  * setData(data) — readonly НЕ проверяется (в OCEmu тоже; иначе нельзя
    обновлять bootAddress каждый boot при случайных UUID файловых систем)
  * set(code) с len(code) > HARD_MAX_EEPROM_SIZE  → raise "not enough space"
  * set(code) с len(code) > текущего size         → size РАСТЁТ, чтобы
    вместить новый код (OCEmu-семантика запрещает молча обрезать данные)
  * makeReadonly(checksum) с неверным checksum    → nil, "incorrect checksum"
  * getSize()                                     → max(4096, len(code)+256)
  * getDataSize()                                 → 256
"""
from __future__ import annotations
import os
import json
import zlib
import logging

from core import component as component_module

logger = logging.getLogger("lupi.eeprom")

EEPROM_SIZE           = 4096                # нижняя граница getSize()
EEPROM_DATA_SIZE      = 256                 # лимит data
HARD_MAX_EEPROM_SIZE  = 1024 * 1024         # 1 МиБ: страховка от runaway
DEFAULT_LABEL         = "EEPROM"

DEFAULT_BOOT_CODE = """\
-- Default LuPI BIOS (Python port)
local component_invoke = component.invoke
function boot_invoke(address, method, ...)
  local result = table.pack(pcall(component_invoke, address, method, ...))
  if not result[1] then
    return nil, result[2]
  else
    return table.unpack(result, 2, result.n)
  end
end

local eeprom = component.list("eeprom")()
computer.getBootAddress = function()
  return boot_invoke(eeprom, "getData")
end
computer.setBootAddress = function(address)
  return boot_invoke(eeprom, "setData", address)
end

do
  local screen = component.list("screen")()
  local gpu = component.list("gpu")()
  if gpu and screen then
    boot_invoke(gpu, "bind", screen)
  end
end

local function tryLoadFrom(address)
  local handle, reason = boot_invoke(address, "open", "/init.lua")
  if not handle then return nil, reason end
  local buffer = ""
  repeat
    local data, reason = boot_invoke(address, "read", handle, math.huge)
    if not data and reason then return nil, reason end
    buffer = buffer .. (data or "")
  until not data
  boot_invoke(address, "close", handle)
  return load(buffer, "=init")
end

local init, reason
if computer.getBootAddress then
  init, reason = tryLoadFrom(computer.getBootAddress())
end
if not init then
  for address in component.list("filesystem") do
    init, reason = tryLoadFrom(address)
    if init then break end
  end
end
if not init then
  error("no bootable medium found" .. (reason and (": " .. tostring(reason)) or ""), 0)
end
init()
"""


class EEPROM:
    def __init__(self, address: str | None, filename: str):
        self._code_path = os.path.abspath(filename)
        self._meta_path = self._code_path + ".meta"
        self._dir = os.path.dirname(self._code_path) or os.getcwd()
        os.makedirs(self._dir, exist_ok=True)

        # ── Метаданные (заполняют data/label/readonly/_meta_uuid) ──────
        self._data = ""
        self._label = DEFAULT_LABEL
        self._readonly = False
        self._meta_uuid: str | None = None
        self._load_meta()

        # ── Адрес EEPROM ────────────────────────────────────────────────
        # Приоритет: явный аргумент > сохранённый в meta > новый UUID.
        if address is None:
            if self._meta_uuid:
                self.address = self._meta_uuid
            else:
                from util import random as random_mod
                self.address = random_mod.uuid()
        else:
            self.address = address

        # ── Код BIOS ────────────────────────────────────────────────────
        if os.path.isfile(self._code_path):
            self._code = self._read_text(self._code_path)
        else:
            self._code = DEFAULT_BOOT_CODE
            try:
                self._write_text(self._code_path, self._code)
                logger.info("EEPROM: created default BIOS at %s (%d B)",
                            self._code_path, len(self._code))
            except OSError as e:
                raise RuntimeError(
                    f"cannot create BIOS file {self._code_path!r}: {e}") from e

        self._size = max(EEPROM_SIZE, len(self._code) + 256)

        # ── Если UUID новый (или отличается от meta) — сохраняем ────────
        if self._meta_uuid != self.address:
            if not self._save_meta():
                logger.warning("EEPROM: could not persist uuid=%s to meta",
                               self.address)

    # ── io helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _read_text(path: str) -> str:
        with open(path, "rb") as f:
            return f.read().decode("latin-1")

    @staticmethod
    def _write_text(path: str, text: str) -> None:
        with open(path, "wb") as f:
            f.write(text.encode("latin-1", errors="replace"))

    def _load_meta(self) -> None:
        try:
            with open(self._meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        if not isinstance(meta, dict):
            return

        uuid_val = meta.get("uuid")
        if isinstance(uuid_val, str) and uuid_val:
            self._meta_uuid = uuid_val

        data = meta.get("data", "")
        if isinstance(data, str):
            self._data = data[:EEPROM_DATA_SIZE]

        label = meta.get("label", DEFAULT_LABEL)
        if isinstance(label, str):
            self._label = label[:16]

        self._readonly = bool(meta.get("readonly", False))

    def _save_meta(self) -> bool:
        meta = {
            "uuid":     self.address,
            "data":     self._data,
            "label":    self._label,
            "readonly": self._readonly,
        }
        try:
            with open(self._meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
            return True
        except OSError as e:
            logger.error("EEPROM: cannot write meta %s: %s",
                         self._meta_path, e)
            return False

    def _checksum(self) -> str:
        crc = zlib.crc32(self._code.encode("latin-1", errors="replace"))
        return f"{crc & 0xFFFFFFFF:08x}"

    # ── code: get / set / getSize / getChecksum / makeReadonly ─────────

    def get(self, *_):
        return self._code

    def set(self, code=None, *_):
        if self._readonly:
            return None, "storage is readonly"
        if code is None:
            code = ""
        if not isinstance(code, str):
            raise TypeError(
                f"bad argument #1 (string expected, got {type(code).__name__})")
        if len(code) > HARD_MAX_EEPROM_SIZE:
            raise ValueError("not enough space")
        try:
            self._write_text(self._code_path, code)
        except OSError as e:
            raise RuntimeError(f"cannot write BIOS file: {e}") from e
        self._code = code
        # Растим size под новый код (OCEmu-семантика: не обрезаем молча).
        self._size = max(EEPROM_SIZE, len(code) + 256)
        return True

    def get_size(self, *_):
        return self._size

    def get_checksum(self, *_):
        return self._checksum()

    def make_readonly(self, checksum=None, *_):
        if checksum is None or not isinstance(checksum, str):
            raise TypeError(
                f"bad argument #1 (string expected, "
                f"got {type(checksum).__name__})")
        if checksum != self._checksum():
            return None, "incorrect checksum"
        if not self._readonly:
            self._readonly = True
            if not self._save_meta():
                self._readonly = False
                return None, "failed to persist readonly"
        return True

    # ── data: getData / setData / getDataSize ──────────────────────────
    #
    # ВАЖНО: setData НЕ проверяет readonly. В OC/OCEmu readonly проверяют
    # только set() и setLabel(). init.py обновляет bootAddress через
    # setData() каждый boot (UUID файловых систем случайные), иначе
    # getBootAddress() будет указывать в никуда и BIOS крашится.

    def get_data(self, *_):
        return self._data

    def set_data(self, data=None, *_):
        if data is None:
            data = ""
        if not isinstance(data, str):
            raise TypeError(
                f"bad argument #1 (string expected, got {type(data).__name__})")
        if len(data) > EEPROM_DATA_SIZE:
            raise ValueError("not enough space")
        old = self._data
        self._data = data
        if not self._save_meta():
            self._data = old
            raise RuntimeError("cannot write EEPROM meta")
        return True

    def get_data_size(self, *_):
        return EEPROM_DATA_SIZE

    # ── label: getLabel / setLabel ─────────────────────────────────────

    def get_label(self, *_):
        return self._label

    def set_label(self, value=None, *_):
        if self._readonly:
            return None, "storage is readonly"
        if value is None:
            value = DEFAULT_LABEL
        if not isinstance(value, str):
            raise TypeError(
                f"bad argument #1 (string expected, got {type(value).__name__})")
        value = value[:16]
        old = self._label
        self._label = value
        if not self._save_meta():
            self._label = old
            raise RuntimeError("cannot write EEPROM meta")
        return value

    # ── proxy ──────────────────────────────────────────────────────────

    def as_proxy(self) -> dict:
        return {
            "get":          self.get,
            "set":          self.set,
            "getSize":      self.get_size,
            "getChecksum":  self.get_checksum,
            "makeReadonly": self.make_readonly,
            "getData":      self.get_data,
            "setData":      self.set_data,
            "getDataSize":  self.get_data_size,
            "getLabel":     self.get_label,
            "setLabel":     self.set_label,
        }


def register(uuid: str | None = None,
             filename: str = "bios/code.lua") -> str:
    """
    Зарегистрировать EEPROM-компонент.

    uuid      — если None, адрес берётся из <bios>.meta["uuid"]. Если
                там пусто — генерируется новый UUID и сохраняется в meta
                (стабилен между запусками). Если передан явно — переопределяет
                и тоже сохраняется в meta.
    filename  — путь к файлу BIOS, который одновременно является хранилищем
                кода (get/set читают/пишут этот файл). Сайдкар создаётся как
                filename + ".meta".
    """
    eeprom = EEPROM(uuid, filename)
    addr = component_module.register(eeprom.address, "eeprom", eeprom.as_proxy())
    logger.info("EEPROM registered: %s (code=%s, size=%d, readonly=%s)",
                addr, eeprom._code_path, eeprom._size, eeprom._readonly)
    return addr