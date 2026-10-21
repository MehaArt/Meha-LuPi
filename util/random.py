"""
util/random.py — порт src/lua/core/util/random.lua
"""
import uuid as _uuid


def uuid() -> str:
    """Генерирует UUID версии 4."""
    return str(_uuid.uuid4())
