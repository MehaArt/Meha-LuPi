"""
core/clipboard.py — компонент clipboard.

Даёт Lua-скриптам доступ к СИСТЕМНОМУ буферу обмена:
  * get()        → строка (или "" если пусто)
  * set(value)   → True, пишет в системный буфер

ВАЖНО: set() НЕ broadcast'ит сигнал 'clipboard'. В реальной OpenComputers
этот сигнал приходит только от ВНЕШНЕГО источника (пользователь
скопировал что-то в Minecraft — ОС уведомляет компьютер). Если broadcast'ить
его из set(), приложения (MineCode IDE и т.п.) получают лишний сигнал
и падают в своих обработчиках.
"""
from __future__ import annotations
import logging

from core import component as component_mod

logger = logging.getLogger("lupi.clipboard")


def _from_lua(s):
    if not isinstance(s, str):
        return s
    try:
        return s.encode('latin-1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def _to_lua(s):
    if not isinstance(s, str):
        return s
    try:
        return s.encode('utf-8').decode('latin-1')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


class ClipboardComponent:

    def __init__(self):
        # Резервный кэш — если системный буфер недоступен
        self._cached = ""

    def get(self, *_):
        try:
            from util import clipboard as _cb
            value = _cb.get()
        except Exception as e:
            logger.debug("clipboard.get: %s", e)
            value = None

        if value is None:
            return _to_lua(self._cached)

        self._cached = value
        return _to_lua(value)

    def set(self, value="", *_):
        value = _from_lua(value)
        if not isinstance(value, str):
            value = str(value)

        self._cached = value

        try:
            from util import clipboard as _cb
            ok = _cb.set_text(value)
            if not ok:
                logger.debug("clipboard.set: system backend refused")
        except Exception as e:
            logger.debug("clipboard.set: %s", e)

        # Намеренно НЕ push_signal('clipboard', ...).
        # См. docstring в начале файла.

        return True

    def as_proxy(self) -> dict:
        return {
            "get": self.get,
            "set": self.set,
        }


def register(uuid: str | None = None) -> str:
    comp = ClipboardComponent()
    addr = component_mod.register(uuid, "clipboard", comp.as_proxy())
    logger.info("Clipboard component registered: %s", addr)
    return addr