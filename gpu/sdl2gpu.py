"""
sdl2gpu.py — чистый SDL2 рендер (PySDL2).

Поддерживает выделение текста Shift+drag, Ctrl+C, Ctrl+V.

В idle-режиме используется SDL_WaitEventTimeout — на Windows это
MsgWaitForMultipleObjects, на Linux — poll на X11/Wayland-сокете.
Поток засыпает в ядре, CPU не тратится на spin.
"""
from __future__ import annotations
import time
import ctypes
import logging

from core import component as component_module
from core import computer

from gpu.base import (
    BaseGPU, KeyboardComponent, ScreenComponent,
    load_font_hex, CTRL_SCAN,
    DEFAULT_COLS, DEFAULT_ROWS, DEFAULT_FPS,
    HARD_MAX_COLS, HARD_MAX_ROWS,
    DESKTOP_MARGIN_W, DESKTOP_MARGIN_H,
)

logger = logging.getLogger("lupi.sdl2gpu")

try:
    import sdl2
    _SDL2_AVAILABLE = True
except ImportError:
    sdl2 = None
    _SDL2_AVAILABLE = False


def _build_key_map():
    if not _SDL2_AVAILABLE:
        return {}
    return {
        sdl2.SDLK_RETURN:    (13, 28),
        sdl2.SDLK_KP_ENTER:  (13, 28),
        sdl2.SDLK_TAB:       (9, 15),
        sdl2.SDLK_BACKSPACE: (8, 14),
        sdl2.SDLK_ESCAPE:    (27, 1),
        sdl2.SDLK_UP:        (-1, 200),
        sdl2.SDLK_DOWN:      (-1, 208),
        sdl2.SDLK_LEFT:      (-1, 203),
        sdl2.SDLK_RIGHT:     (-1, 205),
        sdl2.SDLK_HOME:      (-1, 199),
        sdl2.SDLK_END:       (-1, 207),
        sdl2.SDLK_PAGEUP:    (-1, 201),
        sdl2.SDLK_PAGEDOWN:  (-1, 209),
        sdl2.SDLK_DELETE:    (-1, 211),
        sdl2.SDLK_INSERT:    (-1, 210),
        sdl2.SDLK_F1:        (-1, 59),
        sdl2.SDLK_F2:        (-1, 60),
        sdl2.SDLK_F3:        (-1, 61),
        sdl2.SDLK_F4:        (-1, 62),
        sdl2.SDLK_F5:        (-1, 63),
        sdl2.SDLK_F6:        (-1, 64),
        sdl2.SDLK_F7:        (-1, 65),
        sdl2.SDLK_F8:        (-1, 66),
        sdl2.SDLK_F9:        (-1, 67),
        sdl2.SDLK_F10:       (-1, 68),
        sdl2.SDLK_F11:       (-1, 87),
        sdl2.SDLK_F12:       (-1, 88),

        sdl2.SDLK_LSHIFT:    (-1, 42),
        sdl2.SDLK_RSHIFT:    (-1, 54),
        sdl2.SDLK_LCTRL:     (-1, 29),
        sdl2.SDLK_RCTRL:     (-1, 157),
        sdl2.SDLK_LALT:      (-1, 56),
        sdl2.SDLK_RALT:      (-1, 184),
        sdl2.SDLK_LGUI:      (-1, 219),
        sdl2.SDLK_RGUI:      (-1, 220),
    }

def _build_shift_symbols():
    if not _SDL2_AVAILABLE:
        return {}
    return {
        sdl2.SDLK_1: ord('!'), sdl2.SDLK_2: ord('@'),
        sdl2.SDLK_3: ord('#'), sdl2.SDLK_4: ord('$'),
        sdl2.SDLK_5: ord('%'), sdl2.SDLK_6: ord('^'),
        sdl2.SDLK_7: ord('&'), sdl2.SDLK_8: ord('*'),
        sdl2.SDLK_9: ord('('), sdl2.SDLK_0: ord(')'),
        sdl2.SDLK_MINUS:        ord('_'),
        sdl2.SDLK_EQUALS:       ord('+'),
        sdl2.SDLK_LEFTBRACKET:  ord('{'),
        sdl2.SDLK_RIGHTBRACKET: ord('}'),
        sdl2.SDLK_BACKSLASH:    ord('|'),
        sdl2.SDLK_SEMICOLON:    ord(':'),
        sdl2.SDLK_QUOTE:        ord('"'),
        sdl2.SDLK_COMMA:        ord('<'),
        sdl2.SDLK_PERIOD:       ord('>'),
        sdl2.SDLK_SLASH:        ord('?'),
        sdl2.SDLK_BACKQUOTE:    ord('~'),
    }


class SDL2GPU(BaseGPU):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._window = None
        self._renderer = None
        self._texture = None
        self._pixel_buf = None
        self._glyph_cache: dict = {}
        self._dirty_cells: set[tuple[int, int]] = set()
        self._key_map = _build_key_map()
        self._shift_symbols = _build_shift_symbols()
        self._last_key_sym = None
        self._last_key_scancode = -1
        self._key_unicode_cache = {}

    def _surface_size(self):
        return self._cell_w * self._cols, self._cell_h * self._rows

    def _get_window_size(self):
        if self._window is None:
            return None
        w = ctypes.c_int(0)
        h = ctypes.c_int(0)
        try:
            sdl2.SDL_GetWindowSize(self._window, ctypes.byref(w), ctypes.byref(h))
        except Exception:
            return None
        return (w.value, h.value)
    
    def _init_window(self):
        if not _SDL2_AVAILABLE:
            raise RuntimeError(
                "PySDL2 not available. Install with: pip install PySDL2"
            )

        if sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO) != 0:
            err = sdl2.SDL_GetError().decode('utf-8', errors='replace')
            raise RuntimeError(f"SDL_Init failed: {err}")

        w, h = self._surface_size()
        self._window = sdl2.SDL_CreateWindow(
            b"LuPI SDL2 GPU",
            sdl2.SDL_WINDOWPOS_CENTERED, sdl2.SDL_WINDOWPOS_CENTERED,
            w, h, sdl2.SDL_WINDOW_SHOWN,
        )
        if not self._window:
            err = sdl2.SDL_GetError().decode('utf-8', errors='replace')
            raise RuntimeError(f"SDL_CreateWindow failed: {err}")

        # Без SDL_RENDERER_PRESENTVSYNC — на многих драйверах он делает
        # SDL_RenderPresent spin-wait'ом до vsync. FPS-лимит мы уже
        # держим программно через self._fps.
        self._renderer = sdl2.SDL_CreateRenderer(
            self._window, -1,
            sdl2.SDL_RENDERER_ACCELERATED,
        )
        if not self._renderer:
            self._renderer = sdl2.SDL_CreateRenderer(
                self._window, -1, sdl2.SDL_RENDERER_SOFTWARE)
        if not self._renderer:
            err = sdl2.SDL_GetError().decode('utf-8', errors='replace')
            raise RuntimeError(f"SDL_CreateRenderer failed: {err}")

        self._texture = sdl2.SDL_CreateTexture(
            self._renderer,
            sdl2.SDL_PIXELFORMAT_ARGB8888,
            sdl2.SDL_TEXTUREACCESS_STREAMING,
            w, h,
        )
        if not self._texture:
            err = sdl2.SDL_GetError().decode('utf-8', errors='replace')
            raise RuntimeError(f"SDL_CreateTexture failed: {err}")

        try:
            sdl2.SDL_StartTextInput()
        except Exception:
            pass

        self._rebuild_surface()
        self._compute_max_from_desktop()

        self._font_data = load_font_hex()
        self._redraw_all()

    def _compute_max_from_desktop(self):
        if self._max_cols is not None and self._max_rows is not None:
            return
        try:
            dm = sdl2.SDL_DisplayMode()
            sdl2.SDL_GetCurrentDisplayMode(0, ctypes.byref(dm))
            dw = max(320, int(dm.w))
            dh = max(240, int(dm.h))
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
        self._pixel_buf = bytearray(w * h * 4)
        self._glyph_cache.clear()
        self._dirty = True

    def _get_glyph_rows(self, cp, fg, bg, cells_w):
        key = (cp, fg, bg, cells_w)
        cached = self._glyph_cache.get(key)
        if cached is not None:
            return cached

        target_w = cells_w * self._cell_w
        target_h = self._cell_h

        fg_b = bytes((fg[2], fg[1], fg[0], 0xFF))
        bg_b = bytes((bg[2], bg[1], bg[0], 0xFF))

        bm = self._glyph_bitmap(cp) if cp else None

        if bm is None:
            single = bg_b * target_w
            rows = [single] * target_h
        else:
            bitmap, src_w = bm
            rows = []
            for row in range(target_h):
                src_row = (row * 16) // target_h
                src_line = bitmap[src_row]
                row_bytes = bytearray(target_w * 4)
                off = 0
                for col in range(target_w):
                    src_col = (col * src_w) // target_w
                    if src_line[src_col]:
                        row_bytes[off:off + 4] = fg_b
                    else:
                        row_bytes[off:off + 4] = bg_b
                    off += 4
                rows.append(bytes(row_bytes))

        self._glyph_cache[key] = rows
        return rows

    def _paint_cell(self, x, y):
        self._dirty_cells.add((x, y))

    def _redraw_all(self):
        self._dirty_cells = set(
            (x, y) for y in range(self._rows) for x in range(self._cols)
        )
        self._flush()

    def _render_cell(self, x, y):
        ch = self._tbuffer[y][x]
        fg = self._fbuffer[y][x]
        bg = self._bbuffer[y][x]

        if ch == "\x00":
            return

        bg = self._apply_selection_color(bg, x, y)

        cp = ord(ch) if ch and ch != " " else 0

        cells_w = 1
        if cp:
            bm = self._glyph_bitmap(cp)
            if bm is not None and bm[1] == 16 and x + 1 < self._cols:
                cells_w = 2

        target_w_px = cells_w * self._cell_w
        rows = self._get_glyph_rows(cp, fg, bg, cells_w)

        W = self._cols * self._cell_w
        buf = self._pixel_buf
        x_pix = x * self._cell_w

        for row in range(self._cell_h):
            y_pix = y * self._cell_h + row
            row_base = (y_pix * W + x_pix) * 4
            buf[row_base:row_base + target_w_px * 4] = rows[row]

    def _flush(self):
        if self._quit or self._renderer is None or not self._dirty_cells:
            self._dirty = False
            return

        for (x, y) in self._dirty_cells:
            self._render_cell(x, y)
        self._dirty_cells.clear()

        w, h = self._surface_size()

        try:
            pixels_ptr = (ctypes.c_ubyte * len(self._pixel_buf)).from_buffer(
                self._pixel_buf)
            sdl2.SDL_UpdateTexture(self._texture, None, pixels_ptr, w * 4)
            sdl2.SDL_RenderClear(self._renderer)
            sdl2.SDL_RenderCopy(self._renderer, self._texture, None, None)
            sdl2.SDL_RenderPresent(self._renderer)
        except Exception:
            logger.exception("SDL2 render failed")
            self._quit = True
            return

        self._dirty = False

    # ── Обработка событий ──────────────────────────────────────────────
    def _handle_event(self, event) -> bool:
        """Возвращает False, если нужно прекратить цикл (quit)."""
        et = event.type
        if et == sdl2.SDL_QUIT:
            computer.push_signal("quit")
            computer.shutdown()
            return False
        elif et == sdl2.SDL_KEYDOWN:
            self._on_key(event, True)
        elif et == sdl2.SDL_KEYUP:
            self._on_key(event, False)
        elif et == sdl2.SDL_MOUSEBUTTONDOWN:
            self._on_mouse(event, True)
        elif et == sdl2.SDL_MOUSEBUTTONUP:
            self._on_mouse(event, False)
        elif et == sdl2.SDL_MOUSEMOTION:
            self._on_motion(event)
        elif et == sdl2.SDL_MOUSEWHEEL:
            self._on_wheel(event)
        elif et == sdl2.SDL_TEXTINPUT:
            self._on_textinput(event)
        elif et == sdl2.SDL_WINDOWEVENT:
            if event.window.event == sdl2.SDL_WINDOWEVENT_CLOSE:
                computer.push_signal("quit")
                computer.shutdown()
                return False
        return True

    def _render_if_due(self):
        if not self._dirty_cells:
            return
        now = time.monotonic()
        if now - self._last_render < 1.0 / self._fps:
            return
        self._flush()
        self._last_render = now

    def pump(self):
        """Non-blocking (rate-limited из computer._maybe_pump)."""
        if self._quit or self._window is None:
            return
        event = sdl2.SDL_Event()
        while sdl2.SDL_PollEvent(ctypes.byref(event)) != 0:
            if not self._handle_event(event):
                return
        self._render_if_due()

    def pump_block(self, timeout):
        """
        SDL_WaitEventTimeout блокирует поток в ядре до события или таймаута.
        На Windows использует MsgWaitForMultipleObjects, на Linux — poll
        на X11/Wayland-сокете.
        """
        if self._quit or self._window is None:
            return
        timeout_ms = int(max(1, timeout * 1000))
        event = sdl2.SDL_Event()
        got = sdl2.SDL_WaitEventTimeout(ctypes.byref(event), timeout_ms)
        if got > 0:
            if not self._handle_event(event):
                return
        elif got < 0:
            self._quit = True
            return
        # Слить остаток
        while sdl2.SDL_PollEvent(ctypes.byref(event)) != 0:
            if not self._handle_event(event):
                return
        self._render_if_due()

    def _on_mouse(self, event, down):
        if self._sel_dragging:
            if not down:
                self._sel_dragging = False
            return

        mod = sdl2.SDL_GetModState()
        shift = bool(mod & sdl2.KMOD_SHIFT)

        if down and shift:
            px, py = self._cell_from_pos(event.button.x, event.button.y)
            self._start_selection(px - 1, py - 1)
            self._sel_dragging = True
            return

        if down and not shift:
            if self._has_selection():
                self._clear_selection()

        btn_raw = event.button.button
        btn = {sdl2.SDL_BUTTON_LEFT: 0, sdl2.SDL_BUTTON_RIGHT: 1}.get(btn_raw, -1)
        if btn < 0:
            return
        px, py = self._cell_from_pos(event.button.x, event.button.y)
        if down:
            if self._mouse_btn is not None:
                return
            self._push_touch(px, py, btn)
        else:
            if self._mouse_btn != btn:
                return
            self._push_drop(self._mouse_lx, self._mouse_ly, btn)

    def _on_motion(self, event):
        if self._sel_dragging:
            px, py = self._cell_from_pos(event.motion.x, event.motion.y)
            self._update_selection(px - 1, py - 1)
            return
        if self._mouse_btn is None:
            return
        px, py = self._cell_from_pos(event.motion.x, event.motion.y)
        if px != self._mouse_lx or py != self._mouse_ly:
            self._push_drag(px, py, self._mouse_btn)

    def _on_textinput(self, event):
        """Обработка ввода текста (с учетом раскладки клавиатуры)."""
        try:
            text = event.text.text.decode('utf-8', errors='replace')
            for ch in text:
                code = ord(ch)
                # ВАЖНО: передаем -1 как scancode, чтобы OpenOS не путал
                # скан-коды SDL2 с PC/AT скан-кодами.
                self._push_key(code, -1, True)
                self._push_key(code, -1, False)
        except Exception:
            pass

    def _on_key(self, event, down):
        sym = event.key.keysym.sym
        mod = event.key.keysym.mod
        ctrl = bool(mod & sdl2.KMOD_CTRL)
        shift = bool(mod & sdl2.KMOD_SHIFT)

        if down:
            # ── Горячие клавиши ────────────────────────────────────
            if ctrl and not shift and (sym == sdl2.SDLK_c or sym == sdl2.SDLK_INSERT):
                if self._has_selection():
                    if self._copy_selection_to_clipboard(): return
            if (ctrl and sym == sdl2.SDLK_v) or (sym == sdl2.SDLK_INSERT) or (shift and sym == sdl2.SDLK_INSERT):
                if self._paste_from_clipboard():
                    self._clear_selection()
                return
            if ctrl and shift and sym == sdl2.SDLK_a:
                self._select_all()
                return

            _mods = (sdl2.SDLK_LSHIFT, sdl2.SDLK_RSHIFT,
                     sdl2.SDLK_LCTRL,  sdl2.SDLK_RCTRL,
                     sdl2.SDLK_LALT,   sdl2.SDLK_RALT)
            if sym not in _mods and self._has_selection():
                self._clear_selection()

        # ── Ctrl-комбинации (Ctrl+A, Ctrl+C и т.д.) ────────────────
        if ctrl and sdl2.SDLK_a <= sym <= sdl2.SDLK_z:
            ascii_code = sym - sdl2.SDLK_a + 1
            scan = CTRL_SCAN.get(ascii_code, -1)
            self._push_key(ascii_code, scan, down)
            return

        # ── Специальные клавиши (стрелки, F1-F12, Enter и т.д.) ────
        if sym in self._key_map:
            ascii_code, scan = self._key_map[sym]
            self._push_key(ascii_code, scan, down)
            return

        # ── Обычные печатные клавиши ───────────────────────────────
        # Для них всё делает _on_textinput. Здесь мы ничего не отправляем.
        return
        
    def _on_wheel(self, event):
        dy = event.wheel.y
        if dy == 0:
            return
        mx = ctypes.c_int(0)
        my = ctypes.c_int(0)
        sdl2.SDL_GetMouseState(ctypes.byref(mx), ctypes.byref(my))
        px, py = self._cell_from_pos(mx.value, my.value)
        direction = 1 if dy > 0 else -1
        self._push_scroll(px, py, direction)

    def _on_resolution_changed(self):
        w, h = self._surface_size()
        try:
            sdl2.SDL_SetWindowSize(self._window, w, h)
            if self._texture:
                sdl2.SDL_DestroyTexture(self._texture)
            self._texture = sdl2.SDL_CreateTexture(
                self._renderer,
                sdl2.SDL_PIXELFORMAT_ARGB8888,
                sdl2.SDL_TEXTUREACCESS_STREAMING,
                w, h,
            )
            if not self._texture:
                raise RuntimeError("SDL_CreateTexture failed on resize")
        except Exception:
            logger.exception("SDL2 resize failed")
            raise
        self._rebuild_surface()
        self._redraw_all()

    def _teardown(self):
        self._quit = True
        try:
            try:
                sdl2.SDL_StopTextInput()
            except Exception:
                pass
            if self._texture:
                sdl2.SDL_DestroyTexture(self._texture)
                self._texture = None
            if self._renderer:
                sdl2.SDL_DestroyRenderer(self._renderer)
                self._renderer = None
            if self._window:
                sdl2.SDL_DestroyWindow(self._window)
                self._window = None
            sdl2.SDL_Quit()
        except Exception:
            pass


_instance = None


def start(cols=DEFAULT_COLS, rows=DEFAULT_ROWS, scale=1.0, cell_size=None,
          fps=DEFAULT_FPS, allow_resize=False, max_cols=None, max_rows=None):
    global _instance
    if not _SDL2_AVAILABLE:
        return None, ("PySDL2 not installed. "
                      "Install: pip install PySDL2; "
                      "Linux runtime: sudo apt install libsdl2-2.0-0")
    gpu = SDL2GPU(cols=cols, rows=rows, scale=scale,
                  cell_size=cell_size, fps=fps,
                  allow_resize=allow_resize,
                  max_cols=max_cols, max_rows=max_rows)
    try:
        gpu._init_window()
    except Exception as e:
        logger.error("Cannot init SDL2 GPU: %s", e)
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
    logger.info(
        "SDL2GPU: %s (grid %dx%d, cell %dx%d, win %dx%d, fps %d, "
        "24-bit RGB, allow_resize=%s)",
        gpu_addr, gpu._cols, gpu._rows, gpu._cell_w, gpu._cell_h,
        gpu._cell_w * gpu._cols, gpu._cell_h * gpu._rows,
        gpu._fps, gpu._allow_resize)
    return gpu_addr


def get_instance():
    return _instance