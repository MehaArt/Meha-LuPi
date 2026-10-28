"""
textgpu.py — GPU на базе ANSI-терминала (curses).

Поддерживает Ctrl+C / Ctrl+V / Ctrl+A (select all), F1..F12, Alt
(на Windows — через GetAsyncKeyState, на остальных — через ESC-префикс).

Цвета:
  * COLORS >= 256 → xterm-256 (точное приближение)
  * COLORS >= 16  → 16 ANSI-цветов (curses.COLOR_X и COLOR_X+8)
  * COLORS == 8   → 8 цветов + A_BOLD для ярких
"""
from __future__ import annotations
import os
import sys
import time
import curses
import ctypes
import threading
import logging
from util import color as color_mod
from core import component as component_module
from core import computer

logger = logging.getLogger("lupi.textgpu")


# ── Платформенные константы ───────────────────────────────────────────────────

_IS_WINDOWS = (os.name == 'nt')

_VK_LMENU = 0xA4
_VK_RMENU = 0xA5
_ALT_POLL_INTERVAL = 0.01


# ── Scan-коды (PC/AT set 1) ───────────────────────────────────────────────────

_MODIFIER_SCANS = frozenset((0x2A, 0x36, 0x1D, 0x9D, 0x38, 0xB8))

_LALT_SCAN = 0x38
_RALT_SCAN = 0xB8
_ESC_DELAY_MS = 50


# ── Lua <-> Python строковые конверсии ────────────────────────────────────────

def _best_effort_utf8(raw: bytes) -> str:
    out = []
    i = 0
    n = len(raw)
    while i < n:
        b = raw[i]
        if b < 0x80:
            out.append(chr(b)); i += 1
        elif 0xC0 <= b < 0xE0 and i + 1 < n and 0x80 <= raw[i+1] < 0xC0:
            out.append(chr(((b & 0x1F) << 6) | (raw[i+1] & 0x3F))); i += 2
        elif 0xE0 <= b < 0xF0 and i + 2 < n \
                and 0x80 <= raw[i+1] < 0xC0 and 0x80 <= raw[i+2] < 0xC0:
            out.append(chr(((b & 0x0F) << 12) | ((raw[i+1] & 0x3F) << 6)
                           | (raw[i+2] & 0x3F))); i += 3
        elif 0xF0 <= b < 0xF8 and i + 3 < n \
                and 0x80 <= raw[i+1] < 0xC0 and 0x80 <= raw[i+2] < 0xC0 \
                and 0x80 <= raw[i+3] < 0xC0:
            out.append(chr(((b & 0x07) << 18) | ((raw[i+1] & 0x3F) << 12)
                           | ((raw[i+2] & 0x3F) << 6)
                           | (raw[i+3] & 0x3F))); i += 4
        else:
            out.append(chr(b)); i += 1
    return ''.join(out)


def _from_lua(s):
    if isinstance(s, bytes):
        try:
            return s.decode('utf-8')
        except UnicodeDecodeError:
            return s.decode('latin-1')
    if not isinstance(s, str):
        return s
    if all(ord(c) < 128 for c in s):
        return s

    for _ in range(4):
        try:
            raw = s.encode('latin-1', errors='strict')
        except UnicodeEncodeError:
            break
        try:
            decoded = raw.decode('utf-8')
        except UnicodeDecodeError:
            break
        if decoded == s:
            break
        s = decoded
    return s


def _to_lua(s):
    if not isinstance(s, str):
        return s
    try:
        return s.encode('utf-8').decode('latin-1')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


# ── Fallback-таблица для несовместимых глифов ─────────────────────────────────

_UNICODE_FALLBACK: dict[int, str] = {
    0x00A0: ' ', 0x1680: ' ', 0x2000: ' ', 0x2001: ' ',
    0x2002: ' ', 0x2003: ' ', 0x2004: ' ', 0x2005: ' ',
    0x2006: ' ', 0x2007: ' ', 0x2008: ' ', 0x2009: ' ',
    0x200A: ' ', 0x202F: ' ', 0x205F: ' ', 0x3000: ' ',
    0x200B: ' ', 0x200C: ' ', 0x200D: ' ', 0xFEFF: ' ',

    0x2018: "'", 0x2019: "'", 0x201A: ',', 0x201B: "'",
    0x201C: '"', 0x201D: '"', 0x201E: '"', 0x201F: '"',
    0x2032: "'", 0x2033: '"', 0x2035: "'", 0x2036: '"',
    0x00AB: '<', 0x00BB: '>', 0x00B4: "'",

    0x2010: '-', 0x2011: '-', 0x2012: '-', 0x2013: '-',
    0x2014: '-', 0x2015: '-', 0x2053: '~', 0x2212: '-',

    0x2026: '.', 0x2025: '.',

    0x00B7: '.', 0x2022: '*', 0x2023: '*', 0x2024: '.',
    0x25CF: 'O', 0x25CB: 'o', 0x25AA: '.', 0x25AB: '.',

    0x2190: '<', 0x2192: '>', 0x2191: '^', 0x2193: 'v',
    0x2194: '-', 0x2195: '|',

    0x2713: '+', 0x2714: '+', 0x2717: 'x', 0x2718: 'x',

    0x2580: '"', 0x2581: '_', 0x2582: '_', 0x2583: '_',
    0x2584: '_', 0x2585: '_', 0x2586: '_', 0x2587: '_',
    0x2588: '#', 0x2589: '#', 0x258A: '#', 0x258B: '#',
    0x258C: '#', 0x258D: '#', 0x258E: '#', 0x258F: '#',
    0x2590: '#', 0x2591: '.', 0x2592: ':', 0x2593: '%',
    0x2594: '-', 0x2595: '|',
    0x2596: '.', 0x2597: '.', 0x2598: "'", 0x2599: '#',
    0x259A: '#', 0x259B: '#', 0x259C: '#', 0x259D: "'",
    0x259E: '#', 0x259F: '#',

    0x25A0: '#', 0x25A1: '.', 0x25B2: '^', 0x25B3: '^',
    0x25B6: '>', 0x25B7: '>', 0x25BA: '>',
    0x25BC: 'v', 0x25BD: 'v', 0x25C0: '<', 0x25C4: '<',
    0x25C6: '#', 0x25C9: 'O',

    0x00D7: 'x', 0x00F7: '/', 0x2264: '<', 0x2265: '>',
    0x2260: '!', 0x2248: '~', 0x221E: '8',

    0x20AC: 'E', 0x00A3: 'L', 0x00A5: 'Y', 0x20BD: 'P',
}

for _cp in range(0x2500, 0x2580):
    if _cp in (0x2500, 0x2501, 0x2504, 0x2505, 0x2508, 0x2509,
               0x254C, 0x254D, 0x2550, 0x256A, 0x256C,
               0x2574, 0x2576, 0x2578, 0x257A, 0x257C, 0x257E):
        _UNICODE_FALLBACK.setdefault(_cp, '-')
    elif _cp in (0x2502, 0x2503, 0x2506, 0x2507, 0x250A, 0x250B,
                 0x254E, 0x254F, 0x2551,
                 0x2575, 0x2577, 0x2579, 0x257B, 0x257D, 0x257F):
        _UNICODE_FALLBACK.setdefault(_cp, '|')
    else:
        _UNICODE_FALLBACK.setdefault(_cp, '+')

_unknown_seen: set[int] = set()


def _ascii_fallback(ch: str) -> str:
    if not ch or ch == '\x00':
        return ' '
    code = ord(ch)
    if code < 32:
        return ' '
    if code < 127:
        return ch
        
    # Позволяем терминалу рисовать кириллицу
    if 0x0400 <= code <= 0x04FF:
        return ch

    mapped = _UNICODE_FALLBACK.get(code)
    if mapped is not None:
        return mapped[0] if mapped else ' '

    if 0x2000 <= code <= 0x206F: return '.'
    if 0x2070 <= code <= 0x209F: return '?'
    if 0x20A0 <= code <= 0x20CF: return '$'
    if 0x2150 <= code <= 0x218F: return '#'
    if 0x2190 <= code <= 0x21FF: return '>'
    if 0x2200 <= code <= 0x22FF: return '?'
    if 0x2300 <= code <= 0x23FF: return '?'
    if 0x25A0 <= code <= 0x25FF: return '*'
    if 0x2600 <= code <= 0x26FF: return '*'
    if 0x2700 <= code <= 0x27BF: return '+'

    if 0x3000 <= code <= 0x303F: return '.'
    if 0x3040 <= code <= 0x30FF: return '?'
    if 0x4E00 <= code <= 0x9FFF: return '?'
    if 0xAC00 <= code <= 0xD7AF: return '?'
    if 0xFF01 <= code <= 0xFF5E: return chr(code - 0xFEE0)
    if 0xFF00 <= code <= 0xFFEF: return '?'

    if code not in _unknown_seen:
        _unknown_seen.add(code)
        logger.warning("Unknown glyph U+%04X (%r) -> '?'", code, ch)
    return '?'


# ── RGB helpers ───────────────────────────────────────────────────────────────

def _rgb_from_int(color: int) -> tuple[int, int, int]:
    return ((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF)


def _rgb_to_xterm256(rgb: int) -> int:
    r, g, b = _rgb_from_int(rgb)
    if abs(r - g) <= 10 and abs(g - b) <= 10 and abs(r - b) <= 10:
        avg = (r + g + b) // 3
        if avg < 8:
            return 16
        if avg > 238:
            return 231
        idx = min(23, (avg - 8) // 10)
        return 232 + idx

    def to6(v: int) -> int:
        if v < 48: return 0
        if v < 115: return 1
        return (v - 35) // 40

    return 16 + 36 * to6(r) + 6 * to6(g) + to6(b)


# Образцы для 8-цветного fallback (тёмная половина).
_ANSI8_DARK = (
    (0x00, 0x00, 0x00),   # 0 black
    (0x80, 0x00, 0x00),   # 1 dark red
    (0x00, 0x80, 0x00),   # 2 dark green
    (0x80, 0x80, 0x00),   # 3 dark yellow
    (0x00, 0x00, 0x80),   # 4 dark blue
    (0x80, 0x00, 0x80),   # 5 dark magenta
    (0x00, 0x80, 0x80),   # 6 dark cyan
    (0xC0, 0xC0, 0xC0),   # 7 light grey
)
_ANSI8_BRIGHT = (
    (0x80, 0x80, 0x80),   # bright black
    (0xFF, 0x00, 0x00),   # bright red
    (0x00, 0xFF, 0x00),   # bright green
    (0xFF, 0xFF, 0x00),   # bright yellow
    (0x00, 0x00, 0xFF),   # bright blue
    (0xFF, 0x00, 0xFF),   # bright magenta
    (0x00, 0xFF, 0xFF),   # bright cyan
    (0xFF, 0xFF, 0xFF),   # bright white
)


def _rgb_to_ansi8(rgb: int, allow_bold: bool = True) -> tuple[int, bool]:
    """Ближайший из 8 цветов. Возвращает (idx, use_bold)."""
    r, g, b = _rgb_from_int(rgb)
    best_i = 0
    best_bold = False
    best_d = 1 << 30

    for i in range(8):
        cr, cg, cb = _ANSI8_DARK[i]
        d = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
        if d < best_d:
            best_d = d
            best_i = i
            best_bold = False

    if allow_bold:
        for i in range(8):
            cr, cg, cb = _ANSI8_BRIGHT[i]
            d = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
            if d + 1 < best_d:
                best_d = d
                best_i = i
                best_bold = True

    return best_i, best_bold


# ── Маппинг curses → OC key_down/key_up ───────────────────────────────────────

_CURSES_TO_OC = {
    8:                    (8, 14),
    9:                    (9, 15),
    10:                   (13, 28),
    13:                   (13, 28),
    27:                   (27, 1),
    127:                  (8, 14),
    curses.KEY_UP:        (-1, 200),
    curses.KEY_DOWN:      (-1, 208),
    curses.KEY_LEFT:      (-1, 203),
    curses.KEY_RIGHT:     (-1, 205),
    curses.KEY_HOME:      (-1, 199),
    curses.KEY_END:       (-1, 207),
    curses.KEY_PPAGE:     (-1, 201),
    curses.KEY_NPAGE:     (-1, 209),
    curses.KEY_DC:        (-1, 211),
    curses.KEY_IC:        (-1, 210),
}
if hasattr(curses, "KEY_BACKSPACE"):
    _CURSES_TO_OC[curses.KEY_BACKSPACE] = (8, 14)
if hasattr(curses, "KEY_ENTER"):
    _CURSES_TO_OC[curses.KEY_ENTER] = (13, 28)

_CURSES_TO_OC[343] = (13, 28)  # Numpad Enter (Linux/ncurses)
_CURSES_TO_OC[459] = (13, 28)  # Numpad Enter (Windows/PDCurses)

_FKEY_SCANS = {
    1: 59, 2: 60, 3: 61, 4: 62, 5: 63, 6: 64,
    7: 65, 8: 66, 9: 67, 10: 68, 11: 87, 12: 88,
}
for _i, _scan in _FKEY_SCANS.items():
    _const = getattr(curses, f"KEY_F{_i}", None)
    if _const is not None:
        _CURSES_TO_OC[_const] = (-1, _scan)

_CTRL_SCAN = {
    1:  0x1E, 2:  0x30, 3:  0x2E, 4:  0x20, 5:  0x12, 6:  0x21, 7:  0x22,
    8:  0x23, 9:  0x17, 10: 0x24, 11: 0x25, 12: 0x26, 13: 0x32, 14: 0x31,
    15: 0x18, 16: 0x19, 17: 0x10, 18: 0x13, 19: 0x1F, 20: 0x14, 21: 0x16,
    22: 0x2F, 23: 0x11, 24: 0x2D, 25: 0x15, 26: 0x2C,
}


class TextGPU:
    def __init__(self):
        self._stdscr = None
        self._width = 80
        self._height = 24
        self._fg_rgb = 0xFFFFFF
        self._bg_rgb = 0x000000
        self._tbuffer: list[list[str]] = []
        self._bbuffer: list[list[int]] = []
        self._fbuffer: list[list[int]] = []
        self._lock = threading.Lock()
        self._screen_addr: str | None = None
        self._keyboard_addr: str | None = None

        self._stop_input = threading.Event()
        self._input_thread: threading.Thread | None = None
        self._alt_thread: threading.Thread | None = None
        self._pressed_keys: set[int] = set()

        self._alt_l_down = False
        self._alt_r_down = False

        # "8" | "16" | "256"
        self._color_mode = "8"

        # Lazy pair allocation (для 16/256).
        self._pair_cache: dict[tuple[int, int], int] = {}
        self._next_pair = 1

        self._selection_cells: set[tuple[int, int]] = set()
        self._sel_anchor_x = 0
        self._sel_anchor_y = 0
        self._sel_cursor_x = 0
        self._sel_cursor_y = 0
        self._sel_active = False

    def _init_buffers(self, w: int, h: int):
        self._width = w
        self._height = h
        self._tbuffer = [[" "] * w for _ in range(h)]
        self._bbuffer = [[0x000000] * w for _ in range(h)]
        self._fbuffer = [[0xFFFFFF] * w for _ in range(h)]

    # ── Цвета ──────────────────────────────────────────────────────────
    def _init_colors(self):
        c = curses.COLORS
        if c >= 256:
            self._color_mode = "256"
        elif c >= 16:
            self._color_mode = "16"
        else:
            self._color_mode = "8"

        self._pair_cache = {}
        self._next_pair = 1

        # В 8-цветном режиме все 64 пары инициализируем заранее.
        # В 16-цветном — 255 пар, а нам может понадобиться до 256,
        # поэтому аллоцируем лениво.
        if self._color_mode == "8":
            for fg in range(8):
                for bg in range(8):
                    try:
                        curses.init_pair(fg * 8 + bg + 1, fg, bg)
                    except curses.error:
                        pass

        logger.info(
            "TextGPU colors: %s-color mode "
            "(COLORS=%d, COLOR_PAIRS=%d, RED=%d, BLUE=%d, "
            "GREEN=%d, YELLOW=%d)",
            self._color_mode, c, curses.COLOR_PAIRS,
            curses.COLOR_RED, curses.COLOR_BLUE,
            curses.COLOR_GREEN, curses.COLOR_YELLOW,
        )

    def _pair_for(self, fg_idx: int, bg_idx: int) -> int:
        """Ленивая аллокация пары для 16/256-цветных режимов."""
        key = (fg_idx, bg_idx)
        pair = self._pair_cache.get(key)
        if pair is not None:
            return pair
        if self._next_pair >= curses.COLOR_PAIRS - 1:
            # Пары кончились — отдаём любую существующую.
            return next(iter(self._pair_cache.values()), 1)
        pair = self._next_pair
        self._next_pair += 1
        try:
            curses.init_pair(pair, fg_idx, bg_idx)
        except curses.error:
            return 1
        self._pair_cache[key] = pair
        return pair

    def _attrs_for(self, fg_rgb: int, bg_rgb: int) -> int:
        if self._color_mode == "256":
            fg_idx = _rgb_to_xterm256(fg_rgb)
            bg_idx = _rgb_to_xterm256(bg_rgb)
            if fg_idx == bg_idx:
                fg_idx = 231 if bg_idx != 231 else 16
            pair = self._pair_for(fg_idx, bg_idx)
            return curses.color_pair(pair)

        if self._color_mode == "16":
            # 16 ANSI-цветов через curses.COLOR_X и curses.COLOR_X + 8.
            # Это переносимо: в pdcurses/Windows порядок CGA, но +8 даёт
            # именно «яркий» вариант нужного цвета, независимо от библиотеки.
            fg_idx, fg_bold = _rgb_to_ansi8(fg_rgb, allow_bold=True)
            bg_idx, _ = _rgb_to_ansi8(bg_rgb, allow_bold=False)

            if fg_bold:
                fg_curses = self._ansi_to_curses[fg_idx] + 8
            else:
                fg_curses = self._ansi_to_curses[fg_idx]
            bg_curses = self._ansi_to_curses[bg_idx]

            # Авто-контраст: если цвета совпали — сдвигаем fg на яркий.
            if fg_curses == bg_curses:
                fg_curses = self._ansi_to_curses[fg_idx] + 8

            pair = self._pair_for(fg_curses, bg_curses)
            return curses.color_pair(pair)

        # 8-цветный fallback.
        fg_idx, fg_bold = _rgb_to_ansi8(fg_rgb, allow_bold=True)
        bg_idx, _ = _rgb_to_ansi8(bg_rgb, allow_bold=False)
        if fg_idx == bg_idx and not fg_bold:
            fg_bold = True
        pair = fg_idx * 8 + bg_idx + 1
        attrs = curses.color_pair(pair)
        if fg_bold:
            attrs |= curses.A_BOLD
        return attrs

    # ── Инициализация ──────────────────────────────────────────────────
    def _setup_curses(self):
        self._stdscr = curses.initscr()
        curses.start_color()
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        curses.noecho()
        curses.cbreak()
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        self._stdscr.keypad(True)
        self._stdscr.nodelay(True)

        if not _IS_WINDOWS:
            os.environ.setdefault("ESCDELAY", str(_ESC_DELAY_MS))

        # Табличка curses-констант для ANSI-индексов 0..7.
        # В pdcurses (Windows) COLOR_RED=4, COLOR_BLUE=1;
        # в ncurses (Linux/macOS) COLOR_RED=1, COLOR_BLUE=4.
        # Мы всегда используем сами константы — они означают одно и то же.
        self._ansi_to_curses = (
            curses.COLOR_BLACK,    # 0
            curses.COLOR_RED,      # 1
            curses.COLOR_GREEN,    # 2
            curses.COLOR_YELLOW,   # 3
            curses.COLOR_BLUE,     # 4
            curses.COLOR_MAGENTA,  # 5
            curses.COLOR_CYAN,     # 6
            curses.COLOR_WHITE,    # 7
        )

        self._init_colors()

        h, w = self._stdscr.getmaxyx()
        self._init_buffers(w, h)

    def _teardown_curses(self):
        self._stop_input.set()
        if self._input_thread:
            self._input_thread.join(timeout=1.0)
            self._input_thread = None
        if self._alt_thread:
            self._alt_thread.join(timeout=1.0)
            self._alt_thread = None
        if self._stdscr:
            try:
                curses.nocbreak()
                self._stdscr.keypad(False)
                curses.echo()
                curses.endwin()
            except curses.error:
                pass
            self._stdscr = None

    # ── Selection ──────────────────────────────────────────────────────
    def _has_selection(self) -> bool:
        return self._sel_active and (
            self._sel_anchor_x != self._sel_cursor_x or
            self._sel_anchor_y != self._sel_cursor_y
        )

    def _selection_bounds(self):
        x0 = min(self._sel_anchor_x, self._sel_cursor_x)
        y0 = min(self._sel_anchor_y, self._sel_cursor_y)
        x1 = max(self._sel_anchor_x, self._sel_cursor_x)
        y1 = max(self._sel_anchor_y, self._sel_cursor_y)
        return x0, y0, x1, y1

    def _recompute_selection_cells(self):
        self._selection_cells = set()
        if not self._has_selection():
            return
        x0, y0, x1, y1 = self._selection_bounds()
        for y in range(max(0, y0), min(self._height, y1 + 1)):
            start = max(0, x0) if y == y0 else 0
            end   = min(self._width - 1, x1) if y == y1 else self._width - 1
            if end < start:
                continue
            for x in range(start, end + 1):
                self._selection_cells.add((x, y))

    def _redraw_all(self):
        if self._stdscr is None:
            return
        for y in range(self._height):
            for x in range(self._width):
                self._render_cell(x, y, self._tbuffer[y][x])
        try:
            self._stdscr.refresh()
        except curses.error:
            pass

    def _select_all(self):
        if self._width <= 0 or self._height <= 0:
            return
        self._sel_anchor_x = 0
        self._sel_anchor_y = 0
        self._sel_cursor_x = self._width - 1
        self._sel_cursor_y = self._height - 1
        self._sel_active = True
        self._recompute_selection_cells()
        self._redraw_all()

    def _clear_selection(self):
        if not self._sel_active and not self._selection_cells:
            return
        self._sel_active = False
        self._selection_cells = set()
        self._redraw_all()

    def _selection_text(self) -> str:
        if not self._has_selection():
            return ""
        x0, y0, x1, y1 = self._selection_bounds()
        lines = []
        for y in range(y0, y1 + 1):
            if y < 0 or y >= self._height:
                continue
            start = max(0, x0) if y == y0 else 0
            end   = min(self._width - 1, x1) if y == y1 else self._width - 1
            if end < start:
                lines.append("")
                continue
            row = self._tbuffer[y]
            buf = []
            for x in range(start, end + 1):
                c = row[x]
                if c == "\x00":
                    continue
                buf.append(c)
            lines.append("".join(buf).rstrip())
        return "\n".join(lines)

    def _copy_selection_to_clipboard(self) -> bool:
        text = self._selection_text()
        if not text:
            return False
        try:
            for addr, entry in component_module._components.items():
                if entry.type == "clipboard":
                    component_module.invoke(addr, "set", _to_lua(text))
                    return True
        except Exception:
            logger.exception("copy_selection failed")
        return False

    def _send_paste(self, text: str) -> int:
        kb = self._keyboard_addr or "kb"
        pressed_mods = [s for s in list(self._pressed_keys)
                        if s in _MODIFIER_SCANS]
        for scan in pressed_mods:
            computer.push_signal("key_up", kb, -1, scan, 0)

        pushed = 0
        for ch in text:
            if ch == "\r":
                ch = "\n"
            if ch == "\n":
                computer.push_signal("key_down", kb, 13, 28, 0)
                computer.push_signal("key_up",   kb, 13, 28, 0)
                pushed += 1
            elif ch == "\t":
                computer.push_signal("key_down", kb, 9, 15, 0)
                computer.push_signal("key_up",   kb, 9, 15, 0)
                pushed += 1
            else:
                code = ord(ch)
                if code >= 32:
                    computer.push_signal("key_down", kb, code, -1, 0)
                    computer.push_signal("key_up",   kb, code, -1, 0)
                    pushed += 1

        for scan in pressed_mods:
            computer.push_signal("key_down", kb, -1, scan, 0)

        return pushed

    def _paste_from_clipboard(self) -> bool:
        try:
            raw = None
            for addr, entry in component_module._components.items():
                if entry.type == "clipboard":
                    raw = component_module.invoke(addr, "get")
                    break
            if raw is None:
                return False
        except Exception:
            logger.exception("paste_clipboard failed")
            return False

        text = _from_lua(raw) if isinstance(raw, str) else ""
        if not text:
            return False

        pushed = self._send_paste(text)
        logger.debug("clipboard paste: %d chars pushed", pushed)
        return pushed > 0

    # ── Keyboard helpers ───────────────────────────────────────────────
    def _read_esc_suffix(self) -> int:
        if self._stdscr is None:
            return -1
        try:
            try:
                self._stdscr.timeout(_ESC_DELAY_MS)
            except Exception:
                return -1
            try:
                return self._stdscr.getch()
            except Exception:
                return -1
        finally:
            try:
                self._stdscr.nodelay(True)
            except Exception:
                pass

    def _resolve_key(self, ch, clear_sel=True):
        if ch in _CURSES_TO_OC:
            return _CURSES_TO_OC[ch]

        if 1 <= ch <= 26 and ch not in (8, 9, 10, 13):
            return ch, _CTRL_SCAN.get(ch, -1)

        if 32 <= ch < 127:
            if clear_sel and self._has_selection():
                self._clear_selection()
            return ch, -1

        # Разрешаем ввод кириллицы и других Unicode-символов
        if ch > 127:
            return ch, -1

        return None
        
    def _push_key(self, kb_addr, ascii_code, scan):
        # Ctrl+A..Z приходят из curses «готовым» control-кодом (1..26),
        # без отдельного события нажатия LCtrl. В pygame/sdl2 модификаторы
        # шлются своими событиями, поэтому edit.lua (и др.) видят
        # keyboard.isControlDown()==true. В curses этого нет — синтетически
        # «нажимаем» LCtrl на время key_down/key_up.
        #
        # Ограничение: только 1..26 и не спецкоды (8,9,10,13,27), у которых
        # _CURSES_TO_OC даёт свои пары (Backspace/Tab/LF/CR/Esc).
        is_ctrl_letter = 1 <= ascii_code <= 26 and ascii_code not in (8, 9, 10, 13, 27)

        if is_ctrl_letter:
            key_id = scan if scan != -1 else ascii_code
            try:
                self._pressed_keys.add(0x1D)  # LCtrl
                computer.push_signal("key_down", kb_addr, -1, 0x1D, 0)
                self._pressed_keys.add(key_id)
                computer.push_signal("key_down", kb_addr, ascii_code, scan, 0)
                computer.push_signal("key_up", kb_addr, ascii_code, scan, 0)
                self._pressed_keys.discard(key_id)
                computer.push_signal("key_up", kb_addr, -1, 0x1D, 0)
                self._pressed_keys.discard(0x1D)
            except Exception as e:
                logger.error("keyboard push error: %s", e)
            return

        key_id = scan if scan != -1 else ascii_code
        try:
            self._pressed_keys.add(key_id)
            computer.push_signal("key_down", kb_addr, ascii_code, scan, 0)
            computer.push_signal("key_up", kb_addr, ascii_code, scan, 0)
        except Exception as e:
            logger.error("keyboard push error: %s", e)
        finally:
            self._pressed_keys.discard(key_id)

    def _push_modifier(self, kb_addr, scan, down):
        try:
            if down:
                self._pressed_keys.add(scan)
            else:
                self._pressed_keys.discard(scan)
            computer.push_signal("key_down" if down else "key_up",
                                 kb_addr, -1, scan, 0)
        except Exception as e:
            logger.error("modifier push error: %s", e)

    # ── Keyboard threads ───────────────────────────────────────────────
    def _alt_poll_thread(self):
        if not _IS_WINDOWS:
            return
        kb_addr = self._keyboard_addr or "kb"
        try:
            u = ctypes.windll.user32
        except Exception as e:
            logger.warning("GetAsyncKeyState unavailable: %s", e)
            return

        prev_l = prev_r = False
        while not self._stop_input.is_set():
            try:
                l = bool(u.GetAsyncKeyState(_VK_LMENU) & 0x8000)
                r = bool(u.GetAsyncKeyState(_VK_RMENU) & 0x8000)
            except Exception:
                break

            self._alt_l_down = l
            self._alt_r_down = r

            if l != prev_l:
                self._push_modifier(kb_addr, _LALT_SCAN, l)
                prev_l = l
            if r != prev_r:
                self._push_modifier(kb_addr, _RALT_SCAN, r)
                prev_r = r

            time.sleep(_ALT_POLL_INTERVAL)

    def _keyboard_thread(self):
        kb_addr = self._keyboard_addr or "kb"

        while not self._stop_input.is_set():
            try:
                ch = self._stdscr.getch() if self._stdscr else -1
            except Exception:
                time.sleep(0.05)
                continue

            if ch == -1:
                time.sleep(0.005)
                continue

            if ch == 3 and self._has_selection():
                if self._copy_selection_to_clipboard():
                    continue
            if ch == 22:
                if self._paste_from_clipboard():
                    self._clear_selection()
                    continue

            if ch == 27:
                if _IS_WINDOWS:
                    if self._alt_l_down or self._alt_r_down:
                        suffix = self._read_esc_suffix()
                        if suffix != -1:
                            resolved = self._resolve_key(suffix, clear_sel=False)
                            if resolved is not None:
                                a, s = resolved
                                self._push_key(kb_addr, a, s)
                        continue
                    else:
                        self._push_key(kb_addr, 27, 1)
                        continue
                else:
                    suffix = self._read_esc_suffix()
                    if suffix == -1:
                        self._push_key(kb_addr, 27, 1)
                        continue
                    self._push_modifier(kb_addr, _LALT_SCAN, True)
                    resolved = self._resolve_key(suffix, clear_sel=False)
                    if resolved is not None:
                        a, s = resolved
                        self._push_key(kb_addr, a, s)
                    self._push_modifier(kb_addr, _LALT_SCAN, False)
                    continue

            if ch == 1:
                self._select_all()
                continue

            resolved = self._resolve_key(ch, clear_sel=True)
            if resolved is None:
                continue
            a, s = resolved
            self._push_key(kb_addr, a, s)

    # ── GPU API ────────────────────────────────────────────────────────
    def bind(self, *_):
        return False, "This is static bound gpu"

    def get_screen(self):
        return self._screen_addr

    def set_background(self, color, is_palette=False):
        old = self._bg_rgb
        try:
            self._bg_rgb = int(color) & 0xFFFFFF
        except (TypeError, ValueError):
            pass
        return old

    def set_foreground(self, color, is_palette=False):
        old = self._fg_rgb
        try:
            self._fg_rgb = int(color) & 0xFFFFFF
        except (TypeError, ValueError):
            pass
        return old

    def get_background(self):
        return self._bg_rgb, False

    def get_foreground(self):
        return self._fg_rgb, False

    def get_palette_color(self, *_): return None
    def set_palette_color(self, *_): return None
    def max_depth(self) -> int:
        return {"256": 8, "16": 4, "8": 3}[self._color_mode]
    def set_depth(self, *_): return False
    def get_depth(self) -> int:
        return self.max_depth()

    def max_resolution(self):
        if self._stdscr:
            h, w = self._stdscr.getmaxyx()
            return w, h
        return self._width, self._height

    def get_resolution(self):
        return self.max_resolution()

    def get_viewport(self):
        return self.max_resolution()

    def set_viewport(self, w, h):
        return False, "Viewport not supported"

    def set_resolution(self, w, h):
        return False, "Non resizeable gpu"

    def get(self, x, y):
        x -= 1; y -= 1
        if 0 <= y < self._height and 0 <= x < self._width:
            ch = self._tbuffer[y][x]
            fg = self._fbuffer[y][x]
            bg = self._bbuffer[y][x]
            return _to_lua(ch), fg, bg
        return " ", 0xFFFFFF, 0

    def set(self, x, y, value, vertical=False):
        value = _from_lua(value)
        x -= 1; y -= 1
        with self._lock:
            if not vertical:
                for i, ch in enumerate(value):
                    cx = x + i
                    if 0 <= y < self._height and 0 <= cx < self._width:
                        self._tbuffer[y][cx] = ch
                        self._fbuffer[y][cx] = self._fg_rgb
                        self._bbuffer[y][cx] = self._bg_rgb
                        self._render_cell(cx, y, ch)
            else:
                for i, ch in enumerate(value):
                    cy = y + i
                    if 0 <= cy < self._height and 0 <= x < self._width:
                        self._tbuffer[cy][x] = ch
                        self._fbuffer[cy][x] = self._fg_rgb
                        self._bbuffer[cy][x] = self._bg_rgb
                        self._render_cell(x, cy, ch)
            if self._stdscr:
                try:
                    self._stdscr.refresh()
                except curses.error:
                    pass
        return True

    def copy(self, x, y, w, h, tx, ty):
        x -= 1; y -= 1
        with self._lock:
            tmp_t = [row[x:x+w] for row in self._tbuffer[y:y+h]]
            tmp_b = [row[x:x+w] for row in self._bbuffer[y:y+h]]
            tmp_f = [row[x:x+w] for row in self._fbuffer[y:y+h]]
            dx = tx; dy = ty
            for i, (rt, rb, rf) in enumerate(zip(tmp_t, tmp_b, tmp_f)):
                row_y = y + dy + i
                if 0 <= row_y < self._height:
                    for j, (ct, cb, cf) in enumerate(zip(rt, rb, rf)):
                        col_x = x + dx + j
                        if 0 <= col_x < self._width:
                            self._tbuffer[row_y][col_x] = ct
                            self._bbuffer[row_y][col_x] = cb
                            self._fbuffer[row_y][col_x] = cf
                            self._render_cell(col_x, row_y, ct)
            if self._stdscr:
                try:
                    self._stdscr.refresh()
                except curses.error:
                    pass
        return True

    def fill(self, x, y, w, h, ch):
        ch = _from_lua(ch)
        ch = ch[0] if ch else " "
        x -= 1; y -= 1
        with self._lock:
            for row in range(y, y + h):
                for col in range(x, x + w):
                    if 0 <= row < self._height and 0 <= col < self._width:
                        self._tbuffer[row][col] = ch
                        self._fbuffer[row][col] = self._fg_rgb
                        self._bbuffer[row][col] = self._bg_rgb
                        self._render_cell(col, row, ch)
            if self._stdscr:
                try:
                    self._stdscr.refresh()
                except curses.error:
                    pass
        return True

    def _render_cell(self, x: int, y: int, ch: str):
        if not self._stdscr:
            return
        try:
            fg_rgb = self._fbuffer[y][x]
            bg_rgb = self._bbuffer[y][x]
            attrs = self._attrs_for(fg_rgb, bg_rgb)
            if self._selection_cells and (x, y) in self._selection_cells:
                attrs |= curses.A_REVERSE
            safe = _ascii_fallback(ch)
            if not safe:
                safe = ' '
            elif len(safe) > 1:
                safe = safe[0]
            self._stdscr.addch(y, x, safe, attrs)
        except curses.error:
            pass
        except Exception:
            pass

    def as_proxy(self) -> dict:
        return {
            "bind":            self.bind,
            "getScreen":       self.get_screen,
            "setBackground":   self.set_background,
            "setForeground":   self.set_foreground,
            "getBackground":   self.get_background,
            "getForeground":   self.get_foreground,
            "getPaletteColor": self.get_palette_color,
            "setPaletteColor": self.set_palette_color,
            "maxDepth":        self.max_depth,
            "setDepth":        self.set_depth,
            "getDepth":        self.get_depth,
            "maxResolution":   self.max_resolution,
            "getResolution":   self.get_resolution,
            "getViewport":     self.get_viewport,
            "setViewport":     self.set_viewport,
            "setResolution":   self.set_resolution,
            "get":             self.get,
            "set":             self.set,
            "copy":            self.copy,
            "fill":            self.fill,
        }


class KeyboardComponent:
    def __init__(self, gpu: TextGPU):
        self._gpu = gpu

    def is_key_down(self, code) -> bool:
        return code in self._gpu._pressed_keys

    def as_proxy(self) -> dict:
        return {"isKeyDown": self.is_key_down}


_gpu_instance: TextGPU | None = None


def start():
    global _gpu_instance
    gpu = TextGPU()
    try:
        gpu._setup_curses()
    except Exception as e:
        logger.error("Cannot initialize text gpu: %s", e)
        return None, str(e)

    _gpu_instance = gpu

    kb = KeyboardComponent(gpu)
    keyboard_addr = component_module.register(None, "keyboard", kb.as_proxy())
    gpu._keyboard_addr = keyboard_addr

    screen_addr = component_module.register(None, "screen", {
        "getKeyboards": lambda: [keyboard_addr]
    })
    gpu._screen_addr = screen_addr

    gpu_addr = component_module.register(None, "gpu", gpu.as_proxy())

    gpu._input_thread = threading.Thread(
        target=gpu._keyboard_thread, daemon=True)
    gpu._input_thread.start()

    if _IS_WINDOWS:
        gpu._alt_thread = threading.Thread(
            target=gpu._alt_poll_thread, daemon=True)
        gpu._alt_thread.start()

    def _shutdown():
        gpu._stop_input.set()
        gpu._teardown_curses()

    computer.add_shutdown_hook(_shutdown)
    logger.info(
        "TextGPU started: %s (keyboard %s, screen %s, "
        "windows_alt_poll=%s, colors=%s)",
        gpu_addr, keyboard_addr, screen_addr, _IS_WINDOWS,
        gpu._color_mode)
    return gpu_addr


def get_instance() -> TextGPU | None:
    return _gpu_instance