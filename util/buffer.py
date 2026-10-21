"""
util/buffer.py — порт src/lua/core/util/buffer.lua

Буферизованная обёртка над произвольным stream-объектом.
Stream должен иметь методы:
    read(n: int) -> str | None
    write(data: str) -> bool | None
    close() -> None
"""
from __future__ import annotations

import time


class Buffer:
    DEFAULT_BUFFER_SIZE = 512

    def __init__(self, mode: str, stream):
        """
        mode: 'r', 'w', 'a', 'rb', 'wb' и т.д.
        stream: объект с методами read / write / close
        """
        self.stream = stream
        self.mode = mode
        self.closed = False

        self.buffer_mode = "full"   # "no" | "line" | "full"
        self.buffer_size = self.DEFAULT_BUFFER_SIZE
        self.buffer_read: str = ""
        self.buffer_write: str = ""
        self.read_timeout: float | None = None

    # ------------------------------------------------------------------ close
    def close(self):
        if self.closed:
            return None, "bad file descriptor"
        self.flush()
        self.stream.close()
        self.closed = True
        return True

    # ------------------------------------------------------------------ flush
    def flush(self):
        if self.closed:
            return None, "bad file descriptor"
        if self.buffer_write:
            result = self.stream.write(self.buffer_write)
            self.buffer_write = ""
            if not result:
                return None, "failed to write"
        return self

    # ------------------------------------------------------------------ read
    def read(self, fmt="*l"):
        """
        fmt:
          '*l' | 'l'  — читать строку (без \n)
          '*L' | 'L'  — читать строку (с \n)
          '*a' | 'a'  — читать всё
          '*n' | 'n'  — читать число
          int         — читать N байт
        """
        if self.closed:
            return None, "bad file descriptor"

        if fmt in ("*l", "l", "*L", "L"):
            keep_newline = fmt in ("*L", "L")
            return self._read_line(keep_newline)
        elif fmt in ("*a", "a"):
            return self._read_all()
        elif fmt in ("*n", "n"):
            line, err = self._read_line(False)
            if line is None:
                return None, err
            try:
                return float(line)
            except ValueError:
                return None
        elif isinstance(fmt, int):
            return self._read_n(fmt)
        else:
            return None, "bad argument"

    def _fill_buffer(self) -> bool:
        """Подтянуть данные из stream в буфер чтения. Возвращает True если данные появились."""
        deadline = (time.monotonic() + self.read_timeout) if self.read_timeout else None
        while True:
            chunk = self.stream.read(self.buffer_size)
            if chunk:
                self.buffer_read += chunk
                return True
            if chunk is None:          # EOF / соединение закрыто
                return False
            # chunk == "" — нет данных сейчас (non-blocking)
            if deadline and time.monotonic() >= deadline:
                return False
            time.sleep(0.005)

    def _read_line(self, keep_newline: bool):
        while "\n" not in self.buffer_read:
            if not self._fill_buffer():
                # EOF: вернуть что накопилось или nil
                if self.buffer_read:
                    result = self.buffer_read
                    self.buffer_read = ""
                    return result, None
                return None, None
        idx = self.buffer_read.index("\n")
        if keep_newline:
            result = self.buffer_read[: idx + 1]
        else:
            result = self.buffer_read[:idx]
        self.buffer_read = self.buffer_read[idx + 1 :]
        return result, None

    def _read_all(self):
        while self._fill_buffer():
            pass
        result = self.buffer_read
        self.buffer_read = ""
        return result, None

    def _read_n(self, n: int):
        while len(self.buffer_read) < n:
            if not self._fill_buffer():
                break
        if not self.buffer_read:
            return None, None
        result = self.buffer_read[:n]
        self.buffer_read = self.buffer_read[n:]
        return result, None

    # ------------------------------------------------------------------ write
    def write(self, *args):
        if self.closed:
            return None, "bad file descriptor"

        for arg in args:
            if isinstance(arg, (int, float)):
                arg = str(arg)
            if not isinstance(arg, str):
                return None, "bad argument (string expected)"

            if self.buffer_mode == "full":
                if self.buffer_size - len(self.buffer_write) < len(arg):
                    result, err = self.flush()
                    if result is None:
                        return None, err
                if len(arg) > self.buffer_size:
                    self.stream.write(arg)
                else:
                    self.buffer_write += arg

            elif self.buffer_mode == "line":
                last_nl = arg.rfind("\n")
                if last_nl != -1 or len(arg) > self.buffer_size:
                    result, err = self.flush()
                    if result is None:
                        return None, err
                if last_nl != -1:
                    self.stream.write(arg[: last_nl + 1])
                    arg = arg[last_nl + 1 :]
                if len(arg) > self.buffer_size:
                    self.stream.write(arg)
                else:
                    self.buffer_write += arg

            else:  # "no"
                self.stream.write(arg)

        return self

    # ------------------------------------------------------------------ setvbuf
    def setvbuf(self, mode: str | None = None, size: int | None = None):
        mode = mode or self.buffer_mode
        size = size or self.buffer_size
        assert mode in ("no", "full", "line"), \
            f"bad argument #1 (no, full or line expected, got {mode!r})"
        assert mode == "no" or isinstance(size, int), \
            f"bad argument #2 (number expected)"
        self.buffer_mode = mode
        self.buffer_size = size
        return self.buffer_mode, self.buffer_size

    # ------------------------------------------------------------------ timeout
    def get_timeout(self) -> float | None:
        return self.read_timeout

    def set_timeout(self, value: float | None):
        self.read_timeout = float(value) if value is not None else None


def new(mode: str, stream) -> Buffer:
    """Фабричная функция — аналог buffer.new() в Lua."""
    return Buffer(mode, stream)