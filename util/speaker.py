"""
util/speaker.py — синтез PC-speaker-style тонов + выбор аудио-бэкенда.

Ноль pip-зависимостей. Приоритет:
  pygame.mixer → winsound → aplay/paplay → afplay → тишина.

ВАЖНО:
  * pygame.mixer на Windows упорно держит стерео. Синтезируем mono,
    бэкенд сам делает upmix L=R в start().
  * ADSR-параметры OpenComputers — в МИЛЛИСЕКУНДАХ.
  * Частота дискретизации берётся у бэкенда.
"""
from __future__ import annotations
import logging
import math
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time

logger = logging.getLogger("lupi.speaker")

SAMPLE_RATE  = 22050
SAMPLE_WIDTH = 2
NUM_CHANNELS = 1

WAVE_TRIANGLE = 2
WAVE_SAWTOOTH = 3
WAVE_SQUARE   = 4
WAVE_NOISE    = 5
WAVE_SINE     = 6


# ── Pump-hook ─────────────────────────────────────────────────────────────────

def _get_pump_hook():
    try:
        from core import computer as _c
    except Exception:
        return None
    return getattr(_c, "_pump_hook", None)


def _pump():
    hook = _get_pump_hook()
    if hook is None:
        return
    try:
        hook()
    except Exception:
        pass


def sleep_with_pump(seconds: float):
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        remain = deadline - time.monotonic()
        if remain <= 0:
            return
        _pump()
        time.sleep(min(0.01, remain))


# ── Синтез ────────────────────────────────────────────────────────────────────

def _sample(wave: int, phase: float) -> float:
    if wave == WAVE_SINE:
        return math.sin(2.0 * math.pi * phase)
    if wave == WAVE_SQUARE:
        return 1.0 if phase < 0.5 else -1.0
    if wave == WAVE_SAWTOOTH:
        return 2.0 * phase - 1.0
    if wave == WAVE_TRIANGLE:
        return 4.0 * abs(phase - 0.5) - 1.0
    if wave == WAVE_NOISE:
        return random.uniform(-1.0, 1.0)
    return 0.0


def _envelope_curve(ch: dict, n: int, sr: int):
    """ADSR в миллисекундах. None если огибающая плоская."""
    a_ms = float(ch.get("attack")  or 0.0)
    d_ms = float(ch.get("decay")   or 0.0)
    r_ms = float(ch.get("release") or 0.0)
    s_lv = float(ch.get("sustain", 1.0))

    if a_ms <= 0.0 and d_ms <= 0.0 and r_ms <= 0.0 and abs(s_lv - 1.0) < 1e-6:
        return None

    a = int(a_ms / 1000.0 * sr)
    d = int(d_ms / 1000.0 * sr)
    r = int(r_ms / 1000.0 * sr)
    if r > n:
        r = n

    env = [0.0] * n
    i = 0

    a_end = min(a, n)
    while i < a_end:
        env[i] = (i + 1) / a if a > 0 else 1.0
        i += 1

    d_end = min(a + d, n)
    while i < d_end:
        t = (i - a + 1) / d if d > 0 else 1.0
        env[i] = 1.0 - (1.0 - s_lv) * t
        i += 1

    s_end = max(i, n - r)
    while i < s_end:
        env[i] = s_lv
        i += 1

    rel_start = n - r
    while i < n:
        t = (i - rel_start + 1) / r if r > 0 else 1.0
        env[i] = s_lv * (1.0 - min(1.0, t))
        i += 1

    return env


def synthesize(channels, total_volume: float, duration_ms: float,
               sample_rate: int = SAMPLE_RATE) -> bytes:
    sr = int(sample_rate) or SAMPLE_RATE
    n = int(sr * max(0.0, duration_ms) / 1000.0)
    if n <= 0:
        return b""

    mix = [0.0] * n
    any_voice = False

    for ch in channels:
        freq = float(ch.get("frequency") or 0.0)
        vol  = float(ch.get("volume")    or 0.0)
        wave = int(ch.get("wave")        or WAVE_SINE)
        if freq <= 0.0 or vol <= 0.0:
            continue

        any_voice = True
        env = _envelope_curve(ch, n, sr)
        scale = vol * total_volume
        phase = 0.0
        dp = freq / sr

        for i in range(n):
            s = _sample(wave, phase)
            if env is not None:
                s *= env[i]
            mix[i] += s * scale
            phase += dp
            if phase >= 1.0:
                phase -= int(phase)

    if not any_voice:
        return b""

    out = bytearray(n * 2)
    for i, s in enumerate(mix):
        if s >  1.0: s =  1.0
        elif s < -1.0: s = -1.0
        struct.pack_into("<h", out, i * 2, int(s * 32767))
    return bytes(out)


def _upmix_mono(pcm: bytes, num_ch: int) -> bytes:
    """
    mono s16le → num_ch s16le, дублируя каждый сэмпл во все каналы.

    Раскладка для stereo (num_ch=2):
        L_lo L_hi R_lo R_hi  L_lo L_hi R_lo R_hi  ...
    То есть байты mono идут в позиции 0,1,4,5,8,9,... для L и
    в позиции 2,3,6,7,10,11,... для R.
    """
    if num_ch <= 1:
        return pcm
    n = len(pcm) // 2
    if n <= 0:
        return pcm
    out = bytearray(n * num_ch * 2)
    # out[ch*2::num_ch*2]  — "lo byte" позиции канала ch
    # out[ch*2+1::num_ch*2] — "hi byte" позиции канала ch
    # pcm[0::2], pcm[1::2] — lo/hi байты mono-сэмплов
    for ch in range(num_ch):
        out[ch * 2     :: num_ch * 2] = pcm[0::2]
        out[ch * 2 + 1 :: num_ch * 2] = pcm[1::2]
    return bytes(out)


def _wrap_wav(pcm: bytes, num_ch: int, sample_rate: int) -> bytes:
    n = len(pcm)
    byte_rate   = sample_rate * num_ch * SAMPLE_WIDTH
    block_align = num_ch * SAMPLE_WIDTH
    header  = b"RIFF" + struct.pack("<I", 36 + n) + b"WAVE"
    header += b"fmt " + struct.pack(
        "<IHHIIHH",
        16, 1, num_ch, sample_rate,
        byte_rate, block_align, SAMPLE_WIDTH * 8,
    )
    header += b"data" + struct.pack("<I", n)
    return header + pcm


# ── Бэкенды ───────────────────────────────────────────────────────────────────

class _Backend:
    name = "none"
    sample_rate = SAMPLE_RATE
    channels    = 1

    def start(self, pcm_mono: bytes): pass
    def is_playing(self) -> bool: return False
    def stop(self): pass


class _PygameBackend(_Backend):
    name = "pygame.mixer"

    def __init__(self, pygame_mod):
        self._pg = pygame_mod

        # pygame.init() уже поднимает mixer (обычно 44100/stereo).
        # НЕ пытаемся форсить mono — на Windows это не работает и даёт
        # рассинхрон форматов.
        try:
            actual = pygame_mod.mixer.get_init()
        except Exception:
            actual = None

        if actual is None:
            try:
                pygame_mod.mixer.init(
                    frequency=SAMPLE_RATE, size=-16,
                    channels=NUM_CHANNELS, buffer=512,
                )
            except Exception as e:
                raise ImportError(f"pygame.mixer init failed: {e}")
            actual = pygame_mod.mixer.get_init()

        if actual is None:
            raise ImportError("pygame.mixer.get_init() returned None")

        self.sample_rate = int(actual[0])
        self.channels    = int(actual[2])
        logger.debug("pygame.mixer: rate=%d, size=%d, ch=%d",
                     actual[0], actual[1], actual[2])

        self._sound = None
        self._channel = None

    def start(self, pcm_mono: bytes):
        pcm = pcm_mono
        if self.channels > 1:
            pcm = _upmix_mono(pcm, self.channels)
        self._sound = self._pg.mixer.Sound(buffer=pcm)
        self._channel = self._sound.play()
        if self._channel is None:
            logger.warning("pygame.mixer: Channel.play() -> None (mixer busy)")

    def is_playing(self):
        return self._channel is not None and self._channel.get_busy()

    def stop(self):
        if self._channel is not None:
            try: self._channel.stop()
            except Exception: pass
        self._channel = None
        self._sound = None


class _WinsoundBackend(_Backend):
    name = "winsound"

    def __init__(self):
        if os.name != "nt":
            raise ImportError("not Windows")
        import winsound  # type: ignore
        self._ws = winsound
        self.sample_rate = SAMPLE_RATE
        self.channels = 1
        self._done = threading.Event()
        self._done.set()
        self._buf = None

    def start(self, pcm_mono: bytes):
        self._buf = _wrap_wav(pcm_mono, 1, self.sample_rate)
        self._done.clear()
        buf = self._buf

        def _play():
            try:
                self._ws.PlaySound(buf, self._ws.SND_MEMORY | self._ws.SND_NODEFAULT)
            except Exception:
                pass
            finally:
                self._done.set()

        threading.Thread(target=_play, daemon=True).start()

    def is_playing(self):
        return not self._done.is_set()

    def stop(self):
        try:
            self._ws.PlaySound(None, self._ws.SND_PURGE)
        except Exception:
            pass
        self._done.set()


class _AplayBackend(_Backend):
    name = "aplay"

    def __init__(self):
        if not shutil.which("aplay"):
            raise ImportError("aplay not found")
        self.sample_rate = SAMPLE_RATE
        self.channels = 1
        self._proc = None

    def start(self, pcm_mono: bytes):
        self._proc = subprocess.Popen(
            ["aplay", "-q", "-t", "raw",
             "-f", "S16_LE", "-r", str(self.sample_rate),
             "-c", str(self.channels), "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self._proc.stdin.write(pcm_mono)
            self._proc.stdin.close()
        except Exception:
            pass

    def is_playing(self):
        return self._proc is not None and self._proc.poll() is None

    def stop(self):
        if self._proc is not None:
            try: self._proc.terminate()
            except Exception: pass
            self._proc = None


class _PaplayBackend(_Backend):
    name = "paplay"

    def __init__(self):
        if not shutil.which("paplay"):
            raise ImportError("paplay not found")
        self.sample_rate = SAMPLE_RATE
        self.channels = 1
        self._proc = None

    def start(self, pcm_mono: bytes):
        self._proc = subprocess.Popen(
            ["paplay", "--raw", "--format=s16le",
             f"--rate={self.sample_rate}",
             f"--channels={self.channels}"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self._proc.stdin.write(pcm_mono)
            self._proc.stdin.close()
        except Exception:
            pass

    def is_playing(self):
        return self._proc is not None and self._proc.poll() is None

    def stop(self):
        if self._proc is not None:
            try: self._proc.terminate()
            except Exception: pass
            self._proc = None


class _AfplayBackend(_Backend):
    name = "afplay"

    def __init__(self):
        if sys.platform != "darwin":
            raise ImportError("not macOS")
        if not shutil.which("afplay"):
            raise ImportError("afplay not found")
        self.sample_rate = SAMPLE_RATE
        self.channels = 1
        self._proc = None
        self._tmp  = None

    def start(self, pcm_mono: bytes):
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="lupi_")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(_wrap_wav(pcm_mono, 1, self.sample_rate))
        except Exception:
            try: os.unlink(path)
            except Exception: pass
            raise
        self._tmp = path
        self._proc = subprocess.Popen(
            ["afplay", path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def is_playing(self):
        return self._proc is not None and self._proc.poll() is None

    def stop(self):
        if self._proc is not None:
            try: self._proc.terminate()
            except Exception: pass
            self._proc = None
        if self._tmp is not None:
            try: os.unlink(self._tmp)
            except Exception: pass
            self._tmp = None


# ── Выбор бэкенда ─────────────────────────────────────────────────────────────

def _try_pygame():
    try:
        import pygame  # type: ignore
    except Exception:
        return None
    try:
        return _PygameBackend(pygame)
    except ImportError as e:
        logger.debug("pygame.mixer unavailable: %s", e)
        return None


def _try(factory):
    try:
        return factory()
    except ImportError:
        return None
    except Exception as e:
        logger.debug("backend %s failed: %s", factory.__name__, e)
        return None


def _pick_backend():
    b = _try_pygame()
    if b is not None:
        logger.info("Speaker backend: %s (rate=%d, ch=%d)",
                    b.name, b.sample_rate, b.channels)
        return b

    for factory in (_WinsoundBackend, _AplayBackend, _PaplayBackend, _AfplayBackend):
        b = _try(factory)
        if b is not None:
            logger.info("Speaker backend: %s", b.name)
            return b

    logger.info("No speaker backend available — sound will be silent")
    return None


# ── Фасад ─────────────────────────────────────────────────────────────────────

class Speaker:
    def __init__(self):
        self._backend = _pick_backend()

    @property
    def available(self) -> bool:
        return self._backend is not None

    @property
    def name(self) -> str:
        return self._backend.name if self._backend else "none"

    @property
    def sample_rate(self) -> int:
        if self._backend is None:
            return SAMPLE_RATE
        return getattr(self._backend, "sample_rate", SAMPLE_RATE)

    def play(self, pcm_mono: bytes, block: bool = True):
        if self._backend is None or not pcm_mono:
            if block and pcm_mono:
                dur = len(pcm_mono) / float(self.sample_rate * SAMPLE_WIDTH)
                sleep_with_pump(dur)
            return

        try:
            self._backend.start(pcm_mono)
        except Exception as e:
            logger.error("Speaker start failed: %s", e)
            return

        if not block:
            return

        dur = len(pcm_mono) / float(self.sample_rate * SAMPLE_WIDTH)
        deadline = time.monotonic() + dur + 1.0
        while self._backend.is_playing():
            _pump()
            if time.monotonic() > deadline:
                break
            time.sleep(0.005)

    def stop(self):
        if self._backend is not None:
            try: self._backend.stop()
            except Exception: pass


_speaker: Speaker | None = None


def get_speaker() -> Speaker:
    global _speaker
    if _speaker is None:
        _speaker = Speaker()
    return _speaker