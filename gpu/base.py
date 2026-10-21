"""
gpu/base.py — общая база для GPU-рендеров LuPI.

Содержит всё, что не зависит от конкретного окна:
  * BaseGPU  — буферы экрана, fg/bg, шрифт, состояние клавиш/мыши,
               отсылка сигналов, регистрация компонентов, GPU API,
               выделение текста + копирование/вставка.
  * load_font_hex()        — загрузка font.txt/font.hex.
  * KeyboardComponent,
    ScreenComponent        — прокси компонентов.
"""
from __future__ import annotations
import os
import logging

logger = logging.getLogger("lupi.gpu.base")

BASE_CELL_W = 8
BASE_CELL_H = 16
DEFAULT_COLS = 80
DEFAULT_ROWS = 25
DEFAULT_FPS = 30
DEFAULT_BG = (0x00, 0x00, 0x00)
DEFAULT_FG = (0xFF, 0xFF, 0xFF)
HARD_MAX_COLS = 512
HARD_MAX_ROWS = 256
DESKTOP_MARGIN_W = 40
DESKTOP_MARGIN_H = 100

PLAYER_NAME = "Player"

SELECTION_COLOR = (0x33, 0x77, 0xCC)
SELECTION_BLEND = 0.45

# Scan-коды клавиш-модификаторов (PC/AT set 1)
_MODIFIER_SCANS = frozenset((
    0x2A,  # LShift
    0x36,  # RShift
    0x1D,  # LCtrl
    0x9D,  # RCtrl
    0x38,  # LAlt
    0xB8,  # RAlt
))

_FONT_HEX_PATHS = (
    "font.txt", "font.hex",
    os.path.join("root", "font.txt"),
    os.path.join("root", "font.hex"),
    os.path.join("openos", "font.txt"),
    os.path.join("openos", "font.hex"),
    os.path.join("assets", "font.txt"),
    os.path.join("assets", "font.hex"),
    os.path.join("bios", "font.txt"),
    os.path.join("bios", "font.hex"),
)

# Реестр кодпоинтов, у которых глиф шириной 2 клетки (32 байта в font.hex).
# Заполняется load_font_hex(); используется в т.ч. Lua-функцией unicode.isWide.
_WIDE_CODEPOINTS: set[int] = set()


def is_wide_codepoint(cp: int) -> bool:
    """True, если глиф кодпоинта занимает 2 клетки в загруженном шрифте."""
    return cp in _WIDE_CODEPOINTS


# ── Lua <-> Python строковые конверсии ────────────────────────────────────────

def _from_lua(s):
    if not isinstance(s, str):
        return s
    try:
        s.encode('ascii')
        return s
    except UnicodeEncodeError:
        pass
    try:
        raw = s.encode('latin-1')
    except UnicodeEncodeError:
        return s
    # decode + срезать хвостовые U+FFFD (они всегда только в конце)
    return raw.decode('utf-8', errors='replace').rstrip('\ufffd')
    
def _to_lua(s):
    if not isinstance(s, str):
        return s
    try:
        return s.encode('utf-8').decode('latin-1')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def _rgb_from_int(color):
    color = int(color) & 0xFFFFFF
    return ((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF)


def _int_from_rgb(rgb):
    r, g, b = rgb
    return (r << 16) | (g << 8) | b


def _blend_rgb(rgb, target, t):
    return (
        int(rgb[0] + (target[0] - rgb[0]) * t),
        int(rgb[1] + (target[1] - rgb[1]) * t),
        int(rgb[2] + (target[2] - rgb[2]) * t),
    )


# ── Загрузка шрифта ───────────────────────────────────────────────────────────

def load_font_hex(paths=_FONT_HEX_PATHS):
    data = {}
    used = None
    _WIDE_CODEPOINTS.clear()
    for p in paths:
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or ":" not in line:
                        continue
                    cp_hex, data_hex = line.split(":", 1)
                    try:
                        cp = int(cp_hex.strip(), 16)
                    except ValueError:
                        continue
                    data_hex = data_hex.strip()
                    if not data_hex or len(data_hex) % 2 != 0:
                        continue
                    try:
                        raw = bytes.fromhex(data_hex)
                    except ValueError:
                        continue
                    if len(raw) == 16:
                        data[cp] = (raw, 8)
                    elif len(raw) == 32:
                        data[cp] = (raw, 16)
                        _WIDE_CODEPOINTS.add(cp)
                    elif len(raw) > 32:
                        data[cp] = (raw[:32], 16)
                        _WIDE_CODEPOINTS.add(cp)
                    elif len(raw) > 16:
                        data[cp] = (raw[:16], 8)
                    else:
                        data[cp] = (raw + bytes(16 - len(raw)), 8)
            used = p
            break
        except OSError as e:
            logger.warning("Cannot read font %s: %s", p, e)
            # Стандартные диапазоны широких символов (East Asian Width = W или F).
            # Добавляем их принудительно, так как некоторые CJK-символы
            # хранятся в font.hex как 8x16 (хотя в терминале должны быть 16x16).
            for start, end in [
                (0x1100, 0x115F), (0x2E80, 0xA4CF), (0xAC00, 0xD7A3),
                (0xF900, 0xFAFF), (0xFE10, 0xFE19), (0xFE30, 0xFE6F),
                (0xFF00, 0xFF60), (0xFFE0, 0xFFE6), (0x1F300, 0x1F64F),
                (0x1F900, 0x1F9FF), (0x20000, 0x3FFFD)
            ]:
                for cp in range(start, end + 1):
                    _WIDE_CODEPOINTS.add(cp)

    if used:
        logger.info("Loaded %d glyphs from %s (%d wide)",
                    len(data), used, len(_WIDE_CODEPOINTS))
    else:
        logger.warning("font.hex/font.txt not found (tried %s)", paths)
    return data


# ── Маппинг Ctrl+A..Z → PC/AT set 1 ──────────────────────────────────────────

CTRL_SCAN = {
    1: 0x1E, 2: 0x30, 3: 0x2E, 4: 0x20, 5: 0x12, 6: 0x21, 7: 0x22,
    8: 0x23, 9: 0x17, 10: 0x24, 11: 0x25, 12: 0x26, 13: 0x32, 14: 0x31,
    15: 0x18, 16: 0x19, 17: 0x10, 18: 0x13, 19: 0x1F, 20: 0x14, 21: 0x16,
    22: 0x2F, 23: 0x11, 24: 0x2D, 25: 0x15, 26: 0x2C,
}


# ── Общая база ────────────────────────────────────────────────────────────────

class BaseGPU:
    BASE_CELL_W = BASE_CELL_W
    BASE_CELL_H = BASE_CELL_H
    DEFAULT_FPS = DEFAULT_FPS
    DEFAULT_BG = DEFAULT_BG
    DEFAULT_FG = DEFAULT_FG

    def __init__(self, cols=DEFAULT_COLS, rows=DEFAULT_ROWS, scale=1.0,
                 cell_size=None, fps=DEFAULT_FPS, allow_resize=False,
                 max_cols=None, max_rows=None):
        self._allow_resize = bool(allow_resize)

        if cell_size is not None:
            try:
                cw, chh = cell_size
                self._cell_w = max(1, int(cw))
                self._cell_h = max(1, int(chh))
            except (TypeError, ValueError):
                self._cell_w = BASE_CELL_W
                self._cell_h = BASE_CELL_H
        else:
            try:
                s = float(scale)
            except (TypeError, ValueError):
                s = 1.0
            if s < 1.0:
                s = 1.0
            self._cell_w = max(1, int(round(BASE_CELL_W * s)))
            self._cell_h = max(1, int(round(BASE_CELL_H * s)))

        try:
            f = int(fps)
        except (TypeError, ValueError):
            f = DEFAULT_FPS
        self._fps = max(1, min(240, f))
        self._last_render = 0.0

        self._bg_rgb = DEFAULT_BG
        self._fg_rgb = DEFAULT_FG
        self._fg_is_palette = False
        self._bg_is_palette = False
        self._fg_pal_idx = 0
        self._bg_pal_idx = 0
        self._depth = 8  # 8-bit (полный RGB)

        # Стандартная 16-цветная палитра OpenComputers
        self._palette = [
            (0x00, 0x00, 0x00), (0xFF, 0xCC, 0x33), (0xCC, 0x66, 0xCC), (0x66, 0x99, 0xFF),
            (0xFF, 0xFF, 0x33), (0x33, 0xCC, 0x33), (0xFF, 0x66, 0x99), (0x33, 0x33, 0x33),
            (0xCC, 0xCC, 0xCC), (0x33, 0x66, 0x99), (0x99, 0x33, 0xCC), (0x33, 0x33, 0x99),
            (0x66, 0x33, 0x00), (0x33, 0x66, 0x00), (0xFF, 0x33, 0x33), (0xFF, 0xFF, 0xFF)
        ]

        self._cols = 1
        self._rows = 1
        self._tbuffer = []
        self._bbuffer = []
        self._fbuffer = []
        self._alloc_buffers(max(1, int(cols)), max(1, int(rows)))

        self._max_cols = None
        self._max_rows = None
        if max_cols is not None:
            try:
                self._max_cols = max(self._cols, min(HARD_MAX_COLS, int(max_cols)))
            except (TypeError, ValueError):
                self._max_cols = None
        if max_rows is not None:
            try:
                self._max_rows = max(self._rows, min(HARD_MAX_ROWS, int(max_rows)))
            except (TypeError, ValueError):
                self._max_rows = None

        self._screen_addr = None
        self._keyboard_addr = None
        self._pressed_keys = set()
        self._dirty = False

        self._mouse_btn = None
        self._mouse_lx = 1
        self._mouse_ly = 1

        self._font_data = {}
        self._glyph_bitmap_cache = {}

        self._quit = False

        # Selection state
        self._sel_anchor_x = 0
        self._sel_anchor_y = 0
        self._sel_cursor_x = 0
        self._sel_cursor_y = 0
        self._sel_active = False
        self._sel_dragging = False
        self._selection_cells: set[tuple[int, int]] = set()

        # Паста: физический key_up для 'V' мы проглотили — этот флаг
        # говорит, что следующий key_up 'V' надо тоже проглотить.
        self._v_key_intercepted = False

    # ── Буферы ─────────────────────────────────────────────────────────
    def _alloc_buffers(self, w, h):
        self._cols = max(1, int(w))
        self._rows = max(1, int(h))
        self._tbuffer = [[" "] * self._cols for _ in range(self._rows)]
        self._bbuffer = [[DEFAULT_BG] * self._cols for _ in range(self._rows)]
        self._fbuffer = [[DEFAULT_FG] * self._cols for _ in range(self._rows)]

    def _glyph_bitmap(self, cp):
        cached = self._glyph_bitmap_cache.get(cp)
        if cached is not None:
            return cached
        entry = self._font_data.get(cp)
        if entry is None:
            return None
        raw, width_px = entry
        bytes_per_row = width_px // 8
        bitmap = []
        for row in range(16):
            base = row * bytes_per_row
            row_pixels = []
            for cb in range(bytes_per_row):
                b = raw[base + cb]
                for bit in range(8):
                    row_pixels.append(bool(b & (0x80 >> bit)))
            bitmap.append(row_pixels)
        result = (bitmap, width_px)
        self._glyph_bitmap_cache[cp] = result
        return result

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
        for y in range(max(0, y0), min(self._rows, y1 + 1)):
            start = max(0, x0) if y == y0 else 0
            end   = min(self._cols - 1, x1) if y == y1 else self._cols - 1
            if end < start:
                continue
            for x in range(start, end + 1):
                self._selection_cells.add((x, y))

    def _start_selection(self, x, y):
        self._sel_anchor_x = x
        self._sel_anchor_y = y
        self._sel_cursor_x = x
        self._sel_cursor_y = y
        self._sel_active = True
        self._recompute_selection_cells()
        self._redraw_all()

    def _update_selection(self, x, y):
        if not self._sel_active:
            return
        if (x, y) == (self._sel_cursor_x, self._sel_cursor_y):
            return
        self._sel_cursor_x = x
        self._sel_cursor_y = y
        self._recompute_selection_cells()
        self._redraw_all()

    def _clear_selection(self):
        if not self._sel_active and not self._selection_cells:
            return
        self._sel_active = False
        self._selection_cells = set()
        self._redraw_all()

    def _select_all(self):
        if self._rows <= 0 or self._cols <= 0:
            return
        self._sel_anchor_x = 0
        self._sel_anchor_y = 0
        self._sel_cursor_x = self._cols - 1
        self._sel_cursor_y = self._rows - 1
        self._sel_active = True
        self._recompute_selection_cells()
        self._redraw_all()

    def _apply_selection_color(self, rgb, x, y):
        if not self._selection_cells:
            return rgb
        if (x, y) not in self._selection_cells:
            return rgb
        return _blend_rgb(rgb, SELECTION_COLOR, SELECTION_BLEND)

    def _selection_text(self) -> str:
        if not self._has_selection():
            return ""
        x0, y0, x1, y1 = self._selection_bounds()
        lines = []
        for y in range(y0, y1 + 1):
            if y < 0 or y >= self._rows:
                continue
            start = max(0, x0) if y == y0 else 0
            end   = min(self._cols - 1, x1) if y == y1 else self._cols - 1
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
            logger.info("Copy: nothing selected (Shift+drag to select)")
            return False
        try:
            from core import component as component_mod
            for addr, entry in component_mod._components.items():
                if entry.type == "clipboard":
                    component_mod.invoke(addr, "set", _to_lua(text))
                    logger.info("Copied %d chars to system clipboard: %r",
                                len(text), text[:80])
                    return True
            logger.warning("Copy: no clipboard component registered")
        except Exception:
            logger.exception("copy_selection failed")
        return False

    # ── Вставка ────────────────────────────────────────────────────────
    def _send_paste(self, text: str) -> int:
        """
        Отправить text как последовательность key_down/key_up сигналов.

        КЛЮЧЕВОЙ МОМЕНТ: физический Ctrl (или Shift/Alt) сейчас "зажат".
        Если мы просто отдадим 't', 'e', 's', приложение увидит Ctrl+T и
        проигнорирует. Поэтому:
          1) синтетически отпускаем все зажатые модификаторы;
          2) отдаём текст;
          3) синтетически "нажимаем" модификаторы обратно — тогда
             реальный key_up (который придёт через N мс) корректно
             снимет их состояние.
        """
        from core import computer as _computer
        import time

        kb = self._keyboard_addr or "kb"

        # Снимок зажатых модификаторов по scan-коду.
        pressed_mods = [s for s in list(self._pressed_keys)
                        if s in _MODIFIER_SCANS]

        # 1) Отпускаем
        for scan in pressed_mods:
            _computer.push_signal("key_up", kb, -1, scan, PLAYER_NAME)

        # 2) Текст
        pushed = 0
        for ch in text:
            if ch == "\r":
                ch = "\n"
            if ch == "\n":
                _computer.push_signal("key_down", kb, 13, 28, PLAYER_NAME)
                _computer.push_signal("key_up",   kb, 13, 28, PLAYER_NAME)
                pushed += 1
            elif ch == "\t":
                _computer.push_signal("key_down", kb, 9, 15, PLAYER_NAME)
                _computer.push_signal("key_up",   kb, 9, 15, PLAYER_NAME)
                pushed += 1
            else:
                code = ord(ch)
                if code >= 32:
                    _computer.push_signal("key_down", kb, code, -1, PLAYER_NAME)
                    _computer.push_signal("key_up",   kb, code, -1, PLAYER_NAME)
                    pushed += 1
            
            # Пауза 1 мс, чтобы OpenOS/MineOS успевал обрабатывать широкие символы (CJK)
            time.sleep(0.001)

        # 3) Возвращаем модификаторы в "нажатое" состояние.
        for scan in pressed_mods:
            _computer.push_signal("key_down", kb, -1, scan, PLAYER_NAME)

        return pushed
        
    def _paste_from_clipboard(self) -> bool:
        try:
            from core import component as component_mod
            raw = None
            for addr, entry in component_mod._components.items():
                if entry.type == "clipboard":
                    raw = component_mod.invoke(addr, "get")
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

    # ── Ввод ───────────────────────────────────────────────────────────
    def _push_key(self, ascii_code, scan, down):
        from core import computer
        kb = self._keyboard_addr or "kb"
        computer.push_signal("key_down" if down else "key_up",
                             kb, ascii_code, scan, PLAYER_NAME)
        kid = scan if scan != -1 else ascii_code
        if down:
            self._pressed_keys.add(kid)
        else:
            self._pressed_keys.discard(kid)

    def _cell_from_pos(self, mx, my):
        """
        Пиксельные координаты события → 1-based номер ячейки (как в OC).

        Учитывает HiDPI / DPI-scaling: если размер OS-окна (в пикселях,
        как их видит драйвер) отличается от размера поверхности рендера
        (self._cell_w * self._cols × self._cell_h * self._rows), координаты
        масштабируются. Всё клампится к границам поверхности ДО деления
        на размер ячейки, чтобы мышь в крайнем правом/нижнем пикселе
        попадала в крайнюю ячейку, а не за экран.
        """
        surf_w = self._cell_w * self._cols
        surf_h = self._cell_h * self._rows

        win = self._get_window_size()
        if win is not None and len(win) == 2:
            win_w, win_h = win
            if win_w > 0 and win_w != surf_w:
                mx = mx * surf_w / win_w
            if win_h > 0 and win_h != surf_h:
                my = my * surf_h / win_h

        # Clamp to surface pixel range [0, surf_w-1] × [0, surf_h-1]
        if mx < 0:
            mx = 0
        elif mx >= surf_w:
            mx = surf_w - 1
        if my < 0:
            my = 0
        elif my >= surf_h:
            my = surf_h - 1

        px = int(mx) // self._cell_w + 1
        py = int(my) // self._cell_h + 1

        if px < 1:
            px = 1
        elif px > self._cols:
            px = self._cols
        if py < 1:
            py = 1
        elif py > self._rows:
            py = self._rows

        return px, py

    def _get_window_size(self):
        """
        Размер реального OS-окна (w, h) или None, если неизвестен.
        Переопределяется каждым GPU для HiDPI-коррекции координат мыши.
        """
        return None
    
    def _push_touch(self, px, py, btn):
        from core import computer
        scr = self._screen_addr or "screen"
        self._mouse_btn = btn
        self._mouse_lx, self._mouse_ly = px, py
        computer.push_signal("touch", scr, px, py, btn, PLAYER_NAME)

    def _push_drop(self, px, py, btn):
        from core import computer
        scr = self._screen_addr or "screen"
        computer.push_signal("drop", scr, px, py, btn, PLAYER_NAME)
        self._mouse_btn = None

    def _push_drag(self, px, py, btn):
        from core import computer
        scr = self._screen_addr or "screen"
        computer.push_signal("drag", scr, px, py, btn, PLAYER_NAME)
        self._mouse_lx, self._mouse_ly = px, py

    def _push_scroll(self, px, py, direction):
        from core import computer
        scr = self._screen_addr or "screen"
        computer.push_signal("scroll", scr, px, py, direction, PLAYER_NAME)

    # ── GPU API ────────────────────────────────────────────────────────
    def bind(self, *_):
        return False, "This is static bound gpu"

    def get_screen(self, *_):
        return self._screen_addr

    def _nearest_palette_color(self, rgb):
        """Находит ближайший индекс в 16-цветной палитре."""
        best_idx = 0
        best_dist = float('inf')
        r, g, b = rgb
        for i, (pr, pg, pb) in enumerate(self._palette):
            dist = (r - pr)**2 + (g - pg)**2 + (b - pb)**2
            if dist < best_dist:
                best_dist = dist
                best_idx = i
        return best_idx

    def _remap_colors_for_depth(self, old_depth, new_depth):
        """Пересчитывает цвета всех ячеек при смене глубины."""
        if old_depth == new_depth:
            return

        def map_color(rgb):
            if new_depth == 8:
                return rgb
            elif new_depth == 4:
                return self._palette[self._nearest_palette_color(rgb)]
            else: # 1-bit (монохром)
                r, g, b = rgb
                return (255, 255, 255) if (r + g + b) > 384 else (0, 0, 0)

        for y in range(self._rows):
            for x in range(self._cols):
                self._fbuffer[y][x] = map_color(self._fbuffer[y][x])
                self._bbuffer[y][x] = map_color(self._bbuffer[y][x])
        
        self._fg_rgb = map_color(self._fg_rgb)
        self._bg_rgb = map_color(self._bg_rgb)
        self._redraw_all()

    def set_background(self, color, is_palette=False, *_):
        old_rgb = _int_from_rgb(self._bg_rgb)
        old_pal = self._bg_pal_idx if self._bg_is_palette else None
        
        if is_palette and 0 <= color < 16:
            self._bg_pal_idx = int(color)
            self._bg_is_palette = True
            self._bg_rgb = self._palette[self._bg_pal_idx]
        else:
            self._bg_is_palette = False
            rgb = _rgb_from_int(color)
            if self._depth < 8:
                idx = self._nearest_palette_color(rgb)
                self._bg_rgb = self._palette[idx] if self._depth == 4 else ((255,255,255) if sum(rgb)>384 else (0,0,0))
            else:
                self._bg_rgb = rgb
        return old_rgb, old_pal

    def set_foreground(self, color, is_palette=False, *_):
        old_rgb = _int_from_rgb(self._fg_rgb)
        old_pal = self._fg_pal_idx if self._fg_is_palette else None
        
        if is_palette and 0 <= color < 16:
            self._fg_pal_idx = int(color)
            self._fg_is_palette = True
            self._fg_rgb = self._palette[self._fg_pal_idx]
        else:
            self._fg_is_palette = False
            rgb = _rgb_from_int(color)
            if self._depth < 8:
                idx = self._nearest_palette_color(rgb)
                self._fg_rgb = self._palette[idx] if self._depth == 4 else ((255,255,255) if sum(rgb)>384 else (0,0,0))
            else:
                self._fg_rgb = rgb
        return old_rgb, old_pal

    def get_background(self, *_):
        if self._bg_is_palette:
            return self._bg_pal_idx, True
        return _int_from_rgb(self._bg_rgb), False

    def get_foreground(self, *_):
        if self._fg_is_palette:
            return self._fg_pal_idx, True
        return _int_from_rgb(self._fg_rgb), False

    def get_palette_color(self, index, *_):
        try:
            idx = int(index) & 15
        except (TypeError, ValueError):
            idx = 0
        return _int_from_rgb(self._palette[idx])

    def set_palette_color(self, index, color, *_):
        try:
            idx = int(index) & 15
            rgb = _rgb_from_int(color)
        except (TypeError, ValueError):
            return None
        old = _int_from_rgb(self._palette[idx])
        self._palette[idx] = rgb
        
        # Если текущий цвет привязан к палитре — обновляем
        if self._fg_is_palette and self._fg_pal_idx == idx:
            self._fg_rgb = rgb
        if self._bg_is_palette and self._bg_pal_idx == idx:
            self._bg_rgb = rgb
            
        self._redraw_all()
        return old

    def max_depth(self, *_): return 8

    def set_depth(self, depth, *_):
        try:
            new_depth = int(depth)
        except (TypeError, ValueError):
            return False
        if new_depth not in (1, 4, 8):
            return False
        old = self._depth
        self._depth = new_depth
        self._remap_colors_for_depth(old, new_depth)
        return True

    def get_depth(self, *_):
        return self._depth

    def max_resolution(self, *_):
        if not self._allow_resize:
            return self._cols, self._rows
        mc = self._max_cols if self._max_cols is not None else self._cols
        mr = self._max_rows if self._max_rows is not None else self._rows
        return mc, mr

    def get_resolution(self, *_):
        return self._cols, self._rows

    def get_viewport(self, *_):
        return self._cols, self._rows

    def set_viewport(self, w, h, *_):
        return self._cols, self._rows

    def set_resolution(self, w, h, *_):
        if self._quit:
            return self._cols, self._rows
        if not self._allow_resize:
            return self._cols, self._rows
        try:
            new_w = int(w)
            new_h = int(h)
        except (TypeError, ValueError):
            return self._cols, self._rows
        if new_w < 1 or new_h < 1:
            return self._cols, self._rows

        mc = self._max_cols if self._max_cols is not None else self._cols
        mr = self._max_rows if self._max_rows is not None else self._rows

        clamped_w = max(1, min(new_w, mc, HARD_MAX_COLS))
        clamped_h = max(1, min(new_h, mr, HARD_MAX_ROWS))

        if clamped_w == self._cols and clamped_h == self._rows:
            return self._cols, self._rows

        old_w, old_h = self._cols, self._rows
        old_t, old_b, old_f = self._tbuffer, self._bbuffer, self._fbuffer

        self._alloc_buffers(clamped_w, clamped_h)
        self._clear_selection()

        try:
            self._on_resolution_changed()
        except Exception:
            logger.exception("_on_resolution_changed failed")
            self._cols, self._rows = old_w, old_h
            self._tbuffer, self._bbuffer, self._fbuffer = old_t, old_b, old_f
            return self._cols, self._rows

        logger.info("setResolution: %dx%d -> %dx%d",
                    old_w, old_h, self._cols, self._rows)
        self._dirty = True

        # Отправляем сигнал screen_resized (как в OCEmu)
        try:
            from core import computer as _computer
            if self._screen_addr:
                _computer.push_signal("screen_resized", self._screen_addr, self._cols, self._rows)
        except Exception:
            pass

        return self._cols, self._rows

    def _on_resolution_changed(self):
        self._rebuild_surface()
        self._redraw_all()

    def _rebuild_surface(self):
        pass

    # ── Buffer-based API ───────────────────────────────────────────────
    def get(self, x, y, *_):
        x -= 1; y -= 1
        if 0 <= y < self._rows and 0 <= x < self._cols:
            ch = self._tbuffer[y][x]
            fg = self._fbuffer[y][x]
            bg = self._bbuffer[y][x]
            # В OC возвращается 5 значений: символ, fg, bg, fg_pal_idx, bg_pal_idx.
            # Так как мы храним в буферах только RGB, возвращаем None для индексов палитры.
            return _to_lua(ch), _int_from_rgb(fg), _int_from_rgb(bg), None, None
        return " ", 0xFFFFFF, 0, None, None

    def set(self, x, y, value, vertical=False, *_):
        if self._quit:
            return True
        value = _from_lua(value)
        x -= 1; y -= 1
        fg = self._fg_rgb
        bg = self._bg_rgb
        if not vertical:
            cx = x
            for ch_ in value:
                if cx >= self._cols:
                    break
                if 0 <= y < self._rows and 0 <= cx < self._cols:
                    self._tbuffer[y][cx] = ch_
                    self._fbuffer[y][cx] = fg
                    self._bbuffer[y][cx] = bg
                    entry = self._font_data.get(ord(ch_))
                    is_wide = entry is not None and entry[1] == 16
                    if is_wide and cx + 1 < self._cols:
                        self._tbuffer[y][cx + 1] = "\x00"
                        self._fbuffer[y][cx + 1] = fg
                        self._bbuffer[y][cx + 1] = bg
                        self._paint_cell(cx, y)
                        cx += 2
                    else:
                        self._paint_cell(cx, y)
                        cx += 1
                else:
                    cx += 1
        else:
            cy = y
            for ch_ in value:
                if cy >= self._rows:
                    break
                if 0 <= cy < self._rows and 0 <= x < self._cols:
                    self._tbuffer[cy][x] = ch_
                    self._fbuffer[cy][x] = fg
                    self._bbuffer[cy][x] = bg
                    self._paint_cell(x, cy)
                cy += 1
        self._dirty = True
        return True

    def copy(self, x, y, w, h, tx, ty, *_):
        """
        gpu.copy(x, y, w, h, tx, ty)

        x, y     — 1-based source top-left
        w, h     — размер источника
        tx, ty   — ОТНОСИТЕЛЬНОЕ смещение (дельта), не координата.
                   Целевая позиция: (x + tx, y + ty) в 1-based.

        Проблемы, которые здесь решены:
          * tx/ty могут быть отрицательными (scroll вверх/влево);
          * source и target могут перекрываться — надо сначала снять
            снапшот, потом записывать;
          * источник может быть частично за экраном — лишнее отсекается,
            но построчно, без сдвига enumerate().
        """
        if self._quit:
            return True

        try:
            src_x = int(x) - 1
            src_y = int(y) - 1
            tx    = int(tx)
            ty    = int(ty)
            w     = int(w)
            h     = int(h)
        except (TypeError, ValueError):
            return True

        if w <= 0 or h <= 0:
            return True

        cols = self._cols
        rows = self._rows

        # ── 1. Снапшот источника (учитываем только то, что попадёт в цель) ──
        # Для каждой строки i из [0..h-1]:
        #   source row = src_y + i
        #   target row = src_y + ty + i
        # Если target row за пределами — строку не берём.
        # Колонки: source col идёт с src_x до src_x + w - 1.
        #         target col для source col s = s + tx.
        # Оставляем только те s, для которых 0 <= s + tx < cols.
        snapshot = []
        for i in range(h):
            sy = src_y + i
            dy = src_y + ty + i
            if not (0 <= sy < rows) or not (0 <= dy < rows):
                snapshot.append(None)
                continue

            # Диапазон source columns, которые попадут в цель.
            # d = s + tx, нужно 0 <= d < cols  →  -tx <= s < cols - tx.
            lo = max(0, src_x, -tx)
            hi = min(cols, src_x + w, cols - tx)
            if hi <= lo:
                snapshot.append(None)
                continue

            snapshot.append((
                lo,             # source column, с которой начинается копирование
                dy,             # target row
                # Копируем значения, а не срезы, чтобы избежать мутаций:
                [self._tbuffer[sy][c] for c in range(lo, hi)],
                [self._bbuffer[sy][c] for c in range(lo, hi)],
                [self._fbuffer[sy][c] for c in range(lo, hi)],
            ))

        # ── 2. Запись снапшота в цель ────────────────────────────────────────
        for item in snapshot:
            if item is None:
                continue
            s_lo, dy, rt, rb, rf = item
            # Первая целевая колонка для s_lo = s_lo + tx
            d0 = s_lo + tx
            n = len(rt)
            for k in range(n):
                dx = d0 + k
                if 0 <= dx < cols:
                    self._tbuffer[dy][dx] = rt[k]
                    self._bbuffer[dy][dx] = rb[k]
                    self._fbuffer[dy][dx] = rf[k]
                    self._paint_cell(dx, dy)

        self._dirty = True
        return True
    
    def fill(self, x, y, w, h, ch_, *_):
        if self._quit:
            return True
        ch_ = _from_lua(ch_)
        ch_ = ch_[0] if ch_ else " "
        x -= 1; y -= 1
        fg = self._fg_rgb
        bg = self._bg_rgb
        entry = self._font_data.get(ord(ch_))
        is_wide = entry is not None and entry[1] == 16
        step = 2 if is_wide else 1
        for row in range(y, y + h):
            if not (0 <= row < self._rows):
                continue
            cx = x
            while cx < x + w:
                if 0 <= cx < self._cols:
                    self._tbuffer[row][cx] = ch_
                    self._fbuffer[row][cx] = fg
                    self._bbuffer[row][cx] = bg
                    if is_wide and cx + 1 < self._cols and cx + 1 < x + w:
                        self._tbuffer[row][cx + 1] = "\x00"
                        self._fbuffer[row][cx + 1] = fg
                        self._bbuffer[row][cx + 1] = bg
                    self._paint_cell(cx, row)
                cx += step
        self._dirty = True
        return True

    def as_proxy(self):
        return {
            "bind":             self.bind,
            "getScreen":        self.get_screen,
            "setBackground":    self.set_background,
            "setForeground":    self.set_foreground,
            "getBackground":    self.get_background,
            "getForeground":    self.get_foreground,
            "getPaletteColor":  self.get_palette_color,
            "setPaletteColor":  self.set_palette_color,
            "maxDepth":         self.max_depth,
            "setDepth":         self.set_depth,
            "getDepth":         self.get_depth,
            "maxResolution":    self.max_resolution,
            "getResolution":    self.get_resolution,
            "getViewport":      self.get_viewport,
            "setViewport":      self.set_viewport,
            "setResolution":    self.set_resolution,
            "get":              self.get,
            "set":              self.set,
            "copy":             self.copy,
            "fill":             self.fill,
        }

    # ── Абстрактные ────────────────────────────────────────────────────
    def _init_window(self):
        raise NotImplementedError

    def _paint_cell(self, x, y):
        raise NotImplementedError

    def _redraw_all(self):
        raise NotImplementedError

    def _flush(self):
        raise NotImplementedError

    def pump(self):
        raise NotImplementedError

    def _teardown(self):
        raise NotImplementedError


# ── Прокси компонентов ────────────────────────────────────────────────────────

class KeyboardComponent:
    def __init__(self, gpu: BaseGPU):
        self._gpu = gpu

    def is_key_down(self, code, *_):
        return code in self._gpu._pressed_keys

    def as_proxy(self):
        return {"isKeyDown": self.is_key_down}


class ScreenComponent:
    def __init__(self, gpu: BaseGPU, kb_addr):
        self._gpu = gpu
        self._kb_addr = kb_addr
        self._touch_inverted = False
        self._precise = False
        self._on = True

    def get_keyboards(self, *_):     return [self._kb_addr]
    def is_touch_mode(self, *_):     return True
    def get_touch_mode(self, *_):    return True
    
    def is_touch_mode_inverted(self, *_):
        return self._touch_inverted

    def set_touch_mode_inverted(self, val, *_):
        self._touch_inverted = bool(val)
        return True

    def is_precise(self, *_):
        return self._precise

    def set_precise(self, val, *_):
        self._precise = bool(val)
        return True

    def turn_on(self, *_):
        old = self._on
        self._on = True
        return old

    def turn_off(self, *_):
        old = self._on
        self._on = False
        return old

    def is_on(self, *_):
        return self._on

    def get_aspect_ratio(self, *_):
        # В OpenComputers этот метод возвращает количество блоков (экранов) по X и Y,
        # а НЕ размеры ячейки в пикселях. Иначе MineOS ломает координаты мыши.
        return 1, 1

    def as_proxy(self):
        return {
            "getKeyboards":         self.get_keyboards,
            "isTouchMode":          self.is_touch_mode,
            "getTouchMode":         self.get_touch_mode,
            "isTouchModeInverted":  self.is_touch_mode_inverted,
            "setTouchModeInverted": self.set_touch_mode_inverted,
            "isPrecise":            self.is_precise,
            "setPrecise":           self.set_precise,
            "turnOn":               self.turn_on,
            "turnOff":              self.turn_off,
            "isOn":                 self.is_on,
            "getAspectRatio":       self.get_aspect_ratio,
        }