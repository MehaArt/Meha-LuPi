#!/usr/bin/env python3
"""
initgui.py — лаунчер LuPI (v25 - FS Speed Limit).
"""
from __future__ import annotations
import os, sys, json, shlex, time, tempfile, threading, subprocess, io, atexit
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

HERE    = os.path.dirname(os.path.abspath(__file__))
INIT_PY = os.path.abspath(os.path.join(HERE, "init.py"))
CONFIG  = os.path.join(HERE, "lupigui.json")
IS_WIN  = os.name == "nt"
IS_MAC  = sys.platform == "darwin"

_temp_files = []
def _cleanup_temp():
    for f in _temp_files:
        try: os.remove(f)
        except Exception: pass
atexit.register(_cleanup_temp)

def _python_exe() -> str:
    exe = sys.executable or "python"
    if IS_WIN:
        exe = os.path.abspath(exe)
        exe = os.path.normpath(exe)
        d, f = os.path.split(exe)
        if f.lower() == "pythonw.exe":
            py = os.path.join(d, "python.exe")
            if os.path.isfile(py):
                return os.path.normpath(os.path.abspath(py))
    return exe

# ── Темы ──────────────────────────────────────────────────────────────────────
DARK = {
    "bg": "#1e1e1e", "fg": "#d4d4d4", "muted": "#808080", "border": "#3a3a3a",
    "btn_bg": "#3a3a3a", "btn_fg": "#ffffff", "btn_hover": "#505050",
    "primary": "#0e639c", "primary_hover": "#1177bb",
    "danger": "#c43e1c", "danger_hover": "#d94f2e",
    "entry_bg": "#2d2d2d", "entry_fg": "#d4d4d4",
    "select_bg": "#094771", "select_fg": "#ffffff",
    "log_bg": "#181818", "log_fg": "#d4d4d4",
    "tab_bg": "#2d2d2d", "tab_fg": "#9d9d9d",
    "tab_active_bg": "#1e1e1e", "tab_active_fg": "#ffffff",
    "error": "#f48771", "warn": "#dcdcaa", "info": "#4ec9b0", "cmd": "#569cd6",
    "status_bg": "#007acc", "status_fg": "#ffffff",
}
LIGHT = {
    "bg": "#f5f5f5", "fg": "#1e1e1e", "muted": "#6a6a6a", "border": "#c0c0c0",
    "btn_bg": "#e0e0e0", "btn_fg": "#1e1e1e", "btn_hover": "#d0d0d0",
    "primary": "#0066cc", "primary_hover": "#0077dd",
    "danger": "#cc3333", "danger_hover": "#dd4444",
    "entry_bg": "#ffffff", "entry_fg": "#1e1e1e",
    "select_bg": "#cce4f7", "select_fg": "#000000",
    "log_bg": "#ffffff", "log_fg": "#1e1e1e",
    "tab_bg": "#e8e8e8", "tab_fg": "#5a5a5a",
    "tab_active_bg": "#ffffff", "tab_active_fg": "#1e1e1e",
    "error": "#c41a16", "warn": "#a67400", "info": "#006400", "cmd": "#0055aa",
    "status_bg": "#0066cc", "status_fg": "#ffffff",
}

DEFAULTS = {
    "theme": "dark", "gpu": "pygame", "root": "openos", "ram": "8M",
    "mounts": [], "res_mode": "oc", "cols": 80, "rows": 25,
    "px_w": 640, "px_h": 400, "scale": 1, "fps": 30,
    "allow_resize": False, "bios": "bios/code.lua", "extra": "",
    "allow_beep": False, "allow_sounds": False,
    "fs_speed": "8M",
}

ROOT_OPTIONS = ["openos", "mineos", ""]
RAM_OPTIONS  = ["8M", "", "512K", "1M", "2M", "4M", "16M", "32M", "64M", "128M", "256M", "512M", "1G", "2G"]
FS_SPEED_OPTIONS = ["8M", "", "4K", "8K", "16K", "32K", "64K", "128K", "256K",
                    "512K", "1M", "2M", "4M", "16M", "32M", "64M", "128M",
                    "256M", "512M", "1G"]

def _detect_gpu() -> str:
    for mod, kind in (("pygame", "pygame"), ("sdl2", "sdl2")):
        try:
            __import__(mod)
            return kind
        except ImportError:
            pass
    try:
        import tkinter  # noqa
        return "tk"
    except ImportError:
        return "text"

def _int(v, d):
    try: return int(v)
    except (TypeError, ValueError): return d

def _load_font_hex() -> dict:
    paths = [
        "font.txt", "font.hex", os.path.join("root", "font.txt"), os.path.join("root", "font.hex"),
        os.path.join("openos", "font.txt"), os.path.join("openos", "font.hex"),
        os.path.join("assets", "font.txt"), os.path.join("assets", "font.hex"),
        os.path.join("bios", "font.txt"), os.path.join("bios", "font.hex"),
    ]
    data = {}
    for p in paths:
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if not line or ":" not in line: continue
                        cp_hex, data_hex = line.split(":", 1)
                        cp = int(cp_hex.strip(), 16)
                        raw = bytes.fromhex(data_hex.strip())
                        if len(raw) == 16:
                            data[cp] = (raw, 8)
                        elif len(raw) >= 32:
                            data[cp] = (raw[:32], 16)
                break
            except Exception:
                pass
    return data

# ══════════════════════════════════════════════════════════════════════════════
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LuPI Launcher")
        self.geometry("860x760")
        self.minsize(680, 520)

        self.cfg = self._load_cfg()
        self.theme = self.cfg.get("theme", "dark")
        self.colors = DARK if self.theme == "dark" else LIGHT
        self.mounts = list(self.cfg.get("mounts", []))

        self.proc = None
        self._monitor_id = None
        self._save_id = None
        self._running = False
        self._start_time = 0.0
        self._log_file_path = None

        self.v: dict[str, tk.Variable] = {}
        self.font_data = _load_font_hex()
        self._char_cache = {}

        self._build_ui()
        self._load_values()
        self._apply_theme()
        self._wire_autosave()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Control-Return>", lambda e: self._launch())
        self.bind("<F5>",             lambda e: self._launch())
        self.bind("<Control-s>",      lambda e: self._force_save())

        self._set_running(False)

    # ── Config ────────────────────────────────────────────────────────
    def _load_cfg(self) -> dict:
        cfg = dict(DEFAULTS)
        if os.path.isfile(CONFIG):
            try:
                with open(CONFIG, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    cfg.update(data)
            except Exception:
                pass
        else:
            cfg["gpu"] = _detect_gpu()
        return cfg

    def _gather(self) -> dict:
        cfg = dict(self.cfg)
        cfg["theme"] = self.theme
        cfg["gpu"]   = self.v["gpu"].get()
        cfg["root"]  = self.v["root"].get().strip()
        cfg["ram"]   = self.v["ram"].get().strip()
        cfg["mounts"] = list(self.mounts)
        cfg["fs_speed"] = self.v["fs_speed"].get().strip()
        cfg["res_mode"] = self.v["res_mode"].get()
        cfg["cols"]  = _int(self.v["cols"].get(), 80)
        cfg["rows"]  = _int(self.v["rows"].get(), 25)
        cfg["px_w"]  = _int(self.v["px_w"].get(), 640)
        cfg["px_h"]  = _int(self.v["px_h"].get(), 400)
        cfg["scale"] = self.v["scale"].get()
        cfg["fps"]   = _int(self.v["fps"].get(), 30)
        cfg["allow_resize"] = bool(self.v["allow_resize"].get())
        cfg["bios"]  = self.v["bios"].get().strip()
        cfg["extra"] = self.v["extra"].get().strip()
        cfg["allow_beep"]   = bool(self.v["allow_beep"].get())
        cfg["allow_sounds"] = bool(self.v["allow_sounds"].get())
        return cfg

    def _save(self):
        try:
            self.cfg = self._gather()
            with open(CONFIG, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self._log(f"[save error] {e}\n", "error")

    def _schedule_save(self, *_a):
        if self._save_id is not None:
            try: self.after_cancel(self._save_id)
            except Exception: pass
        self._save_id = self.after(400, self._force_save)

    def _force_save(self):
        self._save_id = None
        self._save()
        self._bios_save_meta(silent=True)

    def _wire_autosave(self):
        for var in self.v.values():
            try: var.trace_add("write", self._schedule_save)
            except Exception: pass

    # ══════════════════════════════════════════════════════════════════
    #  UI
    # ══════════════════════════════════════════════════════════════════
    def _build_ui(self):
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(side="top", fill="x")

        self.btn_launch = ttk.Button(bar, text="▶  Launch", style="Primary.TButton", command=self._launch)
        self.btn_launch.pack(side="left")
        self.btn_stop = ttk.Button(bar, text="■  Stop", style="Danger.TButton", command=self._stop)
        self.btn_stop.pack(side="left", padx=(6, 0))

        ttk.Button(bar, text="📋 Copy Logs", command=self._copy_logs).pack(side="right", padx=(4, 0))
        ttk.Button(bar, text="Show command", command=self._show_command).pack(side="right", padx=(4, 0))
        ttk.Button(bar, text="🌓 Theme", command=self._toggle_theme).pack(side="right", padx=(4, 0))

        self.paned = tk.PanedWindow(self, orient="vertical", sashwidth=5, bd=0, relief="flat", bg=self.colors["bg"])
        self.paned.pack(side="top", fill="both", expand=True, padx=8, pady=(0, 4))

        self.nb = ttk.Notebook(self.paned)
        self.paned.add(self.nb, minsize=340, stretch="always")

        self._build_tab_main()
        self._build_tab_display()
        self._build_tab_disks()
        self._build_tab_sound()
        self._build_tab_bios()

        log_frame = ttk.Frame(self.paned)
        self.paned.add(log_frame, minsize=100, stretch="always")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(log_frame, height=9, wrap="word", font=("TkFixedFont", 9), state="disabled", relief="flat", bd=0, highlightthickness=1)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log_text.config(yscrollcommand=sb.set)

        self.status = tk.Label(self, anchor="w", padx=10, pady=4, text="Ready")
        self.status.pack(side="bottom", fill="x")

    def _build_tab_main(self):
        tab = ttk.Frame(self.nb, padding=16); self.nb.add(tab, text="  Main  "); tab.columnconfigure(1, weight=1)
        r = 0
        ttk.Label(tab, text="Root OS:").grid(row=r, column=0, sticky="w", pady=(0, 4)); r += 1
        row = ttk.Frame(tab); row.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(0, 2)); row.columnconfigure(0, weight=1)
        self.v["root"] = tk.StringVar()
        ttk.Combobox(row, textvariable=self.v["root"], values=ROOT_OPTIONS).grid(row=0, column=0, sticky="ew")
        ttk.Button(row, text="Browse…", command=self._browse_root).grid(row=0, column=1, padx=(6, 0))
        r += 1
        ttk.Label(tab, text="openos / mineos, or any folder with /init.lua.", style="Muted.TLabel").grid(row=r, column=0, columnspan=3, sticky="w", pady=(0, 14))
        r += 1
        ttk.Label(tab, text="RAM:").grid(row=r, column=0, sticky="w", pady=(0, 4)); r += 1
        row = ttk.Frame(tab); row.grid(row=r, column=0, columnspan=3, sticky="w", pady=(0, 14))
        self.v["ram"] = tk.StringVar()
        ttk.Combobox(row, textvariable=self.v["ram"], values=RAM_OPTIONS, width=12).pack(side="left")
        ttk.Label(row, text="  e.g. 512K, 1M, 8M, 64M", style="Muted.TLabel").pack(side="left")
        r += 1
        ttk.Label(tab, text="Extra args:").grid(row=r, column=0, sticky="w", pady=(0, 4)); r += 1
        self.v["extra"] = tk.StringVar()
        ttk.Entry(tab, textvariable=self.v["extra"]).grid(row=r, column=0, columnspan=3, sticky="ew", pady=(0, 2))
        r += 1
        ttk.Label(tab, text="Raw arguments appended to the command line.", style="Muted.TLabel").grid(row=r, column=0, columnspan=3, sticky="w", pady=(0, 14))
        r += 1
        ttk.Label(tab, text="Environment", font=("TkDefaultFont", 10, "bold")).grid(row=r, column=0, columnspan=3, sticky="w", pady=(8, 6)); r += 1
        info = ttk.Frame(tab); info.grid(row=r, column=0, columnspan=3, sticky="ew"); info.columnconfigure(1, weight=1)
        for i, (k, v) in enumerate((("init.py:", INIT_PY), ("Python:", _python_exe()), ("Config:", CONFIG))):
            ttk.Label(info, text=k, width=10).grid(row=i, column=0, sticky="e", padx=(0, 6))
            ttk.Label(info, text=v, style="Muted.TLabel").grid(row=i, column=1, sticky="w")
        tab.rowconfigure(r + 2, weight=1)

    def _build_tab_display(self):
        tab = ttk.Frame(self.nb, padding=16); self.nb.add(tab, text="  Display  "); tab.columnconfigure(1, weight=1)
        r = 0
        ttk.Label(tab, text="GPU backend:", font=("TkDefaultFont", 10, "bold")).grid(row=r, column=0, columnspan=4, sticky="w", pady=(0, 6)); r += 1
        self.v["gpu"] = tk.StringVar()
        gf = ttk.Frame(tab); gf.grid(row=r, column=0, columnspan=4, sticky="w", pady=(0, 4))
        for text, val in (("Text (terminal)", "text"), ("Pygame", "pygame"), ("SDL2", "sdl2"), ("Tkinter", "tk")):
            ttk.Radiobutton(gf, text=text, value=val, variable=self.v["gpu"]).pack(side="left", padx=(0, 14))
        r += 1
        ttk.Label(tab, text="Text mode opens its own terminal. Others run in-process.", style="Muted.TLabel").grid(row=r, column=0, columnspan=4, sticky="w", pady=(0, 14))
        r += 1
        self.v["res_mode"] = tk.StringVar()
        mf = ttk.Frame(tab); mf.grid(row=r, column=0, columnspan=4, sticky="w", pady=(0, 6))
        ttk.Radiobutton(mf, text="OpenComputers (cols × rows)", value="oc", variable=self.v["res_mode"], command=self._on_res_mode_change).pack(side="left", padx=(0, 16))
        ttk.Radiobutton(mf, text="Custom Pixels (W × H)", value="px", variable=self.v["res_mode"], command=self._on_res_mode_change).pack(side="left")
        r += 1
        self.row_oc = ttk.Frame(tab); self.row_oc.grid(row=r, column=0, columnspan=4, sticky="w", pady=(0, 6))
        ttk.Label(self.row_oc, text="Cols:").pack(side="left")
        self.v["cols"] = tk.StringVar()
        ttk.Spinbox(self.row_oc, from_=20, to=400, width=6, textvariable=self.v["cols"]).pack(side="left", padx=(4, 16))
        ttk.Label(self.row_oc, text="Rows:").pack(side="left")
        self.v["rows"] = tk.StringVar()
        ttk.Spinbox(self.row_oc, from_=5, to=200, width=6, textvariable=self.v["rows"]).pack(side="left", padx=(4, 0))
        self.row_px = ttk.Frame(tab); self.row_px.grid(row=r, column=0, columnspan=4, sticky="w", pady=(0, 6))
        ttk.Label(self.row_px, text="Width:").pack(side="left")
        self.v["px_w"] = tk.StringVar()
        self.sp_px_w = ttk.Spinbox(self.row_px, from_=160, to=8192, increment=8, width=6, textvariable=self.v["px_w"])
        self.sp_px_w.pack(side="left", padx=(4, 16))
        ttk.Label(self.row_px, text="Height:").pack(side="left")
        self.v["px_h"] = tk.StringVar()
        self.sp_px_h = ttk.Spinbox(self.row_px, from_=120, to=8192, increment=8, width=6, textvariable=self.v["px_h"])
        self.sp_px_h.pack(side="left", padx=(4, 0))
        r += 1
        self.row_scale = ttk.Frame(tab); self.row_scale.grid(row=r, column=0, columnspan=4, sticky="ew", pady=(4, 6))
        ttk.Label(self.row_scale, text="Pixelization:").pack(side="left")
        self.v["scale"] = tk.IntVar(value=1)
        self.scale_widget = ttk.Scale(self.row_scale, from_=1.0, to=4.0, orient="horizontal", variable=self.v["scale"], command=self._on_scale_change)
        self.scale_widget.pack(side="left", padx=(8, 8), fill="x", expand=True)
        self.lbl_scale = ttk.Label(self.row_scale, text="x1", width=4); self.lbl_scale.pack(side="left")
        r += 1
        prev_frame = ttk.Frame(tab); prev_frame.grid(row=r, column=0, columnspan=4, sticky="nsew", pady=(10, 10)); prev_frame.columnconfigure(0, weight=1)
        self.preview_canvas = tk.Canvas(prev_frame, width=400, height=240, bg="#000000", highlightthickness=1); self.preview_canvas.grid(row=0, column=0)
        self.lbl_grid_info = ttk.Label(prev_frame, text="Grid: ?", style="Muted.TLabel"); self.lbl_grid_info.grid(row=1, column=0, pady=(4, 0))
        r += 1
        fr = ttk.Frame(tab); fr.grid(row=r, column=0, columnspan=4, sticky="w", pady=(10, 6))
        ttk.Label(fr, text="FPS:").pack(side="left")
        self.v["fps"] = tk.StringVar()
        ttk.Spinbox(fr, from_=1, to=240, width=6, textvariable=self.v["fps"]).pack(side="left", padx=(4, 24))
        self.v["allow_resize"] = tk.BooleanVar()
        ttk.Checkbutton(fr, text="Allow window resize", variable=self.v["allow_resize"]).pack(side="left")
        for k in ("cols", "rows", "px_w", "px_h", "scale"):
            self.v[k].trace_add("write", lambda *_: self._update_preview())
        self.v["px_w"].trace_add("write", lambda *_: self._reset_scale_on_resize())
        self.v["px_h"].trace_add("write", lambda *_: self._reset_scale_on_resize())

    def _reset_scale_on_resize(self):
        if self.v["scale"].get() != 1: self.v["scale"].set(1)

    def _on_res_mode_change(self, *_):
        mode = self.v["res_mode"].get()
        if mode == "oc":
            self.row_oc.grid(); self.row_px.grid_remove(); self.row_scale.grid_remove()
        else:
            self.row_oc.grid_remove(); self.row_px.grid(); self.row_scale.grid()
        self._update_preview()

    def _on_scale_change(self, val=None, *_):
        v = self.v["scale"].get()
        if v < 1: v = 1
        if v > 4: v = 4
        self.lbl_scale.config(text=f"x{v}")
        self._update_preview()

    def _get_char_img(self, char, color, cw, ch):
        key = (char, color, cw, ch)
        if key in self._char_cache: return self._char_cache[key]
        img = tk.PhotoImage(width=cw, height=ch); img.put("#000000", to=(0, 0, cw, ch))
        cp = ord(char); entry = self.font_data.get(cp)
        if not entry:
            fallback_map = {0x250C: 0x2B, 0x2500: 0x2D, 0x2510: 0x2B, 0x2514: 0x2B, 0x2518: 0x2B, 0x2502: 0x7C, 0x2552: 0x2B, 0x2550: 0x3D, 0x2555: 0x2B, 0x2551: 0x7C}
            if cp in fallback_map: entry = self.font_data.get(fallback_map[cp])
        if not entry:
            self._char_cache[key] = img; return img
        raw, width_px = entry
        data = []
        for y in range(ch):
            src_y = int(y * 16 / ch); row = []
            for x in range(cw):
                src_x = int(x * width_px / cw); byte_idx = src_y * (width_px // 8) + (src_x // 8); bit_idx = 7 - (src_x % 8); bit = 0
                if byte_idx < len(raw): bit = (raw[byte_idx] >> bit_idx) & 1
                row.append(color if bit else "#000000")
            data.append("{" + " ".join(row) + "}")
        img.put(" ".join(data)); self._char_cache[key] = img; return img

    def _update_preview(self, *_):
        if not hasattr(self, "preview_canvas"): return
        self.preview_canvas.delete("all")
        mode = self.v["res_mode"].get()
        if mode == "oc":
            cols = _int(self.v["cols"].get(), 80); rows = _int(self.v["rows"].get(), 25); w_px = cols * 8; h_px = rows * 16
        else:
            w_px = _int(self.v["px_w"].get(), 640); h_px = _int(self.v["px_h"].get(), 400); scale = self.v["scale"].get()
            cw = 8 * scale; ch = 16 * scale; cols = max(1, w_px // cw); rows = max(1, h_px // ch); w_px = cols * cw; h_px = rows * ch

        if cols < 5 or rows < 5:
            self.preview_canvas.create_text(200, 120, text="Size too small", fill="red")
            self.lbl_grid_info.config(text="Grid: ?")
            return

        self.lbl_grid_info.config(text=f"Grid: {cols} × {rows}  (Resolution: {w_px}x{h_px})")
        max_cw, max_ch = 400, 240; ratio = w_px / h_px
        if ratio > max_cw / max_ch: canvas_w = max_cw; canvas_h = int(max_cw / ratio)
        else: canvas_h = max_ch; canvas_w = int(max_ch * ratio)
        cell_w = max(1, canvas_w // cols); cell_h = max(1, canvas_h // rows); canvas_w = cols * cell_w; canvas_h = rows * cell_h
        self.preview_canvas.config(width=canvas_w, height=canvas_h)
        self.preview_canvas.create_rectangle(0, 0, canvas_w, canvas_h, fill="#000000", outline=self.colors["border"])

        try:
            screen = [[(" ", "#000000") for _ in range(cols)] for _ in range(rows)]
            text_lines = ["OpenOS 1.8.10 (8192k RAM)", "Did you know OpenComputers has a forum? No? Well, it's at https://oc.cil.li/."]
            max_width = max(10, cols - 6); wrapped = []; max_line_len = len(text_lines[0])
            for line in text_lines:
                if len(line) <= max_width: wrapped.append(line); max_line_len = max(max_line_len, len(line))
                else:
                    greeting = line
                    while len(greeting) > 0:
                        part = greeting[:max_width] if len(greeting) > max_width else greeting
                        if len(greeting) > max_width:
                            space_idx = part.rfind(" ")
                            if space_idx != -1: part = greeting[:space_idx]
                        wrapped.append(part); max_line_len = max(max_line_len, len(part)); greeting = greeting[len(part):].lstrip()

            box_w = max_line_len + 4
            if box_w >= cols: box_w = cols - 2

            screen[0][1] = ("╒", "#FFFFFF")
            if box_w < cols: screen[0][box_w] = ("╕", "#FFFFFF")
            for c in range(2, min(box_w, cols)): screen[0][c] = ("═", "#FFFFFF")

            for i, line in enumerate(wrapped):
                if 1 + i >= rows - 1: break
                screen[1 + i][1] = ("│", "#FFFFFF")
                if box_w < cols: screen[1 + i][box_w] = ("│", "#FFFFFF")
                for c, char in enumerate(line):
                    if 2 + c < box_w and 2 + c < cols: screen[1 + i][2 + c] = (char, "#FFFFFF")

            bottom_r = 1 + min(len(wrapped), rows - 2)
            if bottom_r < rows:
                screen[bottom_r][1] = ("└", "#FFFFFF")
                if box_w < cols: screen[bottom_r][box_w] = ("┘", "#FFFFFF")
                for c in range(2, min(box_w, cols)): screen[bottom_r][c] = ("─", "#FFFFFF")

            prompt_r = bottom_r + 1
            if prompt_r < rows:
                for c, char in enumerate("/home #"):
                    if 1 + c < cols: screen[prompt_r][1 + c] = (char, "#FF5555")
        except IndexError:
            pass

        if cell_w > 6 and cell_h > 10:
            for i in range(1, cols): self.preview_canvas.create_line(i*cell_w, 0, i*cell_w, canvas_h, fill="#0a0a0a")
            for j in range(1, rows): self.preview_canvas.create_line(0, j*cell_h, canvas_w, j*cell_h, fill="#0a0a0a")

        for r in range(rows):
            for c in range(cols):
                char, color = screen[r][c]
                if char == " ": continue
                img = self._get_char_img(char, color, cell_w, cell_h)
                self.preview_canvas.create_image(c * cell_w, r * cell_h, image=img, anchor="nw")

    def _build_tab_disks(self):
        tab = ttk.Frame(self.nb, padding=16); self.nb.add(tab, text="  Disks  ")
        tab.columnconfigure(0, weight=1); tab.rowconfigure(2, weight=1)

        # ── Extra disks list ────────────────────────────────────────────
        ttk.Label(tab, text="Extra disks mounted as filesystem components:",
                  font=("TkDefaultFont", 10, "bold")).grid(
                      row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(tab, text="RO = read-only (must exist) • RW = read-write (created if missing)",
                  style="Muted.TLabel").grid(
                      row=1, column=0, columnspan=2, sticky="w", pady=(0, 6))

        lf = ttk.Frame(tab); lf.grid(row=2, column=0, sticky="nsew")
        lf.columnconfigure(0, weight=1); lf.rowconfigure(0, weight=1)
        self.disk_list = tk.Listbox(lf, height=8, relief="flat", bd=0,
                                    highlightthickness=1, activestyle="none")
        self.disk_list.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.disk_list.yview)
        sb.grid(row=0, column=1, sticky="ns"); self.disk_list.config(yscrollcommand=sb.set)

        btns = ttk.Frame(tab); btns.grid(row=2, column=1, sticky="n", padx=(10, 0))
        ttk.Button(btns, text="+ RO", command=lambda: self._add_disk(False)).pack(fill="x", pady=(0, 4))
        ttk.Button(btns, text="+ RW", command=lambda: self._add_disk(True)).pack(fill="x", pady=(0, 4))
        ttk.Button(btns, text="Remove", command=self._remove_disk).pack(fill="x", pady=(0, 4))
        ttk.Button(btns, text="Clear all", command=self._clear_disks).pack(fill="x")

        # ── Filesystem speed limit ──────────────────────────────────────
        ttk.Label(tab, text="Filesystem speed limit:",
                  font=("TkDefaultFont", 10, "bold")).grid(
                      row=3, column=0, columnspan=2, sticky="w", pady=(16, 4))

        speed_row = ttk.Frame(tab)
        speed_row.grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 4))
        self.v["fs_speed"] = tk.StringVar()
        ttk.Combobox(speed_row, textvariable=self.v["fs_speed"],
                     values=FS_SPEED_OPTIONS, width=10).pack(side="left")
        ttk.Label(speed_row,
                  text="  per disk  (4K, 1M, 8M, 64M;  0 / empty = unlimited)",
                  style="Muted.TLabel").pack(side="left")

        ttk.Label(tab,
                  text="Applies to root, tmpfs and every mounted disk. Read + write combined.",
                  style="Muted.TLabel").grid(
                      row=5, column=0, columnspan=2, sticky="w", pady=(0, 0))

        self._refresh_disks()

    def _add_disk(self, rw: bool):
        p = filedialog.askdirectory(parent=self, title="Choose disk directory")
        if p: self.mounts.append({"path": p, "rw": rw}); self._refresh_disks(); self._schedule_save()

    def _remove_disk(self):
        for i in sorted(list(self.disk_list.curselection()), reverse=True):
            if 0 <= i < len(self.mounts): del self.mounts[i]
        self._refresh_disks(); self._schedule_save()

    def _clear_disks(self):
        if self.mounts and messagebox.askyesno("Clear disks", "Remove all disk mounts?", parent=self):
            self.mounts.clear(); self._refresh_disks(); self._schedule_save()

    def _refresh_disks(self):
        self.disk_list.delete(0, "end")
        for m in self.mounts:
            tag = "[RW]" if m.get("rw") else "[RO]"
            self.disk_list.insert("end", f"  {tag}  {m['path']}")

    def _build_tab_sound(self):
        tab = ttk.Frame(self.nb, padding=16); self.nb.add(tab, text="  Sound  "); tab.columnconfigure(0, weight=1)

        ttk.Label(tab, text="Sound & Beeps", font=("TkDefaultFont", 10, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 8))
        ttk.Label(tab, text="By default, all sounds are disabled to prevent unnecessary noise.", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 16))

        self.v["allow_beep"] = tk.BooleanVar()
        chk_beep = ttk.Checkbutton(tab, text="Allow computer.beep() (Lua) and error beeps", variable=self.v["allow_beep"])
        chk_beep.grid(row=2, column=0, sticky="w", pady=(0, 12))

        self.v["allow_sounds"] = tk.BooleanVar()
        chk_sound = ttk.Checkbutton(tab, text="Allow sound component playback (music, effects)", variable=self.v["allow_sounds"])
        chk_sound.grid(row=3, column=0, sticky="w", pady=(0, 12))

        ttk.Label(tab, text="Note: Error BSODs will also beep twice if 'computer.beep()' is allowed.", style="Muted.TLabel").grid(row=4, column=0, sticky="w", pady=(16, 0))

    def _build_tab_bios(self):
        tab = ttk.Frame(self.nb, padding=16); self.nb.add(tab, text="  BIOS  "); tab.columnconfigure(1, weight=1)
        r = 0
        ttk.Label(tab, text="BIOS file:", font=("TkDefaultFont", 10, "bold")).grid(row=r, column=0, columnspan=3, sticky="w", pady=(0, 4)); r += 1
        self.v["bios"] = tk.StringVar()
        row = ttk.Frame(tab); row.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(0, 2)); row.columnconfigure(0, weight=1)
        ttk.Entry(row, textvariable=self.v["bios"]).grid(row=0, column=0, sticky="ew")
        ttk.Button(row, text="Browse…", command=self._browse_bios).grid(row=0, column=1, padx=(6, 0))
        r += 1
        ttk.Label(tab, text="This file IS the EEPROM code storage.", style="Muted.TLabel").grid(row=r, column=0, columnspan=3, sticky="w", pady=(0, 14)); r += 1
        ttk.Label(tab, text="Meta editor (sidecar .meta):", font=("TkDefaultFont", 10, "bold")).grid(row=r, column=0, columnspan=3, sticky="w", pady=(0, 6)); r += 1
        meta_frame = ttk.Frame(tab); meta_frame.grid(row=r, column=0, columnspan=3, sticky="ew"); meta_frame.columnconfigure(1, weight=1)
        self.v["meta_uuid"]  = tk.StringVar(); self.v["meta_data"]  = tk.StringVar(); self.v["meta_label"] = tk.StringVar(); self.v["meta_readonly"] = tk.BooleanVar()
        for i, (k, key, hint) in enumerate((("UUID:", "meta_uuid", "component address"), ("Data:", "meta_data", "boot address"), ("Label:", "meta_label", "short name"))):
            ttk.Label(meta_frame, text=k).grid(row=i, column=0, sticky="e", padx=(0, 8), pady=3)
            ttk.Entry(meta_frame, textvariable=self.v[key]).grid(row=i, column=1, sticky="ew", pady=3)
            ttk.Label(meta_frame, text=hint, style="Muted.TLabel").grid(row=i, column=2, sticky="w", padx=(8, 0))
        ttk.Label(meta_frame, text="Readonly:").grid(row=3, column=0, sticky="e", padx=(0, 8), pady=3)
        ttk.Checkbutton(meta_frame, text="Prevent guest from writing EEPROM", variable=self.v["meta_readonly"]).grid(row=3, column=1, columnspan=2, sticky="w", pady=3)
        r += 1
        btn_row = ttk.Frame(tab); btn_row.grid(row=r, column=0, columnspan=3, sticky="w", pady=(12, 0))
        ttk.Button(btn_row, text="Reload from file", command=self._bios_load_meta).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row, text="Save meta to file", command=lambda: self._bios_save_meta(silent=False)).pack(side="left")
        self.v["bios"].trace_add("write", lambda *_a: self._bios_load_meta())
        for name in ("meta_uuid", "meta_data", "meta_label", "meta_readonly"):
            try: self.v[name].trace_add("write", lambda *_a: self._bios_save_meta(silent=True))
            except Exception: pass

    def _bios_abspath(self) -> str | None:
        p = self.v["bios"].get().strip() if "bios" in self.v else ""
        if not p: return None
        return p if os.path.isabs(p) else os.path.join(HERE, p)

    def _bios_load_meta(self):
        path = self._bios_abspath()
        meta = {}
        if path and os.path.isfile(path + ".meta"):
            try:
                with open(path + ".meta", "r", encoding="utf-8") as f:
                    m = json.load(f)
                if isinstance(m, dict): meta = m
            except Exception as e: self._log(f"[meta load error] {e}\n", "warn")
        self.v["meta_uuid"].set(meta.get("uuid", "")); self.v["meta_data"].set(meta.get("data", ""))
        self.v["meta_label"].set(meta.get("label", "")); self.v["meta_readonly"].set(bool(meta.get("readonly", False)))

    def _bios_save_meta(self, silent: bool = True):
        path = self._bios_abspath()
        if not path or not os.path.isfile(path): return
        meta_path = path + ".meta"
        existing = {}
        if os.path.isfile(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    m = json.load(f)
                if isinstance(m, dict): existing = m
            except Exception: pass
        existing["uuid"] = self.v["meta_uuid"].get().strip(); existing["data"] = self.v["meta_data"].get().strip()
        existing["label"] = self.v["meta_label"].get().strip(); existing["readonly"] = bool(self.v["meta_readonly"].get())
        try:
            with open(meta_path, "w", encoding="utf-8") as f: json.dump(existing, f, indent=2, ensure_ascii=False)
            if not silent: self._log(f"[meta saved] {meta_path}\n", "info")
        except Exception as e:
            if not silent: self._log(f"[meta save error] {e}\n", "error"); messagebox.showerror("Meta save failed", str(e), parent=self)

    @staticmethod
    def _relativize(path: str) -> str:
        try:
            rel = os.path.relpath(path, HERE)
            if not rel.startswith("..") and len(rel) < len(path): return rel
        except ValueError: pass
        return path

    def _browse_root(self):
        p = filedialog.askdirectory(parent=self, title="Choose root OS directory", initialdir=HERE)
        if p: self.v["root"].set(self._relativize(p))

    def _browse_bios(self):
        cur = self.v["bios"].get().strip()
        init = os.path.dirname(cur) if cur else HERE
        if not os.path.isdir(init): init = HERE
        p = filedialog.askopenfilename(parent=self, initialdir=init, filetypes=[("Lua files", "*.lua"), ("All files", "*.*")])
        if p: self.v["bios"].set(self._relativize(p)); self._bios_load_meta()

    def _load_values(self):
        c = self.cfg
        self.v["gpu"].set(c.get("gpu") or "pygame"); self.v["root"].set(c.get("root", "openos"))
        self.v["ram"].set(c.get("ram", "8M")); self.v["res_mode"].set(c.get("res_mode", "oc"))
        self.v["cols"].set(str(c.get("cols", 80))); self.v["rows"].set(str(c.get("rows", 25)))
        self.v["px_w"].set(str(c.get("px_w", 640))); self.v["px_h"].set(str(c.get("px_h", 400)))
        self.v["scale"].set(_int(c.get("scale"), 1)); self.v["fps"].set(str(c.get("fps", 30)))
        self.v["allow_resize"].set(bool(c.get("allow_resize", False))); self.v["bios"].set(c.get("bios", "bios/code.lua"))
        self.v["extra"].set(c.get("extra", ""))
        self.v["allow_beep"].set(bool(c.get("allow_beep", False)))
        self.v["allow_sounds"].set(bool(c.get("allow_sounds", False)))
        self.v["fs_speed"].set(c.get("fs_speed", "8M"))

        self._refresh_disks(); self._bios_load_meta(); self._on_res_mode_change(); self._on_scale_change()

    def _apply_theme(self):
        c = self.colors; style = ttk.Style()
        try: style.theme_use("clam")
        except Exception: pass
        style.configure(".", background=c["bg"], foreground=c["fg"])
        style.configure("TFrame", background=c["bg"]); style.configure("TLabel", background=c["bg"], foreground=c["fg"])
        style.configure("Muted.TLabel", background=c["bg"], foreground=c["muted"])
        style.configure("TLabelframe", background=c["bg"], foreground=c["fg"], bordercolor=c["border"])
        style.configure("TLabelframe.Label", background=c["bg"], foreground=c["fg"])
        style.configure("TButton", background=c["btn_bg"], foreground=c["btn_fg"], borderwidth=0, focuscolor=c["bg"], padding=(10, 4))
        style.map("TButton", background=[("active", c["btn_hover"]), ("disabled", c["bg"])], foreground=[("disabled", c["muted"])])
        style.configure("Primary.TButton", background=c["primary"], foreground="#ffffff", borderwidth=0, padding=(12, 5), font=("TkDefaultFont", 10, "bold"))
        style.map("Primary.TButton", background=[("active", c["primary_hover"]), ("disabled", c["bg"])], foreground=[("disabled", c["muted"])])
        style.configure("Danger.TButton", background=c["danger"], foreground="#ffffff", borderwidth=0, padding=(12, 5), font=("TkDefaultFont", 10, "bold"))
        style.map("Danger.TButton", background=[("active", c["danger_hover"]), ("disabled", c["bg"])], foreground=[("disabled", c["muted"])])
        style.configure("TEntry", fieldbackground=c["entry_bg"], foreground=c["entry_fg"], insertcolor=c["fg"], borderwidth=1, relief="flat")
        style.configure("TCombobox", fieldbackground=c["entry_bg"], background=c["entry_bg"], foreground=c["entry_fg"], arrowcolor=c["fg"], borderwidth=1)
        style.map("TCombobox", fieldbackground=[("readonly", c["entry_bg"])], selectbackground=[("readonly", c["select_bg"])], selectforeground=[("readonly", c["select_fg"])], foreground=[("readonly", c["entry_fg"])])
        style.configure("TSpinbox", fieldbackground=c["entry_bg"], background=c["btn_bg"], foreground=c["entry_fg"], arrowcolor=c["fg"])
        style.configure("TCheckbutton", background=c["bg"], foreground=c["fg"], focuscolor=c["bg"])
        style.configure("TRadiobutton", background=c["bg"], foreground=c["fg"], focuscolor=c["bg"])
        style.configure("TNotebook", background=c["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", background=c["tab_bg"], foreground=c["tab_fg"], padding=(16, 6), borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", c["tab_active_bg"])], foreground=[("selected", c["tab_active_fg"])])
        self.configure(bg=c["bg"])
        try: self.paned.config(bg=c["bg"])
        except Exception: pass
        try:
            self.log_text.config(bg=c["log_bg"], fg=c["log_fg"], insertbackground=c["fg"], selectbackground=c["select_bg"], selectforeground=c["select_fg"], highlightbackground=c["border"], highlightcolor=c["border"])
            for tag, col in (("error", c["error"]), ("warn", c["warn"]), ("info", c["info"]), ("cmd", c["cmd"])): self.log_text.tag_configure(tag, foreground=col)
        except Exception: pass
        try: self.disk_list.config(bg=c["entry_bg"], fg=c["entry_fg"], selectbackground=c["select_bg"], selectforeground=c["select_fg"], highlightbackground=c["border"], highlightcolor=c["border"])
        except Exception: pass
        try: self.status.config(bg=c["status_bg"], fg=c["status_fg"])
        except Exception: pass
        try: self.preview_canvas.config(bg="#000000", highlightbackground=c["border"], highlightcolor=c["border"])
        except Exception: pass

    def _toggle_theme(self):
        self.theme = "light" if self.theme == "dark" else "dark"
        self.colors = DARK if self.theme == "dark" else LIGHT
        self._apply_theme(); self._schedule_save()

    def _build_args(self) -> list[str]:
        c = self._gather(); a: list[str] = []
        if c["root"]: a += ["--root", c["root"]]
        if c["ram"]: a += ["--ram", c["ram"]]
        for m in c["mounts"]: a += ["--mount-disk-rw" if m["rw"] else "--mount-disk", m["path"]]
        if c["fs_speed"]: a += ["--fs-speed", c["fs_speed"]]
        if c["bios"]: a += ["--bios", c["bios"]]
        gpu = c["gpu"]
        if gpu == "pygame": a.append("--pygame")
        elif gpu == "sdl2": a.append("--sdl2")
        elif gpu == "tk":   a.append("--tk")
        if c["res_mode"] == "oc": a += ["--cols", str(c["cols"]), "--rows", str(c["rows"])]
        else:
            a += ["--resolution", f"{c['px_w']}x{c['px_h']}"]
            cw = 8 * c["scale"]; ch = 16 * c["scale"]
            if cw != 8 or ch != 16: a += ["--cell", f"{cw}x{ch}"]
        if c["fps"] and c["fps"] != 30: a += ["--fps", str(c["fps"])]
        if c["allow_resize"]: a.append("--allow-resize")
        if c["allow_beep"]: a.append("--allow-beep")
        if c["allow_sounds"]: a.append("--allow-sounds")
        if c["extra"]:
            try: a += shlex.split(c["extra"])
            except ValueError as e: raise RuntimeError(f"invalid extra args: {e}")
        return a

    def _command_base(self) -> list[str]:
        return [INIT_PY] + self._build_args()

    def _show_command(self):
        try: base = self._command_base()
        except RuntimeError as e: self._log(f"[ERROR] {e}\n", "error"); return
        exe = _python_exe(); cmd = [exe, "-u"] + base
        self._log(f"\n[CMD] " + " ".join(f'"{c}"' if ' ' in c or '(' in c or ')' in c else c for c in cmd) + "\n", "cmd")

    def _launch(self):
        if self._running: return
        if not os.path.isfile(INIT_PY):
            messagebox.showerror("Launch", f"init.py not found:\n{INIT_PY}", parent=self); return
        if not self._confirm_root(): return
        try: base = self._command_base()
        except RuntimeError as e: messagebox.showerror("Launch", str(e), parent=self); return

        self._force_save(); self._start_time = time.time()
        gpu = self.v["gpu"].get()
        self._log("\n" + "─" * 70 + "\n", "info")

        try:
            if gpu == "text":
                self._launch_terminal(base)
            else:
                self._launch_capture(base)
        except Exception as e:
            self._log(f"[LAUNCH ERROR] {type(e).__name__}: {e}\n", "error")
            messagebox.showerror("Launch failed", str(e), parent=self); return

        self._set_running(True)
        self._status("Running...")
        self._start_monitor()

    def _confirm_root(self) -> bool:
        root = self.v["root"].get().strip()
        if not root: return messagebox.askyesno("No root OS", "No root OS selected.\n\nLaunch anyway?", parent=self)
        abs_root = root if os.path.isabs(root) else os.path.join(HERE, root)
        if not os.path.isdir(abs_root): return messagebox.askyesno("Missing root directory", f"Directory not found:\n{abs_root}\n\nLaunch anyway?", parent=self)
        return True

    def _launch_capture(self, base: list[str]):
        exe = _python_exe()
        args = [exe, "-u"] + base

        log_fd, log_path = tempfile.mkstemp(suffix=".log", prefix="lupi_log_", text=True)
        os.close(log_fd)
        self._log_file_path = log_path
        _temp_files.append(log_path)

        kwargs = {
            "cwd": HERE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "bufsize": 1,
            "text": True,
            "errors": "replace",
        }
        if IS_WIN:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        self.proc = subprocess.Popen(args, **kwargs)
        proc = self.proc
        log_file = open(log_path, "w", encoding="utf-8", errors="replace")

        def reader():
            try:
                while True:
                    line = proc.stdout.readline()
                    if not line:
                        break
                    try:
                        log_file.write(line)
                        log_file.flush()
                    except Exception:
                        pass
                    self.after(0, lambda l=line: self._log(l))
            except Exception:
                pass
            finally:
                try:
                    log_file.close()
                except Exception:
                    pass

        threading.Thread(target=reader, daemon=True).start()


    def _launch_terminal(self, base: list[str]):
        exe = _python_exe()
        args = [exe, "-u"] + base

        kwargs = {"cwd": HERE}
        if IS_WIN:
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE

        self.proc = subprocess.Popen(args, **kwargs)

    def _copy_logs(self):
        try:
            text = self.log_text.get("1.0", "end-1c")
            self.clipboard_clear()
            self.clipboard_append(text)
            self._status("Logs copied to clipboard!")
        except Exception as e:
            self._status(f"Copy failed: {e}")

    def _start_monitor(self):
        self._monitor_id = self.after(400, self._monitor)

    def _monitor(self):
        self._monitor_id = None
        if self.proc is None: return
        rc = self.proc.poll()
        if rc is None:
            self._monitor_id = self.after(400, self._monitor); return

        if self._log_file_path and os.path.exists(self._log_file_path):
            try:
                with open(self._log_file_path, "r", encoding="utf-8", errors="replace") as f:
                    pass
            except Exception: pass

        uptime = time.time() - self._start_time
        if uptime < 1.5 and rc != 0: self._log(f"\n[CRASHED after {uptime:.1f}s with code {rc}]\n", "error")
        else: self._log(f"\n[EXITED with code {rc}]\n", "cmd")
        self._status(f"Exited (code {rc})")
        self._set_running(False)
        self.proc = None
        self._log_file_path = None

    def _stop(self):
        if not self._running: return
        self._log("\n[STOP requested]\n", "warn")
        self._status("Stopping…")

        if self.proc is not None and isinstance(self.proc, subprocess.Popen):
            pid = self.proc.pid
            def kill():
                try:
                    if IS_WIN: subprocess.call(["taskkill", "/T", "/F", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        import signal
                        os.kill(pid, signal.SIGTERM)
                        try: self.proc.wait(timeout=2)
                        except subprocess.TimeoutExpired: os.kill(pid, signal.SIGKILL)
                except Exception: pass
            threading.Thread(target=kill, daemon=True).start()
        self._set_running(False)
        self._status("Stopped")

    def _log(self, text, tag=None):
        if not isinstance(text, str): text = str(text)
        if tag is None:
            low = text.lower()
            if "error" in low or "traceback" in low or "exception" in low: tag = "error"
            elif "warn" in low: tag = "warn"
        try:
            self.log_text.config(state="normal")
            self.log_text.insert("end", text, tag or ())
            self.log_text.see("end")
            self.log_text.config(state="disabled")
        except Exception: pass

    def _status(self, text):
        try: self.status.config(text="  " + text)
        except Exception: pass

    def _set_running(self, running: bool):
        self._running = running
        try:
            self.btn_launch.config(state="disabled" if running else "normal")
            self.btn_stop.config(state="normal" if running else "disabled")
        except Exception: pass

    def _on_close(self):
        if self._running and self.proc is not None:
            r = messagebox.askyesnocancel("Quit", "Emulator is running.\n\nYes = stop it and quit\nNo  = quit and leave it running", parent=self)
            if r is None: return
            if r: self._stop()
        if self._monitor_id:
            try: self.after_cancel(self._monitor_id)
            except Exception: pass
        self._force_save()
        self.destroy()

if __name__ == "__main__":
    App().mainloop()