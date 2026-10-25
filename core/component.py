"""
component.py — порт src/lua/core/component.lua

Реестр компонентов: регистрация, удаление, вызов методов, поиск.
"""
from __future__ import annotations
from typing import Any, Callable
import logging
import traceback

logger = logging.getLogger("lupi.component")

# Глобальный реестр: address -> ComponentEntry
_components: dict[str, "_ComponentEntry"] = {}
_push_signal: Callable | None = None
_uuid: Callable | None = None

# Глобальный hook: превращает Python list/dict → Lua-таблицу.
_result_converter: Callable | None = None

# Fast-path для конвертации: примитивы не нужно гонять через _lua_convert
_PRIMITIVES = (bool, int, float, str, bytes, type(None))

# Fast-path для проверки аргументов: эти типы точно НЕ являются Lua-таблицами
_PYTHON_TYPES = (str, bytes, int, float, bool, list, dict, tuple, type(None))


def set_result_converter(fn):
    global _result_converter
    _result_converter = fn


def _convert(result):
    if _result_converter is None:
        return result
    # Если результат — примитив, возвращаем как есть (Lupa отлично его понимает)
    if type(result) in _PRIMITIVES:
        return result
    return _result_converter(result)


def _is_lua_table(x) -> bool:
    """Heuristic: lupa table (не str/bytes/число/list/dict/tuple/None)."""
    # type() работает быстрее, чем isinstance() с длинным кортежем
    if type(x) in _PYTHON_TYPES:
        return False
    return hasattr(x, "items") or hasattr(x, "keys")


def _lua_truthy(v) -> bool:
    """
    Lua-семантика истинности: false и nil — ложь, ВСЁ остальное (включая 0
    и пустые строки) — правда. Это НЕ совпадает с Python bool(), поэтому
    используем для всех опциональных boolean-аргументов, приходящих из Lua.
    """
    return v is not None and v is not False


class _ComponentEntry:
    def __init__(self, address: str, ctype: str, proxy: dict, doc: dict):
        self.address = address
        self.type = ctype
        self.doc = doc
        self.raw_proxy = proxy
        self.proxy: dict = {}
        for k, v in proxy.items():
            if callable(v):
                self.proxy[k] = _ComponentCallback(k, address)
            else:
                self.proxy[k] = v


class _ComponentCallback:
    """Аналог componentCallback в Lua — вызываемый объект с мета-информацией."""

    def __init__(self, name: str, address: str):
        self.name = name
        self.address = address

    def __call__(self, *args):
        entry = _components.get(self.address)
        if entry is None:
            raise RuntimeError(f"Component {self.address!r} not found")
        fn = entry.raw_proxy.get(self.name)
        if fn is None:
            raise RuntimeError(f"Method {self.name!r} not found on {entry.type}")

        # Если Lua вызвал через `:` (colon) — первый аргумент сама proxy-таблица.
        # Оптимизация: проверяем только если аргументы вообще есть.
        if args and _is_lua_table(args[0]):
            args = args[1:]

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("invoke(%s): %s.%s(%s)", self.address, entry.type, self.name,
                         ", ".join(repr(a) for a in args))
        try:
            return _convert(fn(*args))
        except Exception as e:
            logger.error("Python exception in %s.%s(%s):\n%s",
                         entry.type, self.name,
                         ", ".join(repr(a) for a in args),
                         traceback.format_exc())
            # Очищаем текст ошибки от кириллицы перед выбросом в Lupa
            safe_msg = str(e).encode('latin-1', errors='replace').decode('latin-1')
            raise RuntimeError(safe_msg)

    def __str__(self):
        entry = _components.get(self.address)
        if entry and self.name in entry.doc:
            return entry.doc[self.name]
        return "function"


# ── Public API ────────────────────────────────────────────────────────────────

def prepare():
    logger.info("Assembling initial component tree")


def register(address: str | None, ctype: str, proxy: dict, doc: dict | None = None):
    """Зарегистрировать компонент. Возвращает адрес (UUID)."""
    if not isinstance(ctype, str):
        raise TypeError("ctype must be str")
    if not isinstance(proxy, dict):
        raise TypeError("proxy must be dict")

    if not isinstance(address, str):
        if _uuid is None:
            raise RuntimeError("uuid generator not set — call init() first")
        address = _uuid()

    if address in _components:
        return None, "component already at address"

    _components[address] = _ComponentEntry(address, ctype, proxy, doc or {})
    logger.debug("register(%s, %s)", address, ctype)

    if _push_signal:
        _push_signal("component_added", address, ctype)

    return address


def unregister(address: str):
    if not isinstance(address, str):
        raise TypeError("address must be str")
    if address not in _components:
        return None, "no component at address"

    ctype = _components[address].type
    del _components[address]

    if _push_signal:
        _push_signal("component_removed", address, ctype)
    return True


def invoke(address: str, method: str, *args) -> Any:
    if address not in _components:
        raise RuntimeError(f"No such component: {address!r}")
    fn = _components[address].raw_proxy.get(method)
    if fn is None:
        raise RuntimeError(
            f"No such method: {_components[address].type}.{method}"
        )
    try:
        return _convert(fn(*args))
    except Exception as e:
        logger.error("Python exception in invoke(%s, %s):\n%s",
                     address, method, traceback.format_exc())
        safe_msg = str(e).encode('latin-1', errors='replace').decode('latin-1')
        raise RuntimeError(safe_msg)


def list(filter: str | None = None, exact: bool = False):
    """Python-версия component.list — возвращает (iterator, table)."""
    if isinstance(filter, bytes):
        filter = filter.decode('utf-8')
    if isinstance(exact, int):
        exact = bool(exact)

    matches = []
    for addr, c in _components.items():
        if filter is None:
            matches.append((addr, c.type))
        elif exact:
            if c.type == filter:
                matches.append((addr, c.type))
        else:
            if c.type.startswith(filter):
                matches.append((addr, c.type))

    it = iter(matches)

    def iterator(*_):
        try:
            return next(it)
        except StopIteration:
            return None

    tbl = {addr: ctype for addr, ctype in matches}
    return iterator, tbl


def methods(address: str):
    if address not in _components:
        return None, "No such component"
    return {
        k: True
        for k, v in _components[address].raw_proxy.items()
        if callable(v)
    }


def fields(address: str):
    if address not in _components:
        return None, "No such component"
    return {
        k: True
        for k, v in _components[address].raw_proxy.items()
        if not callable(v)
    }


def proxy(address: str):
    if address not in _components:
        return None, "No such component"
    entry = _components[address]
    result = dict(entry.proxy)
    result["address"] = entry.address
    result["type"] = entry.type
    return result


def doc(address: str, method: str) -> str | None:
    if address not in _components:
        raise RuntimeError("No such component")
    return _components[address].doc.get(method)


def get_type(address: str) -> str:
    return _components[address].type


def slot():
    """Legacy stub."""
    return 0


def init(push_signal_fn: Callable, uuid_fn: Callable):
    """Инициализировать зависимости. Вызывается из lupi/init.py."""
    global _push_signal, _uuid
    _push_signal = push_signal_fn
    _uuid = uuid_fn


# ─── Расширения, совместимые с OCEmu ─────────────────────────────────────────

def exists(address: str):
    """component.exists(addr) -> type | nil."""
    if not isinstance(address, str):
        return None
    entry = _components.get(address)
    return entry.type if entry else None


def connect(kind, address=None, slot=None, *args):
    """
    Создать компонент по имени типа.

    Сигнатура OCEmu:
        component.connect(kind, address, slot, arg1, arg2, ...)

    Возвращает:
        True      — успех (совместимо с ocemu.connect в OpenOS)
        nil, err  — ошибка (Lua-конвенция)

    ВАЖНО: раньше возвращался str (адрес) или tuple (None, err). Оба варианта
    truthy в Lua, поэтому `if ok then` срабатывал даже при ошибке. Теперь
    возвращаем именно True / (None, err).
    """
    kind = str(kind)

    # Если address передан не строкой (например, случайно прилетел слот
    # вместо UUID) — принудительно генерируем новый.
    if address is not None and not isinstance(address, str):
        address = None

    try:
        if kind == "filesystem":
            from core import filesystem as fs_mod
            path = args[0] if len(args) > 0 else None
            label = args[1] if len(args) > 1 else None
            readonly = _lua_truthy(args[2]) if len(args) > 2 else False
            try:
                speed = int(args[3]) if len(args) > 3 and args[3] is not None else 4
            except (TypeError, ValueError):
                speed = 4
            if path is None:
                path = "fs_" + (address or "default")
            result = fs_mod.register(path, uuid=address, label=label,
                                     readonly=readonly, speed=speed)
            if isinstance(result, str):
                return True
            return result  # (None, err) от register

        if kind == "data":
            from core import data as data_mod
            try:
                tier = int(args[0]) if args and args[0] is not None else 3
            except (TypeError, ValueError):
                tier = 3
            result = data_mod.register(address, tier=tier)
            if isinstance(result, str):
                return True
            return result

        if kind == "modem":
            from core import modem as modem_mod
            wireless = _lua_truthy(args[0]) if args else False
            result = modem_mod.register(address, wireless=wireless)
            if isinstance(result, str):
                return True
            return result

        if kind == "eeprom":
            from core import eeprom as eeprom_mod
            filename = (args[0] if len(args) > 0 and isinstance(args[0], str)
                        else "bios/code.lua")
            result = eeprom_mod.register(uuid=address, filename=filename)
            if isinstance(result, str):
                return True
            return result

        if kind == "internet":
            from core import internet as internet_mod
            result = internet_mod.start()
            if isinstance(result, str):
                return True
            return result

        if kind == "sound":
            from core import sound as sound_mod
            result = sound_mod.register(address)
            if isinstance(result, str):
                return True
            return result

        if kind == "clipboard":
            from core import clipboard as clipboard_mod
            result = clipboard_mod.register(address)
            if isinstance(result, str):
                return True
            return result

    except Exception as e:
        logger.exception("component.connect(%s) failed", kind)
        safe = str(e).encode('latin-1', errors='replace').decode('latin-1')
        return None, safe

    return None, "unknown component kind: " + kind


def disconnect(address: str):
    # Если это модем — убираем из его глобального реестра
    if exists(address) == "modem":
        try:
            from core import modem as modem_mod
            modem_mod.unregister(address)
            return True
        except Exception:
            logger.exception("modem unregister failed")

    return unregister(address)