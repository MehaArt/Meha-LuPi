"""
pygamegpu.py — пиксельный GPU на pygame.

Поддерживает выделение текста Shift+drag, Ctrl+C / Ctrl+Insert (copy),
Ctrl+V / Insert / Shift+Insert (paste), Ctrl+Shift+A (select all).

В idle-режиме использует pygame.event.wait(timeout), который на Windows
уходит в MsgWaitForMultipleObjects — поток реально усыпляется ядром,
а не крутится в PeekMessage-loop. Это снимает фоновую нагрузку CPU.
"""
from __future__ import annotations
import time
import logging
import pygame

from core import component as component_module
from core import computer

from gpu.base import (
    BaseGPU, KeyboardComponent, ScreenComponent,
    load_font_hex,
    DEFAULT_COLS, DEFAULT_ROWS, DEFAULT_FPS,
    HARD_MAX_COLS, HARD_MAX_ROWS,
    DESKTOP_MARGIN_W, DESKTOP_MARGIN_H,
)

logger = logging.getLogger("lupi.pygamegpu")


_KEY_MAP = {
    pygame.K_RETURN:    (13, 28),
    pygame.K_KP_ENTER:  (13, 28),
    pygame.K_TAB:       (9, 15),
    pygame.K_BACKSPACE: (8, 14),
    pygame.K_ESCAPE:    (27, 1),
    pygame.K_UP:        (-1, 200),
    pygame.K_DOWN:      (-1, 208),
    pygame.K_LEFT:      (-1, 203),
    pygame.K_RIGHT:     (-1, 205),
    pygame.K_HOME:      (-1, 199),
    pygame.K_END:       (-1, 207),
    pygame.K_PAGEUP:    (-1, 201),
    pygame.K_PAGEDOWN:  (-1, 209),
    pygame.K_DELETE:    (-1, 211),
    pygame.K_INSERT:    (-1, 210),
    pygame.K_F1:        (-1, 59),
    pygame.K_F2:        (-1, 60),
    pygame.K_F3:        (-1, 61),
    pygame.K_F4:        (-1, 62),
    pygame.K_F5:        (-1, 63),
    pygame.K_F6:        (-1, 64),
    pygame.K_F7:        (-1, 65),
    pygame.K_F8:        (-1, 66),
    pygame.K_F9:        (-1, 67),
    pygame.K_F10:       (-1, 68),
    pygame.K_F11:       (-1, 87),
    pygame.K_F12:       (-1, 88),
    pygame.K_LSHIFT:    (-1, 42),
    pygame.K_RSHIFT:    (-1, 54),
    pygame.K_LCTRL:     (-1, 29),
    pygame.K_RCTRL:     (-1, 157),
    pygame.K_LALT:      (-1, 56),
    pygame.K_RALT:      (-1, 184),
}

_CTRL_SCAN = {
    1:0x1E, 2:0x30, 3:0x2E, 4:0x20, 5:0x12, 6:0x21, 7:0x22,
    8:0x23, 9:0x17, 10:0x24, 11:0x25, 12:0x26, 13:0x32, 14:0x31,
    15:0x18, 16:0x19, 17:0x10, 18:0x13, 19:0x1F, 20:0x14, 21:0x16,
    22:0x2F, 23:0x11, 24:0x2D, 25:0x15, 26:0x2C,
}

_MODIFIER_KEYS = {
    pygame.K_LSHIFT, pygame.K_RSHIFT,
    pygame.K_LCTRL, pygame.K_RCTRL,
    pygame.K_LALT, pygame.K_RALT,
    pygame.K_LGUI, pygame.K_RGUI,
}

# Максимальное число ячеек в кэше композитных surface
_CELL_CACHE_LIMIT = 8192


class PygameGPU(BaseGPU):
    def __init__(self, require_ce=False, **kwargs):
        super().__init__(**kwargs)
        self._require_ce = require_ce
        self._surface = None
        self._display = None
        self._font = None
        self._glyph_cache = {}
        self._cell_cache = {}           # (cp, fg, bg, wide) -> Surface
        self._unhandled = set()
        self._dirty_cells: set[tuple[int, int]] = set()
        self._key_unicode_cache = {}

    # ── Инициализация ──────────────────────────────────────────────────
    def _surface_size(self):
        return self._cell_w * self._cols, self._cell_h * self._rows

    def _get_window_size(self):
        try:
            return pygame.display.get_window_size()
        except Exception:
            try:
                surf = pygame.display.get_surface()
                return surf.get_size() if surf else None
            except Exception:
                return None

    def _init_window(self):
        pygame.init()
        try:
            pygame.key.set_repeat(400, 30)
        except Exception:
            logger.warning("pygame.key.set_repeat not available")

        pygame.display.set_caption("LuPI — Pygame GPU")

        w, h = self._surface_size()
        self._display = pygame.display.set_mode((w, h))
        self._rebuild_surface()
        self._compute_max_from_desktop()

        for name in ("consolas", "dejavusansmono", "couriernew",
                     "liberationmono", "monospace", "courier"):
            try:
                f = pygame.font.SysFont(name, max(6, int(self._cell_h * 0.9)))
                if f:
                    self._font = f
                    break
            except Exception:
                continue
        if self._font is None:
            self._font = pygame.font.Font(None, max(6, int(self._cell_h * 0.9)))

        self._font_data = load_font_hex()
        self._redraw_all()

    def _compute_max_from_desktop(self):
        if self._max_cols is not None and self._max_rows is not None:
            return
        try:
            info = pygame.display.Info()
            dw = max(320, int(getattr(info, "current_w", 0) or 0))
            dh = max(240, int(getattr(info, "current_h", 0) or 0))
        except Exception:
            dw, dh = 1280, 720

        usable_w = max(320, dw - DESKTOP_MARGIN_W)
        usable_h = max(240, dh - DESKTOP_MARGIN_H)

        if self._max_cols is None:
            self._max_cols = min(max(self._cols, usable_w // self._cell_w),
                                 HARD_MAX_COLS)
        if self._max_rows is None:
            self._max_rows = min(max(self._rows, usable_h // self._cell_h),
                                 HARD_MAX_ROWS)

    def _rebuild_surface(self):
        w, h = self._surface_size()
        self._surface = pygame.Surface((w, h))
        self._surface.fill(self._bg_rgb)
        self._glyph_cache.clear()
        self._cell_cache.clear()
        self._dirty = True
        self._dirty_cells.clear()

    # ── Рендер ─────────────────────────────────────────────────────────
    def _get_glyph_surface(self, cp, fg_rgb, wide=False):
        cw = self._cell_w * (2 if wide else 1)
        ch = self._cell_h
        key = (cp, fg_rgb, cw, ch)
        cached = self._glyph_cache.get(key)
        if cached is not None:
            return cached

        bm = self._glyph_bitmap(cp)
        if bm is None:
            return None
        bitmap, width_px = bm

        src = pygame.Surface((width_px, 16), pygame.SRCALPHA)
        src.fill((0, 0, 0, 0))
        for row in range(16):
            src_line = bitmap[row]
            for col in range(width_px):
                if src_line[col]:
                    src.set_at((col, row), fg_rgb)

        if (cw, ch) != (width_px, 16):
            src = pygame.transform.scale(src, (cw, ch))
        self._glyph_cache[key] = src
        return src

    def _get_cell_surface(self, cp, fg_rgb, bg_rgb, wide):
        """
        Готовая поверхность одной ячейки: bg-заливка + глиф (если есть).
        Кэшируется по (cp, fg, bg, wide), что превращает кадр из
        «fill+blit на каждую клетку» в «один blit на клетку».
        """
        key = (cp, fg_rgb, bg_rgb, wide)
        cached = self._cell_cache.get(key)
        if cached is not None:
            return cached

        cw = self._cell_w * (2 if wide else 1)
        ch = self._cell_h

        try:
            s = pygame.Surface((cw, ch))
            s.fill(bg_rgb)
        except pygame.error:
            return None

        if cp and cp != 0x20:
            glyph = self._get_glyph_surface(cp, fg_rgb, wide)
            if glyph is not None:
                try:
                    s.blit(glyph, (0, 0))
                except pygame.error:
                    pass
            else:
                # fallback: '?'
                q = self._get_glyph_surface(ord("?"), fg_rgb, False)
                if q is not None:
                    try:
                        s.blit(q, (0, 0))
                    except pygame.error:
                        pass
                elif self._font is not None:
                    try:
                        rendered = self._font.render("?", True, fg_rgb)
                        s.blit(rendered, (0, 0))
                    except Exception:
                        pass
                if cp > 0x2500 and cp not in self._unhandled:
                    self._unhandled.add(cp)
                    logger.warning("Unhandled glyph U+%04X — using '?'", cp)

        if len(self._cell_cache) >= _CELL_CACHE_LIMIT:
            self._cell_cache.clear()
        self._cell_cache[key] = s
        return s

    def _cell_rect(self, x, y):
        return pygame.Rect(x * self._cell_w, y * self._cell_h,
                           self._cell_w, self._cell_h)

    def _paint_cell(self, x, y):
        if self._quit or self._surface is None:
            return
        if not (0 <= x < self._cols and 0 <= y < self._rows):
            return
        self._dirty_cells.add((x, y))

    # ── Fallback (сохранён для совместимости / отладки) ────────────────
    def _draw_cell_fallback(self, rect, ch_, fg_rgb, bg_rgb):
        code = ord(ch_)
        q = self._get_glyph_surface(ord("?"), fg_rgb, wide=False)
        if q is not None:
            self._surface.blit(q, rect.topleft)
        else:
            try:
                self._surface.fill(bg_rgb, rect)
                self._surface.blit(self._font.render("?", True, fg_rgb),
                                   rect.topleft)
            except Exception:
                pass
        if code > 0x2500 and code not in self._unhandled:
            self._unhandled.add(code)
            logger.warning("Unhandled glyph U+%04X (%r) — using '?'", code, ch_)

    def _render_cell(self, x, y):
        """
        Отрисовка одной клетки «в лоб». Используется как эталон / резервный
        путь. В горячем пути (``_flush``) не вызывается — там батчинг.
        """
        ch_ = self._tbuffer[y][x]
        fg_rgb = self._fbuffer[y][x]
        bg_rgb = self._bbuffer[y][x]

        bg_rgb = self._apply_selection_color(bg_rgb, x, y)

        rect = self._cell_rect(x, y)

        if ch_ == "\x00":
            return

        try:
            self._surface.fill(bg_rgb, rect)
        except pygame.error:
            return

        if not ch_ or ch_ == " ":
            return

        cp = ord(ch_)
        entry = self._font_data.get(cp)
        is_wide = entry is not None and entry[1] == 16

        if is_wide and x + 1 < self._cols:
            try:
                self._surface.fill(bg_rgb, (rect.x + rect.w, rect.y, rect.w, rect.h))
            except pygame.error:
                return
            surf = self._get_glyph_surface(cp, fg_rgb, wide=True)
            if surf is not None:
                self._surface.blit(surf, rect.topleft)
                return

        surf = self._get_glyph_surface(cp, fg_rgb, wide=False)
        if surf is not None:
            self._surface.blit(surf, rect.topleft)
            return

        self._draw_cell_fallback(rect, ch_, fg_rgb, bg_rgb)

    def _redraw_all(self):
        if self._quit or self._surface is None:
            return
        self._dirty_cells = set((x, y) for y in range(self._rows)
                                       for x in range(self._cols))
        self._flush()

    def _flush(self):
        if self._quit or self._display is None or self._surface is None:
            return
        if not self._dirty_cells:
            self._dirty = False
            return

        dirty = self._dirty_cells
        self._dirty_cells = set()

        surf       = self._surface
        cw         = self._cell_w
        ch         = self._cell_h
        cols       = self._cols
        rows       = self._rows
        font_data  = self._font_data
        tb         = self._tbuffer
        fb         = self._fbuffer
        bb         = self._bbuffer
        get_cell   = self._get_cell_surface
        has_sel    = bool(self._selection_cells)
        apply_sel  = self._apply_selection_color

        min_x = 1 << 30
        min_y = 1 << 30
        max_x = -1
        max_y = -1

        blits = []
        append = blits.append

        for (x, y) in dirty:
            if x < 0 or x >= cols or y < 0 or y >= rows:
                continue
            cell = tb[y][x]
            if cell == "\x00":
                # continuation of a wide glyph — drawn by its origin cell
                continue

            fg = fb[y][x]
            bg = bb[y][x]
            if has_sel:
                bg = apply_sel(bg, x, y)

            px = x * cw
            py = y * ch

            cp = 0
            wide = False
            if cell and cell != " ":
                cp = ord(cell)
                entry = font_data.get(cp)
                if entry is not None and entry[1] == 16 and x + 1 < cols:
                    wide = True

            try:
                cell_surf = get_cell(cp, fg, bg, wide)
            except Exception:
                logger.exception("cell surface failed for (%d,%d)", x, y)
                cell_surf = None

            if cell_surf is not None:
                append((cell_surf, (px, py)))

            if x < min_x: min_x = x
            if x > max_x: max_x = x
            if y < min_y: min_y = y
            if y > max_y: max_y = y

        if max_x < 0:
            self._dirty = False
            return

        # Одна батч-операция (pygame 2.1+ → fblits, 2.0 → blits, fallback loop)
        if blits:
            try:
                surf.fblits(blits)
            except AttributeError:
                try:
                    surf.blits(blits)
                except AttributeError:
                    for s, p in blits:
                        surf.blit(s, p)
            except Exception:
                logger.exception("batched blit failed, per-cell fallback")
                for s, p in blits:
                    try:
                        surf.blit(s, p)
                    except pygame.error:
                        self._quit = True
                        return

        rect = pygame.Rect(
            min_x * cw, min_y * ch,
            (max_x - min_x + 1) * cw,
            (max_y - min_y + 1) * ch,
        )
        # Сохраняем поведение оригинала: чуть расширяем, если правый край
        # не упирается в границу экрана.
        if rect.right < cw * cols:
            rect.width += cw

        try:
            self._display.blit(surf, rect.topleft, rect)
            pygame.display.update(rect)
        except pygame.error:
            self._quit = True
            return

        self._dirty = False

    # ── Обработка событий ──────────────────────────────────────────────
    def _handle_event(self, ev):
        if ev.type == pygame.QUIT:
            computer.push_signal("quit")
            computer.shutdown()
        elif ev.type == pygame.KEYDOWN:
            self._on_key(ev, True)
        elif ev.type == pygame.KEYUP:
            self._on_key(ev, False)
        elif ev.type == pygame.MOUSEBUTTONDOWN:
            if ev.button in (4, 5):
                self._on_scroll(ev, 1 if ev.button == 4 else -1)
            else:
                self._on_mouse(ev, True)
        elif ev.type == pygame.MOUSEBUTTONUP:
            if ev.button in (4, 5):
                return
            self._on_mouse(ev, False)
        elif ev.type == pygame.MOUSEMOTION:
            self._on_motion(ev)

    def _render_if_due(self):
        if not self._dirty_cells:
            return
        now = time.monotonic()
        if now - self._last_render < 1.0 / self._fps:
            return
        self._flush()
        self._last_render = now

    def pump(self):
        """Non-blocking pump (rate-limited из computer._maybe_pump)."""
        if self._quit or self._surface is None:
            return
        try:
            if pygame.event.peek():
                for ev in pygame.event.get():
                    self._handle_event(ev)
        except pygame.error:
            self._quit = True
            return

        self._render_if_due()

    def pump_block(self, timeout):
        """
        Blocking pump. pygame.event.wait(timeout) на Windows использует
        MsgWaitForMultipleObjects, на Linux — poll/epoll на X11/Wayland —
        поток засыпает в ядре до появления события или до таймаута.
        """
        if self._quit or self._surface is None:
            return
        try:
            ev = pygame.event.wait(timeout=timeout)
            if ev.type != pygame.NOEVENT:
                self._handle_event(ev)
            for ev in pygame.event.get():
                self._handle_event(ev)
        except pygame.error:
            self._quit = True
            return

        self._render_if_due()

    # ── Клавиатура / мышь ──────────────────────────────────────────────
    def _on_key(self, ev, down):
        key, mods = ev.key, ev.mod
        ctrl = bool(mods & pygame.KMOD_CTRL)
        shift = bool(mods & pygame.KMOD_SHIFT)

        if down:
            if ctrl and not shift and (key == pygame.K_c or key == pygame.K_INSERT):
                if self._has_selection():
                    if self._copy_selection_to_clipboard():
                        return

            if (ctrl and key == pygame.K_v) \
                    or (key == pygame.K_INSERT) \
                    or (shift and key == pygame.K_INSERT):
                if self._paste_from_clipboard():
                    self._clear_selection()
                return

            if ctrl and shift and key == pygame.K_a:
                self._select_all()
                return

            if key not in _MODIFIER_KEYS and self._has_selection():
                self._clear_selection()

        if key in _KEY_MAP:
            ascii_code, scan = _KEY_MAP[key]
        elif ctrl and pygame.K_a <= key <= pygame.K_z:
            ascii_code = key - pygame.K_a + 1
            scan = _CTRL_SCAN.get(ascii_code, -1)
        elif down:
            ch = ev.unicode
            if ch:
                if len(ch) == 1 and 32 <= ord(ch) < 127:
                    ascii_code, scan = ord(ch), -1
                else:
                    ascii_code = ord(ch)
                    scan = -1
                self._key_unicode_cache[key] = (ascii_code, scan)
            else:
                return
        else:  # KEYUP
            cached = self._key_unicode_cache.pop(key, None)
            if cached is not None:
                ascii_code, scan = cached
            else:
                ch = ev.unicode
                if ch:
                    if len(ch) == 1 and 32 <= ord(ch) < 127:
                        ascii_code, scan = ord(ch), -1
                    else:
                        ascii_code = ord(ch)
                        scan = -1
                else:
                    return

        self._push_key(ascii_code, scan, down)

    def _on_mouse(self, ev, down):
        if self._sel_dragging:
            if not down:
                self._sel_dragging = False
            return

        mods = pygame.key.get_mods()
        shift = bool(mods & pygame.KMOD_SHIFT)

        if down and shift:
            px, py = self._cell_from_pos(*ev.pos)
            self._start_selection(px - 1, py - 1)
            self._sel_dragging = True
            return

        if down and not shift:
            if self._has_selection():
                self._clear_selection()

        btn = {1: 0, 3: 1}.get(ev.button, -1)
        if btn < 0:
            return
        px, py = self._cell_from_pos(*ev.pos)
        if down:
            if self._mouse_btn is not None:
                return
            self._push_touch(px, py, btn)
        else:
            if self._mouse_btn != btn:
                return
            self._push_drop(self._mouse_lx, self._mouse_ly, btn)

    def _on_motion(self, ev):
        if self._sel_dragging:
            px, py = self._cell_from_pos(*ev.pos)
            self._update_selection(px - 1, py - 1)
            return
        if self._mouse_btn is None:
            return
        px, py = self._cell_from_pos(*ev.pos)
        if px != self._mouse_lx or py != self._mouse_ly:
            self._push_drag(px, py, self._mouse_btn)

    def _on_scroll(self, ev, direction):
        px, py = self._cell_from_pos(*ev.pos)
        self._push_scroll(px, py, direction)

    # ── Ресайз / teardown ─────────────────────────────────────────────
    def _on_resolution_changed(self):
        try:
            self._display = pygame.display.set_mode(self._surface_size())
            self._rebuild_surface()
            self._redraw_all()
        except pygame.error as e:
            logger.error("setResolution failed: %s", e)
            raise

    def _teardown(self):
        self._quit = True
        try:
            pygame.quit()
        except Exception:
            pass


_instance = None


def start(cols=DEFAULT_COLS, rows=DEFAULT_ROWS, scale=1.0, cell_size=None,
          fps=DEFAULT_FPS, allow_resize=False, max_cols=None, max_rows=None,
          require_ce=False):
    global _instance
    gpu = PygameGPU(require_ce=require_ce, cols=cols, rows=rows, scale=scale,
                    cell_size=cell_size, fps=fps,
                    allow_resize=allow_resize,
                    max_cols=max_cols, max_rows=max_rows)
    try:
        gpu._init_window()
    except Exception as e:
        logger.error("Cannot init pygame GPU: %s", e)
        return None, str(e)
    _instance = gpu

    kb_addr = component_module.register(
        None, "keyboard", KeyboardComponent(gpu).as_proxy())
    gpu._keyboard_addr = kb_addr

    screen_addr = component_module.register(
        None, "screen", ScreenComponent(gpu, kb_addr).as_proxy())
    gpu._screen_addr = screen_addr

    gpu_addr = component_module.register(None, "gpu", gpu.as_proxy())

    computer.set_pump_hook(gpu.pump)
    computer.set_pump_block_hook(gpu.pump_block)
    computer.add_shutdown_hook(gpu._teardown)
    tag = "Pygame-CE" if require_ce else "Pygame"
    logger.info(
        "%sGPU: %s (grid %dx%d, cell %dx%d, win %s, fps %d, "
        "24-bit RGB, allow_resize=%s, max %sx%s)",
        tag, gpu_addr, gpu._cols, gpu._rows, gpu._cell_w, gpu._cell_h,
        gpu._surface.get_size(), gpu._fps, gpu._allow_resize,
        gpu._max_cols, gpu._max_rows)
    return gpu_addr


def get_instance():
    return _instance