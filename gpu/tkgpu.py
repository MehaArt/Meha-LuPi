"""
tkgpu.py — GPU на Tkinter (Python stdlib).

Поддерживает выделение текста Shift+drag, Ctrl+C, Ctrl+V.
"""
from __future__ import annotations
import time
import base64
import logging

try:
    import tkinter as tk
    _TK_AVAILABLE = True
except ImportError:
    tk = None
    _TK_AVAILABLE = False

from core import component as component_module
from core import computer

from gpu.base import (
    BaseGPU, KeyboardComponent, ScreenComponent,
    load_font_hex, CTRL_SCAN,
    DEFAULT_COLS, DEFAULT_ROWS, DEFAULT_FPS,
)

logger = logging.getLogger("lupi.tkgpu")


_KEYSYM_MAP = {
    'Return':    (13, 28),
    'KP_Enter':  (13, 28),
    'Tab':       (9, 15),
    'BackSpace': (8, 14),
    'Escape':    (27, 1),
    'Up':        (-1, 200),
    'Down':      (-1, 208),
    'Left':      (-1, 203),
    'Right':     (-1, 205),
    'Home':      (-1, 199),
    'End':       (-1, 207),
    'Prior':     (-1, 201),
    'Next':      (-1, 209),
    'Delete':    (-1, 211),
    'Insert':    (-1, 210),
    'F1':        (-1, 59),
    'F2':        (-1, 60),
    'F3':        (-1, 61),
    'F4':        (-1, 62),
    'F5':        (-1, 63),
    'F6':        (-1, 64),
    'F7':        (-1, 65),
    'F8':        (-1, 66),
    'F9':        (-1, 67),
    'F10':       (-1, 68),
    'F11':       (-1, 87),
    'F12':       (-1, 88),
    'Shift_L':   (-1, 42),
    'Shift_R':   (-1, 54),
    'Control_L': (-1, 29),
    'Control_R': (-1, 157),
    'Alt_L':     (-1, 56),
    'Alt_R':     (-1, 184),
}

_MODIFIER_KEYSYMS = {
    'Shift_L', 'Shift_R', 'Control_L', 'Control_R',
    'Alt_L', 'Alt_R', 'Super_L', 'Super_R',
    'Caps_Lock', 'Num_Lock',
}


class TkinterGPU(BaseGPU):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._root = None
        self._canvas = None
        self._screen_image = None
        self._canvas_image_id = None

        self._pixel_buf = bytearray()
        self._glyph_row_cache = {}
        self._dirty_rows: set[int] = set()

    # ── Инициализация ──────────────────────────────────────────────────
    def _init_window(self):
        if not _TK_AVAILABLE:
            raise RuntimeError("tkinter not available")

        self._root = tk.Tk()
        self._root.title("LuPI — Tkinter GPU")
        self._root.resizable(False, False)

        w = self._cell_w * self._cols
        h = self._cell_h * self._rows

        self._canvas = tk.Canvas(self._root, width=w, height=h,
                                 highlightthickness=0, bd=0,
                                 bg="#000000")
        self._canvas.pack(fill="both", expand=True)
        self._canvas.focus_set()

        self._screen_image = tk.PhotoImage(master=self._root, width=w, height=h)
        self._canvas_image_id = self._canvas.create_image(
            0, 0, image=self._screen_image, anchor="nw"
        )

        self._root.bind("<KeyPress>",   lambda e: self._on_key(e, True))
        self._root.bind("<KeyRelease>", lambda e: self._on_key(e, False))

        self._canvas.bind("<Button-1>",        lambda e: self._on_mouse(e, 0, True))
        self._canvas.bind("<ButtonRelease-1>", lambda e: self._on_mouse(e, 0, False))
        self._canvas.bind("<Button-3>",        lambda e: self._on_mouse(e, 1, True))
        self._canvas.bind("<ButtonRelease-3>", lambda e: self._on_mouse(e, 1, False))
        self._canvas.bind("<Motion>",          self._on_motion)
        self._canvas.bind("<Button-4>",        lambda e: self._on_scroll(e,  1))
        self._canvas.bind("<Button-5>",        lambda e: self._on_scroll(e, -1))
        self._canvas.bind("<MouseWheel>",      self._on_wheel)

        self._font_data = load_font_hex()
        self._rebuild_surface()
        self._redraw_all()

    # ── Буферы ─────────────────────────────────────────────────────────
    def _rebuild_surface(self):
        w = self._cell_w * self._cols
        h = self._cell_h * self._rows
        self._pixel_buf = bytearray(w * h * 3)
        self._glyph_row_cache.clear()

    def _get_window_size(self):
        if self._canvas is None:
            return None
        try:
            return (self._canvas.winfo_width(), self._canvas.winfo_height())
        except Exception:
            return None
        
    def _paint_cell(self, x, y):
        self._dirty_rows.add(y)

    def _redraw_all(self):
        self._dirty_rows = set(range(self._rows))
        self._flush()

    def _get_glyph_rows(self, cp, fg, bg, cells_w):
        key = (cp, fg, bg, cells_w)
        cached = self._glyph_row_cache.get(key)
        if cached is not None:
            return cached

        target_w = cells_w * self._cell_w
        target_h = self._cell_h

        fg_b = bytes(fg)
        bg_b = bytes(bg)

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
                row_bytes = bytearray(target_w * 3)
                off = 0
                for col in range(target_w):
                    src_col = (col * src_w) // target_w
                    if src_line[src_col]:
                        row_bytes[off:off+3] = fg_b
                    else:
                        row_bytes[off:off+3] = bg_b
                    off += 3
                rows.append(bytes(row_bytes))

        self._glyph_row_cache[key] = rows
        return rows

    def _render_cell_row(self, y_cell):
        cw = self._cell_w
        ch_ = self._cell_h
        cols = self._cols
        w = cw * cols
        buf = self._pixel_buf
        y_pix = y_cell * ch_

        x_cell = 0
        while x_cell < cols:
            ch = self._tbuffer[y_cell][x_cell]
            if ch == "\x00":
                x_cell += 1
                continue

            fg = self._fbuffer[y_cell][x_cell]
            bg = self._bbuffer[y_cell][x_cell]

            bg = self._apply_selection_color(bg, x_cell, y_cell)

            cp = ord(ch) if ch and ch != " " else 0
            cells_w = 1
            if cp:
                bm = self._glyph_bitmap(cp)
                if bm is not None and bm[1] == 16 and x_cell + 1 < cols:
                    cells_w = 2

            x_pix = x_cell * cw
            target_w_px = cells_w * cw

            rows = self._get_glyph_rows(cp, fg, bg, cells_w)

            for row in range(ch_):
                row_base = ((y_pix + row) * w + x_pix) * 3
                buf[row_base:row_base + target_w_px * 3] = rows[row]

            x_cell += cells_w

    def _flush(self):
        if self._quit or not self._dirty_rows or self._screen_image is None:
            return

        for y_cell in self._dirty_rows:
            self._render_cell_row(y_cell)
        self._dirty_rows.clear()

        w = self._cell_w * self._cols
        h = self._cell_h * self._rows
        header = b"P6\n%d %d\n255\n" % (w, h)
        ppm = header + bytes(self._pixel_buf)

        try:
            self._screen_image.configure(data=ppm, format='ppm')
        except Exception:
            try:
                b64 = base64.b64encode(ppm).decode('ascii')
                self._screen_image.configure(data=b64, format='ppm')
            except Exception:
                logger.exception("tk flush failed")
                self._quit = True
                return

        self._dirty = False

    # ── События ────────────────────────────────────────────────────────

    def _on_key(self, event, down):
        keysym = event.keysym
        char = event.char
        state = int(getattr(event, "state", 0))
        ctrl = bool(state & 0x0004)
        shift = bool(state & 0x0001)

        if down:
            if ctrl and not shift and keysym.lower() == 'c':
                if self._has_selection():
                    if self._copy_selection_to_clipboard():
                        return "break"
            if ctrl and not shift and keysym == 'Insert':
                if self._has_selection():
                    if self._copy_selection_to_clipboard():
                        return "break"

            if (ctrl and keysym.lower() == 'v') \
                    or (keysym == 'Insert') \
                    or (shift and keysym == 'Insert'):
                if self._paste_from_clipboard():
                    self._clear_selection()
                return "break"

            if ctrl and shift and keysym.lower() == 'a':
                self._select_all()
                return "break"

            if keysym not in _MODIFIER_KEYSYMS and self._has_selection():
                self._clear_selection()

        if keysym in _KEYSYM_MAP:
            ascii_code, scan = _KEYSYM_MAP[keysym]
        elif char and len(char) == 1:
            code = ord(char)
            if 1 <= code <= 26:
                ascii_code = code
                scan = CTRL_SCAN.get(code, -1)
            elif code >= 32:
                # Передаем ЧИСЛОВОЙ код символа
                ascii_code, scan = code, -1
            else:
                return None
        else:
            return None

        self._push_key(ascii_code, scan, down)
        return None

    def _on_mouse(self, event, btn, down):
        if self._sel_dragging:
            if not down:
                self._sel_dragging = False
            return

        shift = bool(int(getattr(event, "state", 0)) & 0x0001)

        if down and shift:
            px, py = self._cell_from_pos(event.x, event.y)
            self._start_selection(px - 1, py - 1)
            self._sel_dragging = True
            return

        if down and not shift:
            if self._has_selection():
                self._clear_selection()

        px, py = self._cell_from_pos(event.x, event.y)
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
            px, py = self._cell_from_pos(event.x, event.y)
            self._update_selection(px - 1, py - 1)
            return
        if self._mouse_btn is None:
            return
        px, py = self._cell_from_pos(event.x, event.y)
        if px != self._mouse_lx or py != self._mouse_ly:
            self._push_drag(px, py, self._mouse_btn)

    def _on_scroll(self, event, direction):
        px, py = self._cell_from_pos(event.x, event.y)
        self._push_scroll(px, py, direction)

    def _on_wheel(self, event):
        direction = 1 if event.delta > 0 else -1
        px, py = self._cell_from_pos(event.x, event.y)
        self._push_scroll(px, py, direction)

    # ── Pump / teardown ────────────────────────────────────────────────
    def _render_if_due(self):
        if not self._dirty_rows:
            return
        now = time.monotonic()
        if now - self._last_render < 1.0 / self._fps:
            return
        self._flush()
        self._last_render = now

    def pump(self):
        if self._quit or self._root is None:
            return
        try:
            self._root.update()
        except Exception:
            logger.exception("tk update failed")
            self._quit = True
            return
        self._render_if_due()

    def pump_block(self, timeout):
        """
        tkinter не предоставляет блокирующего wait()-а с таймаутом на
        уровне Python. Поэтому делаем update() и уходим в обычный sleep —
        всё равно эффективнее, чем крутить update() в tight loop.
        """
        if self._quit or self._root is None:
            return
        try:
            self._root.update()
        except Exception:
            logger.exception("tk update failed")
            self._quit = True
            return
        # Дробим, чтобы в худшем случае не переспать событие.
        time.sleep(min(timeout, 0.02))
        self._render_if_due()

    def _teardown(self):
        self._quit = True
        try:
            if self._root is not None:
                self._root.destroy()
        except Exception:
            pass
        self._root = None
        self._canvas = None
        self._screen_image = None

    def _on_resolution_changed(self):
        if self._root is None or self._canvas is None:
            return
        w = self._cell_w * self._cols
        h = self._cell_h * self._rows
        try:
            self._canvas.config(width=w, height=h)
            new_img = tk.PhotoImage(master=self._root, width=w, height=h)
            self._screen_image = new_img
            if self._canvas_image_id is not None:
                self._canvas.itemconfig(self._canvas_image_id, image=new_img)
            else:
                self._canvas_image_id = self._canvas.create_image(
                    0, 0, image=new_img, anchor="nw")
        except Exception:
            logger.exception("tk resize failed")
            raise
        self._rebuild_surface()
        self._dirty_rows = set(range(self._rows))
        self._flush()


_instance = None


def start(cols=DEFAULT_COLS, rows=DEFAULT_ROWS, scale=1.0, cell_size=None,
          fps=DEFAULT_FPS, allow_resize=False, max_cols=None, max_rows=None):
    global _instance
    if not _TK_AVAILABLE:
        return None, "tkinter not available (install python3-tk)"
    gpu = TkinterGPU(cols=cols, rows=rows, scale=scale,
                     cell_size=cell_size, fps=fps,
                     allow_resize=allow_resize,
                     max_cols=max_cols, max_rows=max_rows)
    try:
        gpu._init_window()
    except Exception as e:
        logger.error("Cannot init tkinter GPU: %s", e)
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
        "TkinterGPU: %s (grid %dx%d, cell %dx%d, win %dx%d, fps %d, "
        "24-bit RGB, allow_resize=%s)",
        gpu_addr, gpu._cols, gpu._rows, gpu._cell_w, gpu._cell_h,
        gpu._cell_w * gpu._cols, gpu._cell_h * gpu._rows,
        gpu._fps, gpu._allow_resize)
    return gpu_addr


def get_instance():
    return _instance