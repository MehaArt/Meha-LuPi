"""
computer.py — порт src/lua/core/computer.lua + src/c/main.c (event loop)

Управление сигналами / событиями, uptime, beep, память и т.д.

Модель ОЗУ:
  * total    — виртуальный объём «плашки» (--ram);
  * raw_heap — реальный Lua-heap (collectgarbage("count"));
  * baseline — «вес» нашего рантайма, снимается сразу после init;
  * used = max(0, raw_heap - baseline);
  * free = max(0, total - used).

Enforcement делается в init.py через debug.sethook.
"""
from __future__ import annotations
import os
import sys
import time
import queue
import select
import threading
import logging
from typing import Any, Callable

logger = logging.getLogger("lupi.computer")

# ── Тайминги ожидания ────────────────────────────────────────────────────────
_WAIT_POLL_INTERVAL = 0.01
_NONBLOCK_SLEEP = 0.002
_PUMP_MAX_HZ = 60.0
_LOG_RATE_HZ = 500.0
_DRAIN_SAFETY_PUMP_INTERVAL = 0.5   # сек: не даём окну залипнуть при долгом drain
_drain_last_pump = time.monotonic()

# ── Внутреннее состояние ──────────────────────────────────────────────────────
_start_time = time.monotonic()
_signal_queue: queue.Queue = queue.Queue()
_address: str = ""
_tmp_address: str = ""
_shutdown_hooks: list = []
_pump_hook = None
_pump_block_hook = None
_shutting_down = False
_debug_mode = False

_pump_last_call = 0.0
_pull_counter = 0
_pull_counter_ts = 0.0

# ── Модель ОЗУ ────────────────────────────────────────────────────────────────
_DEFAULT_MEMORY_BYTES = 64 * 1024 * 1024
_total_memory_bytes = _DEFAULT_MEMORY_BYTES
_used_memory_getter: Callable[[], int] | None = None
_memory_baseline_bytes = 0


def set_memory_model(total_bytes: int):
    """Задать полный объём ОЗУ (в байтах). Меньше 4 КБ — обрезается."""
    global _total_memory_bytes
    try:
        n = int(total_bytes)
    except (TypeError, ValueError):
        return
    if n < 4096:
        n = 4096
    _total_memory_bytes = n
    logger.info("Memory model: total = %d bytes (%.2f KiB)", n, n / 1024.0)


def set_memory_usage_getter(fn: Callable[[], int] | None):
    """Callback, возвращающий РЕАЛЬНЫЙ Lua-heap в байтах (или None)."""
    global _used_memory_getter
    _used_memory_getter = fn


def set_memory_baseline(bytes_: int):
    """Зафиксировать «вес» рантайма, не считающийся памятью ОС."""
    global _memory_baseline_bytes
    try:
        n = int(bytes_)
    except (TypeError, ValueError):
        return
    _memory_baseline_bytes = max(0, n)
    logger.info("Memory baseline: %d bytes (%.2f KiB)",
                _memory_baseline_bytes, _memory_baseline_bytes / 1024.0)


def memory_baseline() -> int:
    return _memory_baseline_bytes


def _raw_used_memory() -> int:
    if _used_memory_getter is None:
        return 0
    try:
        v = _used_memory_getter()
        if v is None:
            return 0
        v = int(v)
        return v if v > 0 else 0
    except Exception:
        logger.exception("memory usage getter failed")
        return 0


def used_memory() -> int:
    raw = _raw_used_memory()
    used = raw - _memory_baseline_bytes
    return used if used > 0 else 0


def raw_memory_used() -> int:
    return _raw_used_memory()


def total_memory() -> int:
    return _total_memory_bytes


def free_memory() -> int:
    used = used_memory()
    free = _total_memory_bytes - used
    return free if free > 0 else 0


def memory_stats() -> tuple[int, int, float]:
    used = used_memory()
    total = _total_memory_bytes
    ratio = (used / total) if total > 0 else 0.0
    return used, total, ratio


def set_debug_mode(v: bool):
    global _debug_mode
    _debug_mode = bool(v)


def set_pump_hook(fn):
    global _pump_hook
    _pump_hook = fn


def set_pump_block_hook(fn):
    global _pump_block_hook
    _pump_block_hook = fn


def _maybe_pump():
    global _pump_last_call
    if _pump_hook is None:
        return
    now = time.monotonic()
    if now - _pump_last_call < 1.0 / _PUMP_MAX_HZ:
        return
    _pump_last_call = now
    try:
        _pump_hook()
    except Exception:
        logger.exception("pump hook failed")


def _pull_rate_check():
    global _pull_counter, _pull_counter_ts
    _pull_counter += 1
    if not _debug_mode:
        return
    now = time.monotonic()
    if _pull_counter_ts == 0.0:
        _pull_counter_ts = now
        return
    dt = now - _pull_counter_ts
    if dt < 1.0:
        return
    rate = _pull_counter / dt
    if rate > _LOG_RATE_HZ:
        logger.warning("pull_signal rate: %.0f Hz (Lua tight-loop?)", rate)
    _pull_counter = 0
    _pull_counter_ts = now

def pull_signal(timeout: float | None = None):
    """
    Возвращает один сигнал или None.

    Оптимизация:
      * Fast-path — если сигнал уже в очереди, отдаём его МГНОВЕННО,
        не запуская pump (это ускоряет Ctrl+V paste в 2–3 раза —
        между символами не происходит перерисовки).
      * Safety-pump — если дренаж длится >0.5с, всё равно запускаем
        pump, чтобы окно не залипло и не пропустило клики.
    """
    global _drain_last_pump
    _pull_rate_check()

    if timeout is not None:
        try:
            if timeout == float('inf') or timeout > 1e9:
                timeout = None
        except (TypeError, ValueError):
            timeout = None

    # ── Fast path: сигнал уже в очереди ────────────────────────────
    try:
        item = _signal_queue.get_nowait()
        if _pump_hook is not None:
            now = time.monotonic()
            if now - _drain_last_pump >= _DRAIN_SAFETY_PUMP_INTERVAL:
                _drain_last_pump = now
                try:
                    _pump_hook()
                except Exception:
                    logger.exception("drain safety pump failed")
        logger.debug("pull_signal (fast): %s", item)
        return item
    except queue.Empty:
        pass

    # ── Slow path ──────────────────────────────────────────────────
    if timeout is not None and timeout <= 0:
        _maybe_pump()
        try:
            item = _signal_queue.get_nowait()
            logger.debug("pull_signal: %s", item)
            return item
        except queue.Empty:
            time.sleep(_NONBLOCK_SLEEP)
            return None

    if timeout is None:
        while True:
            _maybe_pump()
            try:
                item = _signal_queue.get(
                    block=True, timeout=_WAIT_POLL_INTERVAL)
                logger.debug("pull_signal: %s", item)
                return item
            except queue.Empty:
                continue

    deadline = time.monotonic() + timeout
    while True:
        _maybe_pump()
        remain = deadline - time.monotonic()
        if remain <= 0:
            return None
        try:
            item = _signal_queue.get(
                block=True,
                timeout=min(_WAIT_POLL_INTERVAL, remain),
            )
            logger.debug("pull_signal: %s", item)
            return item
        except queue.Empty:
            continue

# ── Keymap ────────────────────────────────────────────────────────────────────
_KEYMAP: dict[int, int] = {
    48: 0x2,  49: 0x3,  50: 0x4,  51: 0x5,  52: 0x6,
    53: 0x7,  54: 0x8,  55: 0x9,  56: 0xA,  57: 0xB,
    13: 28,   127: 14,  9: 15,
}
_CTRL_MAP = {
    0x01: 0x1E, 0x02: 0x30, 0x04: 0x2E, 0x05: 0x20, 0x06: 0x12,
    0x07: 0x21, 0x08: 0x22, 0x09: 0x23, 0x0A: 0x17, 0x0B: 0x24,
    0x0C: 0x25, 0x0D: 0x26, 0x0E: 0x32, 0x0F: 0x31, 0x11: 0x18,
    0x12: 0x19, 0x13: 0x10, 0x14: 0x13, 0x15: 0x1F, 0x16: 0x14,
    0x17: 0x16, 0x18: 0x2F, 0x19: 0x11, 0x1A: 0x2D, 0x1B: 0x15,
    0x1C: 0x2C,
}
for _base in (0x40, 0x60):
    for _k, _v in _CTRL_MAP.items():
        _KEYMAP[_base + _k] = _v

_ASCII_TR: dict[int, int] = {127: 8}


def _transform_key(signal_name: str, addr: str, ascii_code: int, key: int, user):
    if key != -1:
        return signal_name, addr, ascii_code, key, user
    new_ascii = _ASCII_TR.get(ascii_code, ascii_code)
    new_key = _KEYMAP.get(ascii_code, key)
    return signal_name, addr, new_ascii, new_key, user


_SIGNAL_TRANSFORMERS: dict[str, Any] = {
    "key_down": _transform_key,
    "key_up": _transform_key,
}


def _transform(signal: str, *args):
    fn = _SIGNAL_TRANSFORMERS.get(signal)
    if fn:
        return fn(signal, *args)
    return (signal, *args)


# ── Public API ────────────────────────────────────────────────────────────────

def prepare():
    pass


def get_address() -> str:
    return _address


def uptime() -> float:
    return time.monotonic() - _start_time


def real_time() -> int:
    return int(time.time() * 1000)


def energy() -> float:
    return 1.0


def max_energy() -> float:
    return 1.0


def push_signal(signal: str, *args):
    transformed = _transform(signal, *args)
    _signal_queue.put(transformed)
    logger.debug("push_signal: %s", transformed)


def beep(freq: int = 1000, duration_ms: float = 200):
    try:
        freq = float(freq)
        duration_ms = float(duration_ms)
    except (TypeError, ValueError):
        return

    freq = max(37.0, min(20000.0, freq))
    duration_ms = max(1.0, min(10000.0, duration_ms))

    try:
        from core import sound as _sound
        if _sound.play_beep(freq, duration_ms):
            return
    except Exception:
        logger.exception("computer.beep: sound.play_beep failed")

    if os.name == "nt":
        try:
            import winsound
            winsound.Beep(int(freq), int(duration_ms))
            return
        except Exception as e:
            logger.debug("winsound.Beep failed: %s", e)
            return

    try:
        import fcntl
        KIOCSOUND = 0x4B2F
        period = int(1193180 / freq) if freq > 0 else 0
        fd = os.open("/dev/tty0", os.O_WRONLY)
        try:
            fcntl.ioctl(fd, KIOCSOUND, period)
            time.sleep(duration_ms / 1000.0)
            fcntl.ioctl(fd, KIOCSOUND, 0)
        finally:
            os.close(fd)
        return
    except Exception as e:
        logger.debug("PC speaker ioctl failed: %s", e)

    logger.debug("beep: no sound backend available")


def tmp_address() -> str:
    return _tmp_address


def _do_reboot():
    try:
        logging.shutdown()
    except Exception:
        pass
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass

    argv = list(sys.argv) if sys.argv else ['init.py']
    try:
        argv[0] = os.path.abspath(argv[0])
    except Exception:
        pass
    exe = sys.executable or 'python'

    if os.name == 'nt':
        import subprocess
        try:
            if _debug_mode:
                flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
            else:
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            subprocess.Popen([exe] + argv, creationflags=flags, close_fds=True)
        except Exception as e:
            logger.error("Reboot Popen failed: %s", e)
    else:
        try:
            os.execv(exe, [exe] + argv)
        except Exception as e:
            logger.error("Reboot execv failed: %s", e)
            try:
                import subprocess
                subprocess.Popen([exe] + argv)
            except Exception as e2:
                logger.error("Reboot Popen fallback failed: %s", e2)

    os._exit(0)


def shutdown(*args):
    global _shutting_down

    reboot = False
    for a in args:
        if isinstance(a, bool):
            reboot = a
            break
        if isinstance(a, int) and not isinstance(a, bool):
            try:
                reboot = bool(a)
                break
            except Exception:
                pass

    if _shutting_down:
        if reboot:
            _do_reboot()
        os._exit(0)
    _shutting_down = True

    logger.info("Running shutdown hooks (%d), reboot=%s",
                len(_shutdown_hooks), reboot)
    for hook in _shutdown_hooks:
        try:
            hook()
        except Exception as e:
            logger.error("Shutdown hook error: %s", e)

    try:
        logging.shutdown()
    except Exception:
        pass
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass

    if reboot:
        _do_reboot()

    os._exit(0)


def add_shutdown_hook(fn):
    _shutdown_hooks.append(fn)


def init(address: str, tmp_addr: str):
    global _address, _tmp_address, _start_time
    _address = address
    _tmp_address = tmp_addr
    _start_time = time.monotonic()


class KeyboardReader:
    def __init__(self, keyboard_addr: str, screen_addr: str):
        self.keyboard_addr = keyboard_addr
        self.screen_addr = screen_addr
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        import tty
        import termios

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while not self._stop.is_set():
                r, _, _ = select.select([fd], [], [], 0.1)
                if r:
                    ch = os.read(fd, 1)
                    if ch:
                        code = ch[0]
                        push_signal("key_down", self.keyboard_addr, code, -1, 0)
                        push_signal("key_up", self.keyboard_addr, code, -1, 0)
        except Exception as e:
            logger.error("KeyboardReader error: %s", e)
        finally:
            termios.tcsetattr(fd, TCSAFLUSH, old)


# ─── Расширения, совместимые с OCEmu ─────────────────────────────────────────

def get_device_info() -> dict:
    """
    Минимальная замена computer.getDeviceInfo():
    для каждого компонента возвращаем его type в поле Class.
    """
    try:
        from core import component as component_mod
    except Exception:
        return {}
    out = {}
    for addr, entry in component_mod._components.items():
        out[addr] = {
            "Class":       entry.type,
            "Description": "",
            "Product":     entry.type,
            "Vendor":      "",
            "Capacity":    0,
        }
    return out
    
# ─── OCEmu-совместимые расширения computer API ───────────────────────────────

# Архитектуры, которые мы «эмулируем». Первая — дефолтная, getArchitecture()
# возвращает именно её. Порядок совпадает с OCEmu.
_ARCHITECTURES: tuple[str, ...] = (
    "Lua 5.4",
    "Lua 5.3",
    "Lua 5.2",
    "LuaJIT 2.0",
    "LuaJIT 2.1",
)

# Каталог «программ, доступных на установочных дисках» (OC spec).
# OC использует Map<String, String>: имя программы → метка диска.
# OCEmu ошибочно возвращает table.pack(pack(name, disk), ...) — это не по
# спеке; мы придерживаемся OC spec (map), т.к. именно это ожидает
# OpenOS/mineos.
_PROGRAMS: dict[str, str] = {
    "build":     "builder",
    "dig":       "dig",
    "base64":    "data",
    "deflate":   "data",
    "gpg":       "data",
    "inflate":   "data",
    "md5sum":    "data",
    "sha256sum": "data",
    "refuel":    "generator",
    "irc":       "irc",
    "maze":      "maze",
    "arp":       "network",
    "ifconfig":  "network",
    "ping":      "network",
    "route":     "network",
    "opl-flash": "openloader",
    "oppm":      "oppm",
}


def get_architectures() -> dict:
    """computer.getArchitectures() → {[1]="Lua 5.4", [2]=...}."""
    return {i + 1: name for i, name in enumerate(_ARCHITECTURES)}


def get_architecture() -> str:
    """computer.getArchitecture() → текущая архитектура."""
    return _ARCHITECTURES[0]


def set_architecture(name) -> tuple:
    """
    computer.setArchitecture(name) — переключить архитектуру.
    Возвращает:
      * False                    — если name совпадает с текущей (уже выбрана)
      * nil, "unknown architecture" — если name не из списка
    """
    if not isinstance(name, str):
        raise TypeError(
            f"bad argument #1 (string expected, got {type(name).__name__})")
    if name not in _ARCHITECTURES:
        return None, "unknown architecture"
    return False


def add_user(username) -> tuple:
    """
    computer.addUser(username) — в реальном OC добавляет игрока в машину.
    У нас нет игроков, поэтому — stub как в OCEmu.
    """
    if not isinstance(username, str):
        raise TypeError(
            f"bad argument #1 (string expected, got {type(username).__name__})")
    return None, "player must be online"


def remove_user(username) -> bool:
    """
    computer.removeUser(username) — stub как в OCEmu.
    """
    if not isinstance(username, str):
        raise TypeError(
            f"bad argument #1 (string expected, got {type(username).__name__})")
    return False


def get_program_locations() -> dict:
    """
    computer.getProgramLocations() → {program_name = install_disk_label}.

    Возвращает копию статической таблицы (безопасно от мутаций).
    """
    return dict(_PROGRAMS)