"""
filesystem.py — порт src/lua/core/filesystem.lua + src/c/lnative.c (fs-часть)

Оборачивает реальную директорию как компонент filesystem.
Все данные (read/write) гоняются в latin-1 byte-в-byte.
Метод list() возвращает LuaIterable — OC-совместимый callable-table.

Метка (label) — виртуальное имя раздела, которое видит Lua через getLabel().
По умолчанию = basename(base_path) ("root", "home" и т.п.), а не полный
путь ОС. setLabel() реально сохраняет метку (обрезая до 16 символов, как
в OCEmu), а не возвращает аргумент.

readonly — если True, запрещает открытие на запись / remove / rename /
makeDirectory / setLabel. По умолчанию False. Пробрасывается из
component.connect() (см. core/component.py).

rate_limit — если > 0, ограничивает суммарную скорость чтения и записи
байт/сек (token bucket, допускает «всплеск» до 100 мс, чтобы мелкие
операции вроде getLabel() не тормозили). Устанавливается глобально через
set_default_rate_limit() (см. init.py --fs-speed). 0 = без лимита.

hdd_hook — callback, устанавливаемый через set_hdd_activity_hook().
Вызывается на реальном I/O (read/write/open w|a/remove/rename/
makeDirectory). Используется --allow-pc-sounds для клика HDD.
"""
from __future__ import annotations
import os
import time
import shutil
import logging
from pathlib import Path

from core import component as component_module

logger = logging.getLogger("lupi.filesystem")


# ── Глобальный лимит скорости (по умолчанию 0 = unlimited) ────────────────────
_default_rate_limit = 0


def set_default_rate_limit(rate: int) -> None:
    """Установить лимит скорости чтения/записи байт/сек для ВСЕХ новых ФС.

    0 = без лимита. Применяется ко всем последующим register() и
    component.connect("filesystem", ...). Уже зарегистрированные ФС
    сохраняют свой лимит (при желании можно пересоздать).
    """
    global _default_rate_limit
    try:
        n = int(rate) if rate else 0
    except (TypeError, ValueError):
        n = 0
    _default_rate_limit = max(0, n)
    logger.info("Filesystem default rate limit: %s",
                f"{_default_rate_limit} B/s" if _default_rate_limit else "unlimited")


def get_default_rate_limit() -> int:
    return _default_rate_limit


# ── HDD activity hook (используется --allow-pc-sounds) ────────────────────────
_hdd_hook = None


def set_hdd_activity_hook(fn) -> None:
    """
    Установить callback, вызываемый на реальном I/O (read/write/open w|a/
    remove/rename/makeDirectory). Ставится один раз на старте эмулятора.
    """
    global _hdd_hook
    _hdd_hook = fn


def _notify_hdd() -> None:
    hook = _hdd_hook
    if hook is not None:
        try:
            hook()
        except Exception:
            pass


# ── Lua <-> Python строковые конверсии ────────────────────────────────────────
def _from_lua(s):
    if not isinstance(s, str):
        return s
    try:
        return s.encode('latin-1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def _to_lua(s):
    """
    Python str → latin-1-строка для Lua.

    UTF-8 байты всегда укладываются в latin-1 (0..255), поэтому encode/decode
    здесь не может упасть — except не нужен.
    """
    if not isinstance(s, str):
        return s
    return s.encode('utf-8').decode('latin-1')


# ── Token bucket ──────────────────────────────────────────────────────────────
class _RateLimiter:
    """Ограничитель суммарной скорости (байт/сек) через token bucket.

    max_tokens = rate * 0.1 — разрешаем «всплеск» до 100 мс, чтобы мелкие
    операции (list/getLabel и т.п.) не тормозили, но устойчивая скорость
    не превышает rate.
    """

    def __init__(self, bytes_per_sec: float):
        self.rate = float(bytes_per_sec)
        self.last = time.monotonic()
        self.tokens = 0.0
        self.max_tokens = max(self.rate * 0.1, 1.0)

    def consume(self, n_bytes: int) -> None:
        if self.rate <= 0 or n_bytes <= 0:
            return

        now = time.monotonic()
        self.tokens = min(self.max_tokens,
                          self.tokens + (now - self.last) * self.rate)
        self.last = now

        if n_bytes > self.tokens:
            deficit = n_bytes - self.tokens
            self._sleep(deficit / self.rate)
            self.tokens = 0.0
            self.last = time.monotonic()
        else:
            self.tokens -= n_bytes

    @staticmethod
    def _sleep(seconds: float) -> None:
        """Спим, но пумпим pygame/Tk, чтобы окно не «замерзало»."""
        if seconds <= 0:
            return
        deadline = time.monotonic() + seconds
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            try:
                from core import computer as _c
                _c._maybe_pump()
            except Exception:
                pass
            time.sleep(min(0.01, remain))


class LuaIterable:
    """
    Маркер для list(): init.py превратит это в Lua-таблицу с __call.
    Такая таблица одновременно:
      * индексируется как массив (tbl[1] = "file1", ...)
      * вызывается как итератор (для `for name in tbl do`)
    — как в OpenComputers.
    """
    __slots__ = ("items",)
    def __init__(self, items):
        self.items = items


def _segments(path: str) -> list[str]:
    path = path.replace("\\", "/")
    while "//" in path:
        path = path.replace("//", "/")
    parts = [p for p in path.split("/") if p]
    result = []
    for part in parts:
        if part == ".":
            continue
        elif part == "..":
            if result:
                result.pop()
        else:
            result.append(part)
    return result


def _canonical(path: str) -> str:
    segs = _segments(path)
    joined = "/".join(segs)
    return ("/" + joined) if path.startswith("/") else joined


class Filesystem:
    def __init__(self, base_path: str, label: str | None = None,
                 readonly: bool = False, speed: int = 4,
                 rate_limit: int | None = None):
        self.base_path = str(Path(base_path).resolve())
        os.makedirs(self.base_path, exist_ok=True)
        self._handles: dict[int, object] = {}
        self._next_handle = 1
        self._readonly = bool(readonly)
        self._speed = int(speed)  # OCEmu передаёт tier/speed, у нас не используется
        if label is None:
            label = os.path.basename(self.base_path) or "/"
        self._label = str(label)

        # rate_limit: None → взять глобальный default
        if rate_limit is None:
            rate_limit = _default_rate_limit
        try:
            self._rate_limit = max(0, int(rate_limit)) if rate_limit else 0
        except (TypeError, ValueError):
            self._rate_limit = 0
        self._limiter = (_RateLimiter(self._rate_limit)
                         if self._rate_limit > 0 else None)

    def _real(self, path: str) -> str:
        if isinstance(path, str):
            path = _from_lua(path)
        return os.path.join(self.base_path, _canonical(path).lstrip("/"))

    def _err(self, e: BaseException) -> str:
        """Спрятать абсолютный base_path из текста ошибки."""
        msg = str(e)
        if self.base_path and self.base_path in msg:
            msg = msg.replace(self.base_path, "").lstrip("\\/")
        return msg.encode('latin-1', errors='replace').decode('latin-1') or "io error"

    # ── API методы ──────────────────────────────────────────────────────────

    def space_used(self, *_):
        total = 0
        for dirpath, _, filenames in os.walk(self.base_path):
            for f in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
        return total

    def space_total(self, *_):
        try:
            st = os.statvfs(self.base_path)
            return st.f_frsize * st.f_blocks
        except AttributeError:
            total, _, _ = shutil.disk_usage(self.base_path)
            return total

    def open(self, path, mode="r", *_):
        if mode is None:
            mode = "r"
        m = "r"
        if "a" in mode:
            m = "a"
        elif "w" in mode:
            m = "w"
        if m != "r" and self._readonly:
            return None, "filesystem is read only"
        try:
            f = open(self._real(path), m + "b")
        except OSError as e:
            return None, self._err(e)
        handle = self._next_handle
        self._next_handle += 1
        self._handles[handle] = f
        if m != "r":
            _notify_hdd()
        return handle

    def seek(self, handle, whence, offset=0, *_):
        f = self._handles.get(handle)
        if f is None:
            return None, "bad file descriptor"
        w_map = {"cur": 1, "set": 0, "end": 2}
        w = w_map.get(whence)
        if w is None:
            return None, "invalid whence"
        return f.seek(offset, w)

    def make_directory(self, path, *_):
        if self._readonly:
            return False
        try:
            os.makedirs(self._real(path), exist_ok=True)
            _notify_hdd()
            return True
        except OSError:
            return False

    def exists(self, path, *_):
        return os.path.exists(self._real(path))

    def is_read_only(self, *_):
        return self._readonly

    def write(self, handle, value, *_):
        f = self._handles.get(handle)
        if f is None:
            return None, "bad file descriptor"
        try:
            if isinstance(value, str):
                value = value.encode("latin-1", errors="replace")
            elif not isinstance(value, (bytes, bytearray)):
                value = str(value).encode("utf-8")
            # Throttle ДО записи: размер известен, списываем токены заранее.
            if self._limiter is not None:
                self._limiter.consume(len(value))
            f.write(value)
            _notify_hdd()
            return True
        except OSError as e:
            return None, self._err(e)

    def is_directory(self, path, *_):
        return os.path.isdir(self._real(path))

    def rename(self, from_path, to_path, *_):
        if self._readonly:
            return False
        try:
            os.rename(self._real(from_path), self._real(to_path))
            _notify_hdd()
            return True
        except OSError as e:
            logger.error("OSError renaming %s to %s: %s", from_path, to_path, e)
            return False, str(e)

    def remove(self, path, *_):
        if self._readonly:
            return False
        real = self._real(path)
        try:
            if os.path.isdir(real):
                shutil.rmtree(real)
            else:
                os.unlink(real)
            _notify_hdd()
            return True
        except OSError as e:
            logger.error("OSError removing %s: %s", path, e)
            return False, str(e)

    def list(self, path, *_):
        """
        OC-совместимый list:
          * несуществующий путь        → nil, "no such file or directory"
          * путь на файл               → [basename] (LuaIterable)
          * путь на директорию         → [name1, name2, ...]
                                            (имена директорий с "/" в конце)

        Возвращает LuaIterable — init.py превратит это в Lua-таблицу с __call.
        """
        real = self._real(path)
        if not os.path.exists(real):
            return None, "no such file or directory"

        if not os.path.isdir(real):
            base = os.path.basename(real)
            safe = base.encode('latin-1', errors='replace').decode('latin-1')
            return LuaIterable([safe])

        try:
            entries = os.listdir(real)
        except OSError as e:
            return None, self._err(e)

        items = []
        for e in sorted(entries):
            full = os.path.join(real, e)
            name = e + ("/" if os.path.isdir(full) else "")
            safe_name = _to_lua(name)  # <--- ИСПОЛЬЗУЕМ ПРАВИЛЬНУЮ КОНВЕРТАЦИЮ
            items.append(safe_name)
        return LuaIterable(items)

    def last_modified(self, path, *_):
        try:
            return int(os.path.getmtime(self._real(path)) * 1000)
        except OSError as e:
            return None, self._err(e)

    def get_label(self, *_):
        return _to_lua(self._label)

    def set_label(self, value, *_):
        """
        OCEmu-совместимая семантика:
          * checkArg (string expected) — TypeError при неверном типе
          * readonly                 — RuntimeError
          * truncate to 16 chars     — как value:sub(1,16) в OCEmu
        """
        value = _from_lua(value)
        if not isinstance(value, str):
            raise TypeError(
                f"bad argument #1 (string expected, got {type(value).__name__})")
        if self._readonly:
            raise RuntimeError("label is read only")
        self._label = value[:16]
        return _to_lua(self._label)

    def close(self, handle, *_):
        f = self._handles.pop(handle, None)
        if f is None:
            return None, "bad file descriptor"
        f.close()
        return True

    def size(self, path, *_):
        try:
            return os.path.getsize(self._real(path))
        except OSError as e:
            return None, self._err(e)

    def read(self, handle, count, *_):
        """
        latin-1, чтобы байты не портились (.pic, бинарные иконки и т.д.).

        Семантика OC:
          count == inf / огромное число → читаем всё, nil в конце файла.
          count < 0                     → клампим в 0.
          count == 0                    → "" если НЕ в конце файла,
                                          nil если в конце.
          count > 0                     → читаем до n байт,
                                          nil в конце файла.
        """
        f = self._handles.get(handle)
        if f is None:
            return None, "bad file descriptor"
        try:
            if count == float('inf') or count > 10**9:
                data = f.read()
                if not data:
                    return None
            else:
                try:
                    n = int(count) if count is not None else 0
                except (TypeError, ValueError):
                    return None, "bad argument #2 (number expected)"
                if n < 0:
                    n = 0
                if n == 0:
                    # OC semantics: read(0) → nil at EOF, "" otherwise.
                    # Python f.read(0) всегда возвращает b"", поэтому
                    # проверяем EOF через peek.
                    pos = f.tell()
                    peek = f.read(1)
                    if not peek:
                        return None
                    f.seek(pos)
                    _notify_hdd()
                    return ""
                data = f.read(n)
                if not data:
                    return None
            # Throttle ПОСЛЕ чтения: реальный размер не был известен заранее.
            if self._limiter is not None:
                self._limiter.consume(len(data))
            _notify_hdd()
            return data.decode("latin-1")
        except OSError as e:
            return None, self._err(e)
        except Exception as e:
            return None, self._err(e)

    def as_proxy(self) -> dict:
        return {
            "spaceUsed":     self.space_used,
            "spaceTotal":    self.space_total,
            "open":          self.open,
            "seek":          self.seek,
            "makeDirectory": self.make_directory,
            "exists":        self.exists,
            "isReadOnly":    self.is_read_only,
            "write":         self.write,
            "isDirectory":   self.is_directory,
            "rename":        self.rename,
            "list":          self.list,
            "lastModified":  self.last_modified,
            "getLabel":      self.get_label,
            "setLabel":      self.set_label,
            "remove":        self.remove,
            "close":         self.close,
            "size":          self.size,
            "read":          self.read,
        }


def register(base_path: str, uuid: str | None = None,
             label: str | None = None, readonly: bool = False,
             speed: int = 4, rate_limit: int | None = None) -> str:
    fs = Filesystem(base_path, label=label, readonly=readonly,
                    speed=speed, rate_limit=rate_limit)
    addr = component_module.register(uuid, "filesystem", fs.as_proxy())
    logger.info("Filesystem registered: %s -> %s (label=%r, readonly=%s, rate_limit=%s)",
                addr, base_path, fs._label, readonly,
                f"{fs._rate_limit} B/s" if fs._rate_limit else "unlimited")
    return addr