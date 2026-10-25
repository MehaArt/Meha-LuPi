"""
util/pcsounds.py — ambient PC noise: running hum + HDD access clicks.

Фоновый core/sounds/computer_running.ogg играется в цикле на тихой
громкости, а на чтение/запись файловой системы проигрывается случайный
hdd_access1..7.ogg.

Требует pygame.mixer. Если pygame нет, или --allow-pc-sounds передан,
а какой-то из 8 ogg-файлов отсутствует — init.py завершится с ошибкой
ДО любого запуска.

Каналы 6 и 7 резервируются под эти звуки; остальные (0..5 и т.д.)
остаются sound.py — фоновый гул не будет выбивать звук звуковой карты.
"""
from __future__ import annotations
import logging
import os
import random
import threading
import time

logger = logging.getLogger("lupi.pcsounds")

_SOUND_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "core", "sounds",
)

_SOUND_NAMES = (
    "computer_running.ogg",
    "hdd_access1.ogg", "hdd_access2.ogg", "hdd_access3.ogg",
    "hdd_access4.ogg", "hdd_access5.ogg", "hdd_access6.ogg",
    "hdd_access7.ogg",
)

RUNNING_VOLUME   = 0.20
HDD_VOLUME       = 0.60
HDD_THROTTLE_S   = 0.08
_RESERVED_RUNNING_CH = 6
_RESERVED_HDD_CH     = 7

_lock = threading.Lock()
_enabled = False
_player: "_Player | None" = None


def sounds_dir() -> str:
    return _SOUND_DIR


def validate_files() -> list[str]:
    """Возвращает список отсутствующих путей (пусто — всё на месте)."""
    return [
        os.path.join(_SOUND_DIR, n)
        for n in _SOUND_NAMES
        if not os.path.isfile(os.path.join(_SOUND_DIR, n))
    ]


class _Player:
    name = "pygame"

    def __init__(self):
        import pygame  # может бросить ImportError

        if not pygame.mixer.get_init():
            pygame.mixer.init()

        try:
            if pygame.mixer.get_num_channels() < 16:
                pygame.mixer.set_num_channels(16)
        except Exception:
            pass

        self._pg = pygame
        self._running_ch = pygame.mixer.Channel(_RESERVED_RUNNING_CH)
        self._hdd_ch     = pygame.mixer.Channel(_RESERVED_HDD_CH)

        self._running = pygame.mixer.Sound(
            os.path.join(_SOUND_DIR, "computer_running.ogg"))
        self._running.set_volume(RUNNING_VOLUME)

        self._hdd = []
        for i in range(1, 8):
            s = pygame.mixer.Sound(
                os.path.join(_SOUND_DIR, f"hdd_access{i}.ogg"))
            s.set_volume(HDD_VOLUME)
            self._hdd.append(s)

        self._last_hdd = 0.0
        self._started  = False

    def start(self):
        if self._started:
            return
        self._started = True
        try:
            self._running_ch.play(self._running, loops=-1)
            logger.info("PC sounds: background hum started")
        except Exception:
            logger.exception("PC sounds: running sound start failed")

    def play_hdd(self):
        now = time.monotonic()
        if now - self._last_hdd < HDD_THROTTLE_S:
            return
        self._last_hdd = now
        try:
            if self._hdd_ch.get_busy():
                return
            self._hdd_ch.play(random.choice(self._hdd))
        except Exception:
            pass

    def stop(self):
        try: self._running_ch.stop()
        except Exception: pass
        try: self._hdd_ch.stop()
        except Exception: pass


def start() -> bool:
    """
    Загрузить и запустить фоновые звуки.

    Вызывать только если --allow-pc-sounds задан и validate_files() вернул [].
    При проблемах с pygame бросает RuntimeError — init.py её ловит.
    """
    global _enabled, _player
    with _lock:
        if _player is not None:
            _enabled = True
            return True
        try:
            p = _Player()
        except ImportError:
            raise RuntimeError(
                "--allow-pc-sounds requires pygame (pip install pygame)")
        except Exception as e:
            raise RuntimeError(
                f"--allow-pc-sounds: pygame.mixer init failed: {e}")
        _player = p
        _enabled = True
        p.start()
        return True


def stop():
    global _enabled, _player
    with _lock:
        _enabled = False
        if _player is not None:
            try: _player.stop()
            except Exception: pass


def is_enabled() -> bool:
    return _enabled


def trigger_hdd():
    """Дешёвый callback из ФС. Ничего не делает, если выключено."""
    if not _enabled:
        return
    p = _player
    if p is None:
        return
    p.play_hdd()