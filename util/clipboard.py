"""
util/clipboard.py — доступ к системному буферу обмена.

Ноль обязательных pip-зависимостей. Порядок бэкендов подобран под каждую ОС:

  Windows:  win32clipboard → tkinter → PowerShell
  macOS:    pbcopy/pbpaste → tkinter
  Linux:    wl-clipboard → xclip → xsel → tkinter

Публичный API:
  get() -> str | None
  set_text(s: str) -> bool
"""
from __future__ import annotations
import logging
import os
import shutil
import subprocess
import sys
import threading
import time

logger = logging.getLogger("lupi.clipboard")

_lock = threading.Lock()
_get_impl = None
_set_impl = None
_init_tried = False


# ── Windows: win32clipboard (pywin32) ────────────────────────────────────────

def _open_clipboard_retry(win32clipboard, attempts: int = 8, delay: float = 0.02) -> bool:
    """
    OpenClipboard() может упасть с ERROR_ACCESS_DENIED, если другой процесс
    в этот момент держит буфер. Кратковременные ретраи решают проблему.
    """
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            win32clipboard.OpenClipboard()
            return True
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(delay)
    if last_err is not None:
        raise last_err
    return False


def _init_win_native():
    """Прямой доступ к Windows clipboard через pywin32."""
    try:
        import win32clipboard  # type: ignore
        import win32con  # type: ignore
    except Exception as e:
        # pywin32 может быть установлен, но с битыми DLL —
        # поэтому ловим Exception, а не ImportError.
        logger.debug("pywin32 clipboard unavailable: %s", e)
        return None, None

    def _get():
        try:
            if not _open_clipboard_retry(win32clipboard):
                return None
            try:
                if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                    return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT) or ""
                if win32clipboard.IsClipboardFormatAvailable(win32con.CF_TEXT):
                    raw = win32clipboard.GetClipboardData(win32con.CF_TEXT)
                    if isinstance(raw, bytes):
                        return raw.decode("utf-8", errors="replace")
                    return str(raw)
                return ""  # не текст — считаем пустым
            finally:
                win32clipboard.CloseClipboard()
        except Exception as e:
            logger.debug("win32clipboard.get failed: %s", e)
            return None

    def _set(s: str):
        try:
            if not _open_clipboard_retry(win32clipboard):
                return False
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, s)
            finally:
                win32clipboard.CloseClipboard()
            return True
        except Exception as e:
            logger.debug("win32clipboard.set failed: %s", e)
            return False

    return _get, _set


# ── tkinter (универсальный fallback) ─────────────────────────────────────────

def _init_tk():
    try:
        import tkinter as tk
    except Exception:
        return None, None

    try:
        root = tk.Tk()
        root.withdraw()
        root.update_idletasks()
    except Exception as e:
        logger.debug("tk clipboard init failed: %s", e)
        return None, None

    def _get():
        try:
            return root.clipboard_get()
        except tk.TclError:
            return ""  # пустой буфер — это не ошибка
        except Exception as e:
            logger.debug("tk clipboard_get error: %s", e)
            return None

    def _set(s: str):
        try:
            root.clipboard_clear()
            root.clipboard_append(s)
            root.update_idletasks()
            return True
        except Exception as e:
            logger.debug("tk clipboard_set error: %s", e)
            return False

    return _get, _set


# ── Linux: Wayland → X11 ─────────────────────────────────────────────────────

def _init_linux_cmd():
    # 1) Wayland
    if shutil.which("wl-paste") and shutil.which("wl-copy"):
        def _get():
            try:
                return subprocess.check_output(
                    ["wl-paste", "--no-newline"],
                    stderr=subprocess.DEVNULL, timeout=2,
                ).decode("utf-8", errors="replace")
            except subprocess.CalledProcessError:
                return ""  # пустой буфер
            except Exception:
                return None

        def _set(s):
            try:
                p = subprocess.Popen(["wl-copy"],
                                     stdin=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
                p.communicate(s.encode("utf-8"), timeout=2)
                return p.returncode == 0
            except Exception:
                return False
        return _get, _set

    # 2) X11: xclip
    if shutil.which("xclip"):
        def _get():
            try:
                return subprocess.check_output(
                    ["xclip", "-selection", "clipboard", "-o"],
                    stderr=subprocess.DEVNULL, timeout=2,
                ).decode("utf-8", errors="replace")
            except subprocess.CalledProcessError:
                return ""
            except Exception:
                return None

        def _set(s):
            try:
                p = subprocess.Popen(
                    ["xclip", "-selection", "clipboard", "-i"],
                    stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
                p.communicate(s.encode("utf-8"), timeout=2)
                return p.returncode == 0
            except Exception:
                return False
        return _get, _set

    # 3) X11: xsel
    if shutil.which("xsel"):
        def _get():
            try:
                return subprocess.check_output(
                    ["xsel", "--clipboard", "--output"],
                    stderr=subprocess.DEVNULL, timeout=2,
                ).decode("utf-8", errors="replace")
            except subprocess.CalledProcessError:
                return ""
            except Exception:
                return None

        def _set(s):
            try:
                p = subprocess.Popen(
                    ["xsel", "--clipboard", "--input"],
                    stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
                p.communicate(s.encode("utf-8"), timeout=2)
                return p.returncode == 0
            except Exception:
                return False
        return _get, _set

    return None, None


# ── macOS ────────────────────────────────────────────────────────────────────

def _init_mac_cmd():
    if not (shutil.which("pbpaste") and shutil.which("pbcopy")):
        return None, None

    def _get():
        try:
            return subprocess.check_output(
                ["pbpaste"], stderr=subprocess.DEVNULL, timeout=2,
            ).decode("utf-8", errors="replace")
        except Exception:
            return None

    def _set(s):
        try:
            p = subprocess.Popen(["pbcopy"],
                                 stdin=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL)
            p.communicate(s.encode("utf-8"), timeout=2)
            return p.returncode == 0
        except Exception:
            return False
    return _get, _set


# ── Windows: PowerShell (last resort) ────────────────────────────────────────

def _init_win_cmd():
    if not shutil.which("powershell"):
        return None, None

    def _get():
        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-NonInteractive",
                 "-Command", "Get-Clipboard -Raw"],
                stderr=subprocess.DEVNULL, timeout=5,
            )
            return out.decode("utf-8", errors="replace").rstrip("\r\n")
        except Exception:
            return None

    def _set(s):
        try:
            p = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive",
                 "-Command",
                 "$in = [Console]::In.ReadToEnd(); Set-Clipboard -Value $in"],
                stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
            p.communicate(s.encode("utf-8"), timeout=5)
            return p.returncode == 0
        except Exception:
            return False
    return _get, _set


# ── Выбор бэкенда ────────────────────────────────────────────────────────────

def _try(factory, label: str):
    try:
        g, s = factory()
    except Exception as e:
        logger.debug("clipboard backend %s crashed: %s", label, e)
        return None
    if g is None:
        return None
    logger.info("Clipboard backend: %s", label)
    return g, s


def _init():
    global _get_impl, _set_impl, _init_tried
    with _lock:
        if _init_tried:
            return
        _init_tried = True

        candidates: list[tuple] = []

        if os.name == "nt":
            candidates = [
                (_init_win_native, "win32clipboard"),
                (_init_tk,         "tkinter (Windows)"),
                (_init_win_cmd,    "PowerShell (last resort)"),
            ]
        elif sys.platform == "darwin":
            candidates = [
                (_init_mac_cmd, "pbcopy/pbpaste"),
                (_init_tk,      "tkinter (macOS)"),
            ]
        else:
            candidates = [
                (_init_linux_cmd, "wl-clipboard/xclip/xsel"),
                (_init_tk,        "tkinter (Unix)"),
            ]

        for factory, label in candidates:
            result = _try(factory, label)
            if result is not None:
                _get_impl, _set_impl = result
                return

        logger.info("Clipboard: no backend available — using memory only")
        _get_impl = None
        _set_impl = None


# ── Public API ───────────────────────────────────────────────────────────────

def get() -> str | None:
    _init()
    if _get_impl is None:
        return None
    try:
        return _get_impl()
    except Exception as e:
        logger.debug("clipboard.get failed: %s", e)
        return None


def set_text(s: str) -> bool:
    _init()
    if _set_impl is None:
        return False
    try:
        return bool(_set_impl(s))
    except Exception as e:
        logger.debug("clipboard.set failed: %s", e)
        return False