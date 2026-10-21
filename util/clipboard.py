"""
util/clipboard.py — доступ к системному буферу обмена.

Ноль pip-зависимостей. Пробует по очереди:
  1) tkinter (stdlib) — Windows / macOS / Linux с X11
  2) wl-paste/wl-copy    (Wayland)
  3) xclip / xsel        (X11)
  4) pbpaste / pbcopy    (macOS)
  5) PowerShell Get/Set-Clipboard (Windows, крайний случай)

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

logger = logging.getLogger("lupi.clipboard")

_lock = threading.Lock()
_get_impl = None
_set_impl = None
_init_tried = False


# ── Бэкенды ───────────────────────────────────────────────────────────────────

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
            return ""
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


def _init_linux_cmd():
    if shutil.which("wl-paste") and shutil.which("wl-copy"):
        def _get():
            try:
                return subprocess.check_output(
                    ["wl-paste", "--no-newline"],
                    stderr=subprocess.DEVNULL, timeout=2,
                ).decode("utf-8", errors="replace")
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

    if shutil.which("xclip"):
        def _get():
            try:
                return subprocess.check_output(
                    ["xclip", "-selection", "clipboard", "-o"],
                    stderr=subprocess.DEVNULL, timeout=2,
                ).decode("utf-8", errors="replace")
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

    if shutil.which("xsel"):
        def _get():
            try:
                return subprocess.check_output(
                    ["xsel", "--clipboard", "--output"],
                    stderr=subprocess.DEVNULL, timeout=2,
                ).decode("utf-8", errors="replace")
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


def _init_mac_cmd():
    if shutil.which("pbpaste") and shutil.which("pbcopy"):
        def _get():
            try:
                return subprocess.check_output(
                    ["pbpaste"],
                    stderr=subprocess.DEVNULL, timeout=2,
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
    return None, None


def _init_win_cmd():
    if not shutil.which("powershell"):
        return None, None

    def _get():
        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                stderr=subprocess.DEVNULL, timeout=5,
            )
            return out.decode("utf-8", errors="replace").rstrip("\r\n")
        except Exception:
            return None

    def _set(s):
        try:
            p = subprocess.Popen(
                ["powershell", "-NoProfile", "-Command",
                 "$in = [Console]::In.ReadToEnd(); Set-Clipboard -Value $in"],
                stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
            p.communicate(s.encode("utf-8"), timeout=5)
            return p.returncode == 0
        except Exception:
            return False
    return _get, _set


def _init():
    global _get_impl, _set_impl, _init_tried
    with _lock:
        if _init_tried:
            return
        _init_tried = True

        g, s = _init_tk()
        if g is not None:
            _get_impl, _set_impl = g, s
            logger.info("Clipboard backend: tkinter")
            return

        if sys.platform == "darwin":
            g, s = _init_mac_cmd()
        elif os.name == "nt":
            g, s = _init_win_cmd()
        else:
            g, s = _init_linux_cmd()

        if g is not None:
            _get_impl, _set_impl = g, s
            logger.info("Clipboard backend: system commands")
            return

        logger.info("Clipboard: no backend available — using memory only")
        _get_impl = None
        _set_impl = None


# ── Public API ────────────────────────────────────────────────────────────────

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