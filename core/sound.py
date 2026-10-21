"""
sound.py — sound-компонент для LuPI (OpenComputers-совместимый).

Реализует полный API звуковой карты OpenComputers:
  - 8 каналов со своим состоянием (wave / frequency / volume / ADSR / FM / AM)
  - setWave / setFrequency / setVolume / setADSR / setFM / setAM
  - resetFM / resetAM / resetEnvelope / open / close
  - setTotalVolume / getTotalVolume
  - delay(ms) + process()/play() — накопили буфер, потом проиграли
  - modes.* константы (square / triangle / sine / noise / sawtooth)

Плюс утилита play_beep(freq, duration_ms) для computer.beep().

Воспроизведение идёт через util/speaker.py (ноль pip-зависимостей):
  pygame.mixer → winsound → aplay/paplay → afplay → тишина.
Отключить реальное воспроизведение: LUPI_SOUND=0.
"""
from __future__ import annotations
import os
import time
import logging
import threading

from core import component as component_mod

logger = logging.getLogger("lupi.sound")

# По умолчанию звук ВКЛЮЧЁН. Отключить: LUPI_SOUND=0 (или false/no/off).
_val = os.getenv("LUPI_SOUND")
_EMIT_ACTUAL_SOUND = (
    True if _val is None
    else _val.strip().lower() not in ("0", "false", "no", "off")
)

# Значения как в OpenComputers EnumSoundMode.
SOUND_MODES = {
    "mono":     0,
    "stereo":   1,
    "triangle": 2,
    "sawtooth": 3,
    "square":   4,
    "noise":    5,
    "sine":     6,
}

NUM_CHANNELS = 8

_speaker_singleton = None
_speaker_failed    = False
_instance          = None     # type: SoundComponent | None


# ── Утилиты ───────────────────────────────────────────────────────────────────

def _from_lua(s):
    if not isinstance(s, str):
        return s
    try:
        return s.encode('latin-1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _as_float(v, default=None):
    if _is_number(v):
        return float(v)
    return default


def _as_int(v, default=None):
    if _is_number(v):
        try:
            return int(v)
        except Exception:
            return default
    return default


def _get_speaker():
    global _speaker_singleton, _speaker_failed
    if _speaker_singleton is not None:
        return _speaker_singleton
    if _speaker_failed:
        return None
    try:
        from util import speaker as _spk
        _speaker_singleton = _spk.get_speaker()
        return _speaker_singleton
    except Exception as e:
        logger.warning("Speaker init failed: %s", e)
        _speaker_failed = True
        return None


def _sleep_with_pump(seconds: float):
    try:
        from util.speaker import sleep_with_pump
        sleep_with_pump(seconds)
    except Exception:
        if seconds > 0:
            time.sleep(seconds)


class _Channel:
    __slots__ = (
        "opened", "wave", "frequency", "volume",
        "attack", "decay", "sustain", "release",
        "fm_freq", "fm_mod", "am_freq", "am_mod",
    )

    def __init__(self):
        self.reset()

    def reset(self):
        self.opened = False
        self.wave = SOUND_MODES["sine"]
        self.frequency = 0.0
        self.volume = 0.0
        self.attack = 0.0
        self.decay = 0.0
        self.sustain = 1.0
        self.release = 0.0
        self.fm_freq = 0.0
        self.fm_mod = 0.0
        self.am_freq = 0.0
        self.am_mod = 0.0


# ── Компонент ─────────────────────────────────────────────────────────────────

class SoundComponent:
    def __init__(self):
        self._total_volume = 1.0
        self._delay_ms = 0.0
        self._channels = [_Channel() for _ in range(NUM_CHANNELS + 1)]
        self._lock = threading.Lock()

    # ── helpers ────────────────────────────────────────────────────────
    def _ch(self, *args):
        if not args:
            return self._channels[1], ()
        first = args[0]
        idx = _as_int(first)
        if idx is not None and 1 <= idx <= NUM_CHANNELS:
            return self._channels[idx], args[1:]
        return self._channels[1], args

    # ── volume / total volume ──────────────────────────────────────────
    def set_total_volume(self, *args):
        v = None
        for a in args:
            v = _as_float(a)
            if v is not None:
                break
        if v is not None:
            self._total_volume = max(0.0, min(1.0, v))
        return self._total_volume

    def get_total_volume(self, *_):
        return self._total_volume

    # ── channel control ────────────────────────────────────────────────
    def open(self, *args):
        ch, rest = self._ch(*args)
        ch.opened = True
        if rest:
            v = _as_float(rest[0])
            if v is not None:
                ch.volume = max(0.0, min(1.0, v))
        else:
            ch.volume = 1.0
        return True

    def close(self, *args):
        ch, _ = self._ch(*args)
        ch.reset()
        return True

    def set_wave(self, *args):
        ch, rest = self._ch(*args)
        if not rest:
            return False
        v = rest[0]
        if isinstance(v, str):
            name = _from_lua(v).lower()
            if name in SOUND_MODES:
                ch.wave = SOUND_MODES[name]
                return True
            return False
        iv = _as_int(v)
        if iv is not None:
            ch.wave = iv
            return True
        return False

    def set_frequency(self, *args):
        ch, rest = self._ch(*args)
        v = None
        for a in rest:
            v = _as_float(a)
            if v is not None:
                break
        if v is not None:
            ch.frequency = v
            return True
        return False

    def set_volume(self, *args):
        ch, rest = self._ch(*args)
        v = None
        for a in rest:
            v = _as_float(a)
            if v is not None:
                break
        if v is not None:
            ch.volume = max(0.0, min(1.0, v))
            return True
        return False

    def get_volume(self, *args):
        ch, _ = self._ch(*args)
        return ch.volume

    def set_pitch(self, *args):
        ch, rest = self._ch(*args)
        v = None
        for a in rest:
            v = _as_float(a)
            if v is not None:
                break
        if v is not None:
            ch.frequency = ch.frequency * v
            return True
        return False

    def get_pitch(self, *_):
        return 1.0

    def set_speed(self, *args):
        return True

    def get_speed(self, *_):
        return 1.0

    # ── envelope / modulation ──────────────────────────────────────────
    # OC-семантика: attack/decay/release в МИЛЛИСЕКУНДАХ, sustain — уровень 0..1.
    def set_adsr(self, *args):
        ch, rest = self._ch(*args)
        nums = [_as_float(a) for a in rest]
        nums = [n for n in nums if n is not None]

        # OC-спека: attack/decay/release — СЕКУНДЫ, sustain — уровень 0..1.
        # Внутри эмулятора attack/decay/release хранятся в МИЛЛИСЕКУНДАХ
        # (так их ожидает util/speaker._envelope_curve).
        if len(nums) >= 4:
            a, d, sus, r = nums[:4]
            ch.attack  = max(0.0, a) * 1000.0
            ch.decay   = max(0.0, d) * 1000.0
            ch.sustain = max(0.0, min(1.0, sus))
            ch.release = max(0.0, r) * 1000.0
        elif len(nums) == 3:
            a, d, sus = nums
            ch.attack  = max(0.0, a) * 1000.0
            ch.decay   = max(0.0, d) * 1000.0
            ch.sustain = max(0.0, min(1.0, sus))
        elif len(nums) == 2:
            a, d = nums
            ch.attack = max(0.0, a) * 1000.0
            ch.decay  = max(0.0, d) * 1000.0
        elif len(nums) == 1:
            ch.attack = max(0.0, nums[0]) * 1000.0
        return True
    
    def reset_envelope(self, *args):
        ch, _ = self._ch(*args)
        ch.attack = 0.0
        ch.decay = 0.0
        ch.sustain = 1.0
        ch.release = 0.0
        return True

    def set_fm(self, *args):
        ch, rest = self._ch(*args)
        nums = [_as_float(a) for a in rest]
        nums = [n for n in nums if n is not None]
        if nums:
            ch.fm_freq = nums[0]
        if len(nums) > 1:
            ch.fm_mod = nums[1]
        return True

    def reset_fm(self, *args):
        ch, _ = self._ch(*args)
        ch.fm_freq = 0.0
        ch.fm_mod = 0.0
        return True

    def set_am(self, *args):
        ch, rest = self._ch(*args)
        nums = [_as_float(a) for a in rest]
        nums = [n for n in nums if n is not None]
        if nums:
            ch.am_freq = nums[0]
        if len(nums) > 1:
            ch.am_mod = nums[1]
        return True

    def reset_am(self, *args):
        ch, _ = self._ch(*args)
        ch.am_freq = 0.0
        ch.am_mod = 0.0
        return True

    # ── delay / process ────────────────────────────────────────────────
    def delay(self, *args):
        v = None
        for a in args:
            v = _as_float(a)
            if v is not None:
                break
        if v is not None and v >= 0:
            self._delay_ms = v
        return True

    def _active_freq(self):
        best = None
        best_vol = 0.0
        for ch in self._channels[1:]:
            if not ch.opened:
                continue
            if ch.volume <= 0 or ch.frequency <= 0:
                continue
            if ch.volume > best_vol:
                best_vol = ch.volume
                best = ch.frequency
        return best

    def _collect_active(self):
        out = []
        for ch in self._channels[1:]:
            if not ch.opened:
                continue
            if ch.volume <= 0 or ch.frequency <= 0:
                continue
            out.append({
                "wave":      ch.wave,
                "frequency": ch.frequency,
                "volume":    ch.volume,
                "attack":    ch.attack,
                "decay":     ch.decay,
                "sustain":   ch.sustain,
                "release":   ch.release,
            })
        return out

    def process(self, *_):
        delay_ms = self._delay_ms
        self._delay_ms = 0.0

        if not _EMIT_ACTUAL_SOUND:
            logger.debug("sound.process: muted (LUPI_SOUND)")
            if delay_ms > 0:
                _sleep_with_pump(delay_ms / 1000.0)
            return True

        active = self._collect_active()
        if not active:
            logger.debug("sound.process: no active channels (delay=%sms)", delay_ms)
            if delay_ms > 0:
                _sleep_with_pump(delay_ms / 1000.0)
            return True

        duration_ms = delay_ms if delay_ms > 0 else 200.0
        logger.debug("sound.process: %d active channel(s), dur=%sms, total_vol=%.2f",
                     len(active), duration_ms, self._total_volume)

        sp = _get_speaker()
        logger.debug("sound.process: speaker available=%s name=%s rate=%s",
                     bool(sp and sp.available),
                     sp.name if sp else "none",
                     sp.sample_rate if sp else "-")

        if sp is not None and sp.available:
            try:
                from util import speaker as _spk
                pcm = _spk.synthesize(active, self._total_volume, duration_ms,
                                      sample_rate=sp.sample_rate)
                logger.debug("sound.process: synthesized %d bytes PCM", len(pcm))
                if pcm:
                    sp.play(pcm, block=True)
                    return True
                logger.debug("sound.process: PCM empty, skipping playback")
                if delay_ms > 0:
                    _sleep_with_pump(delay_ms / 1000.0)
                return True
            except Exception:
                logger.exception("sound.process: playback failed")

        # Раньше тут был fallback через computer.beep — теперь его нет,
        # чтобы избежать бесконечной рекурсии (computer.beep сам идёт
        # через sound.play_beep). Просто тихо ждём длительность.
        logger.debug("sound.process: no speaker — silent wait %sms", delay_ms)
        if delay_ms > 0:
            _sleep_with_pump(delay_ms / 1000.0)
        return True

    def play(self, *_):
        return self.process()

    def stop(self, *_):
        with self._lock:
            for ch in self._channels[1:]:
                ch.reset()
            self._delay_ms = 0.0
        sp = _get_speaker()
        if sp is not None:
            try: sp.stop()
            except Exception: pass
        return True

    # ── misc ───────────────────────────────────────────────────────────
    def get_available_sounds(self, *_):
        return {}

    # ── proxy ──────────────────────────────────────────────────────────
    def as_proxy(self) -> dict:
        return {
            "modes":              dict(SOUND_MODES),

            "open":               self.open,
            "close":              self.close,
            "setWave":            self.set_wave,
            "setFrequency":       self.set_frequency,
            "setVolume":          self.set_volume,
            "getVolume":          self.get_volume,
            "setPitch":           self.set_pitch,
            "getPitch":           self.get_pitch,
            "setSpeed":           self.set_speed,
            "getSpeed":           self.get_speed,
            "setTotalVolume":     self.set_total_volume,
            "getTotalVolume":     self.get_total_volume,
            "setADSR":            self.set_adsr,
            "resetEnvelope":      self.reset_envelope,
            "setFM":              self.set_fm,
            "resetFM":            self.reset_fm,
            "setAM":              self.set_am,
            "resetAM":            self.reset_am,
            "delay":              self.delay,
            "process":            self.process,
            "play":               self.play,
            "stop":               self.stop,
            "getAvailableSounds": self.get_available_sounds,
        }


def register(uuid: str | None = None) -> str:
    global _instance
    comp = SoundComponent()
    _instance = comp
    addr = component_mod.register(uuid, "sound", comp.as_proxy())
    logger.info("Sound component registered: %s", addr)
    return addr


# ── Public: для computer.beep() ───────────────────────────────────────────────

def play_beep(freq: float = 1000.0, duration_ms: float = 200.0) -> bool:
    """
    Синтезирует квадратный тон и играет через спикер.
    Возвращает True, если звук реально ушёл в бэкенд.
    """
    if not _EMIT_ACTUAL_SOUND:
        return False

    sp = _get_speaker()
    if sp is None or not sp.available:
        return False

    try:
        from util import speaker as _spk
        ch = {
            "wave":      SOUND_MODES["square"],
            "frequency": float(freq),
            "volume":    1.0,
            "attack":    0.0,
            "decay":     0.0,
            "sustain":   1.0,
            "release":   0.0,
        }
        pcm = _spk.synthesize([ch], 1.0, float(duration_ms),
                              sample_rate=sp.sample_rate)
        if not pcm:
            return False
        sp.play(pcm, block=True)
        return True
    except Exception:
        logger.exception("play_beep failed")
        return False