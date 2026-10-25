"""
init.py — главный модуль запуска LuPI (Python port)
"""
from __future__ import annotations
import sys
import os
import time
import atexit
import logging
import argparse
import math

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

logger = logging.getLogger("lupi.init")

from util import color
from util import random  as random_mod
from util import buffer

from core import component   as component_mod
from core import computer    as computer_mod
from core import eeprom      as eeprom_mod
from core import gpio        as gpio_mod
from core import internet    as internet_mod
from core import filesystem  as filesystem_mod
from core import sound       as sound_mod
from core import clipboard   as clipboard_mod
from gpu  import textgpu     as textgpu_mod

_DEBUG_MODE = False
_debug_stream = None

_OOM_GRACE_KB = 8.0
_MEM_HOOK_INTERVAL = 20000
_GC_MIN_INTERVAL = 0.3

_MEMORY_SCALE = 0.5674
_REAL_MEMORY_MULTIPLIER = 1.0 / _MEMORY_SCALE

_GPU_KIND = "text"

def _close_log_stream():
    global _debug_stream
    s = _debug_stream
    _debug_stream = None
    if s is not None:
        try:
            s.flush()
            s.close()
        except Exception:
            pass


atexit.register(_close_log_stream)


# ── Аргументы ─────────────────────────────────────────────────────────────────

def _parse_size(s):
    if s is None:
        return None
    try:
        w_str, h_str = str(s).lower().split("x", 1)
        w = int(w_str.strip())
        h = int(h_str.strip())
    except (ValueError, AttributeError):
        raise ValueError(f"bad size {s!r}, expected WxH")
    if w <= 0 or h <= 0:
        raise ValueError(f"bad size {s!r}: values must be positive")
    return w, h


def _parse_ram(s):
    if s is None:
        return None
    t = str(s).strip().upper()
    mult = 1
    if t.endswith("K") or t.endswith("KB"):
        mult = 1024
        t = t.rstrip("KB").strip()
    elif t.endswith("M") or t.endswith("MB"):
        mult = 1024 * 1024
        t = t.rstrip("MB").strip()
    elif t.endswith("G") or t.endswith("GB"):
        mult = 1024 * 1024 * 1024
        t = t.rstrip("GB").strip()
    try:
        n = float(t)
    except ValueError:
        raise ValueError(f"bad RAM size {s!r} (expected e.g. 512K, 1M, 1.5M, 64M)")
    if n <= 0:
        raise ValueError(f"bad RAM size {s!r}: must be positive")
    return int(n * mult)


def _parse_fs_speed(s):
    """'4k' → 4096; '64K' → 65536; '1m' → 1048576; '0'/None → 0 (unlimited)."""
    if s is None:
        return 0
    t = str(s).strip().lower()
    if not t or t == "0" or t in ("none", "unlimited", "off"):
        return 0

    mult = 1
    if t.endswith("kb"):
        mult = 1024
        t = t[:-2]
    elif t.endswith("mb"):
        mult = 1024 * 1024
        t = t[:-2]
    elif t.endswith("gb"):
        mult = 1024 * 1024 * 1024
        t = t[:-2]
    elif t.endswith("k"):
        mult = 1024
        t = t[:-1]
    elif t.endswith("m"):
        mult = 1024 * 1024
        t = t[:-1]
    elif t.endswith("g"):
        mult = 1024 * 1024 * 1024
        t = t[:-1]
    elif t.endswith("b"):
        t = t[:-1]

    try:
        n = float(t)
    except ValueError:
        raise ValueError(
            f"bad fs-speed {s!r} (expected e.g. 4k, 64k, 1m, 0=unlimited)")
    if n < 0:
        raise ValueError(f"bad fs-speed {s!r}: must be >= 0")
    return int(n * mult)


def _expand_mounts(specs):
    result = []
    for raw in specs or []:
        for part in str(raw).split(','):
            p = part.strip()
            if p:
                result.append(p)
    return result


def parse_args():
    p = argparse.ArgumentParser(description="LuPI2 Python port")

    p.add_argument(
        "--debug", nargs="?", const="lupi.log", default=None,
        metavar="FILE",
        help="Write ALL logs (Python + Lua print) to FILE. "
             "If FILE is omitted, uses 'lupi.log' next to init.py. "
             "Env var LUPI_DEBUG=1 enables with default filename; "
             "LUPI_DEBUG=/path/to/file.log sets custom path.",
    )

    p.add_argument("--root",       default="root",      help="Root filesystem path")
    p.add_argument("--tmpfs",      default="tmpfs",
                   help="Path to tmpfs directory.")
    p.add_argument("--root-label", default=None,
                   help="Virtual label for root fs. Default: basename(--root).")
    p.add_argument("--tmp-label",  default="tmpfs",
                   help="Virtual label for tmpfs. Default: 'tmpfs'.")

    p.add_argument(
        "--mount-disk", action="append", default=[],
        metavar="PATH[,PATH...]",
        help="Доп. read-only диски. Можно перечислять через запятую "
             "(--mount-disk a,b) или повторять флаг (--mount-disk a "
             "--mount-disk b). Каталоги должны существовать. "
             "Метка = basename(path), обрезается до 16 символов.",
    )
    p.add_argument(
        "--mount-disk-rw", action="append", default=[],
        metavar="PATH[,PATH...]",
        help="То же, что --mount-disk, но read-write. Каталоги создаются, "
             "если отсутствуют.",
    )

    p.add_argument(
        "--fs-speed", default=None, metavar="BYTES",
        help="Ограничение суммарной скорости чтения/записи КАЖДОЙ "
             "файловой системы (байт/сек, можно с суффиксом k/m/g). "
             "Например: --fs-speed 4k (не более 4 КиБ/с на диск). "
             "0 / пусто = без лимита (по умолчанию).",
    )

    p.add_argument("--bios",       default="bios/code.lua",
                   help="Path to BIOS Lua file. This file IS the EEPROM "
                        "storage — it is read AND written by the eeprom "
                        "component (guest calls to eeprom.set() overwrite "
                        "this file directly). A sidecar <bios>.meta stores "
                        "data/label/readonly. If the file is missing it is "
                        "created with DEFAULT_BOOT_CODE.")

    p.add_argument("--eeprom-uuid", default=None, metavar="UUID",
                   help="Override the EEPROM component address. By default "
                        "the address is read from <bios>.meta if present, "
                        "otherwise a fresh UUID is generated and persisted "
                        "there. Passing --eeprom-uuid overrides and persists "
                        "the new value.")

    p.add_argument("--pygame",     action="store_true", help="Use pygame GPU")
    p.add_argument("--tk",         action="store_true",
                   help="Use tkinter GPU (zero-dependency fallback)")
    p.add_argument("--sdl2",       action="store_true",
                   help="Use pure SDL2 GPU (requires PySDL2)")

    p.add_argument("--cols", type=int, default=80)
    p.add_argument("--rows", type=int, default=25)
    p.add_argument("--cell", default=None, metavar="WxH")
    p.add_argument("--resolution", default=None, metavar="WxH")
    p.add_argument("--max-cols", type=int, default=None)
    p.add_argument("--max-rows", type=int, default=None)
    p.add_argument("--scale", type=float, default=1.0)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--allow-resize", action="store_true")

    p.add_argument("--ram", default=None, metavar="SIZE",
                   help="Virtual RAM size (e.g. 512K, 1M, 1.5M, 64M). "
                        "Default: 64M. Env: LUPI_RAM.")
    p.add_argument("--no-ram-enforce", action="store_true",
                   help="Disable hard RAM enforcement.")

    p.add_argument("--allow-beep", action="store_true",
                   help="Enable computer.beep() sounds, including error beeps "
                        "(default: off).")
    p.add_argument("--allow-sounds", action="store_true",
                   help="Enable the sound component's playback "
                        "(default: off).")
    return p.parse_args()


# ── Вспомогательные ───────────────────────────────────────────────────────────

def lprint(*args):
    if not _DEBUG_MODE:
        return
    logger.info(" ".join(str(a) for a in args))


def _lua_src(s: str) -> str:
    try:
        return s.encode('utf-8').decode('latin-1')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def _resolve_tmpfs_path(tmp_arg: str, root_arg: str) -> str:
    if os.path.isabs(tmp_arg):
        return tmp_arg
    root_abs = os.path.abspath(root_arg)
    parent = os.path.dirname(root_abs) or os.getcwd()
    return os.path.join(parent, tmp_arg)


def _compute_wide_ranges(codepoints) -> list[tuple[int, int]]:
    cps = sorted(codepoints)
    if not cps:
        return []
    ranges = []
    start = prev = cps[0]
    for cp in cps[1:]:
        if cp == prev + 1:
            prev = cp
        else:
            ranges.append((start, prev))
            start = prev = cp
    ranges.append((start, prev))
    return ranges


def _mount_disk(spec: str, readonly: bool, filesystem_mod) -> None:
    tag = "RO" if readonly else "RW"
    flag_name = "--mount-disk" if readonly else "--mount-disk-rw"

    path = os.path.abspath(spec)
    lprint(f"Preparing {tag} mount: spec={spec!r} → {path}")

    if readonly:
        if not os.path.isdir(path):
            msg = f"[init] {flag_name}: directory not found: {path!r}"
            print(msg, file=sys.stderr); sys.stderr.flush()
            lprint(msg)
            sys.exit(1)
    else:
        try:
            os.makedirs(path, exist_ok=True)
            lprint(f"Ensured directory for {flag_name}: {path}")
        except OSError as e:
            msg = f"[init] {flag_name}: cannot create {path!r}: {e}"
            print(msg, file=sys.stderr); sys.stderr.flush()
            lprint(msg)
            sys.exit(1)

    label = (os.path.basename(path.rstrip('/\\')) or "disk")[:16]

    try:
        addr = filesystem_mod.register(path, label=label, readonly=readonly)
    except Exception as e:
        import traceback
        msg = f"[init] {flag_name} {spec!r}: {e}"
        print(msg, file=sys.stderr); sys.stderr.flush()
        lprint(msg)
        lprint(traceback.format_exc())
        sys.exit(1)

    lprint(f"Mounted {tag} disk: {path} → {addr} (label={label!r})")


# ── Логирование ───────────────────────────────────────────────────────────────

def _setup_logging(args) -> str | None:
    global _DEBUG_MODE, _debug_stream

    debug_target = args.debug

    if debug_target is None:
        env_dbg = os.getenv("LUPI_DEBUG")
        if env_dbg:
            if env_dbg.strip().lower() in ("1", "true", "yes", "on"):
                debug_target = "lupi.log"
            else:
                debug_target = env_dbg

    if not debug_target:
        logging.disable(logging.CRITICAL + 1)
        _DEBUG_MODE = False
        return None

    if not os.path.isabs(debug_target):
        debug_target = os.path.join(_HERE, debug_target)

    try:
        _debug_stream = open(debug_target, "w", encoding="utf-8", buffering=1)
    except OSError as e:
        print(f"[init] Cannot open log file {debug_target!r}: {e}",
              file=sys.stderr)
        sys.exit(1)

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=_debug_stream,
        force=True,
    )
    _DEBUG_MODE = True

    logger.info("=" * 72)
    logger.info("LuPI session started: %s", time.strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("log file:  %s", debug_target)
    logger.info("argv:      %s", sys.argv)
    logger.info("cwd:       %s", os.getcwd())
    logger.info("=" * 72)
    return debug_target


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    global _DEBUG_MODE, _GPU_KIND

    args = parse_args()

    log_path = _setup_logging(args)
    if log_path:
        computer_mod.add_shutdown_hook(_close_log_stream)

    computer_mod.set_debug_mode(_DEBUG_MODE)

    if args.pygame or args.tk or args.sdl2:
        _GPU_KIND = "graphics"
    else:
        _GPU_KIND = "text"
    lprint(f"GPU kind selected: {_GPU_KIND}")

    ram_raw = args.ram if args.ram is not None else os.getenv("LUPI_RAM")
    if ram_raw:
        try:
            ram_bytes = _parse_ram(ram_raw)
        except ValueError as e:
            print(f"[init] {e}", file=sys.stderr)
            sys.exit(1)
        if ram_bytes is not None:
            computer_mod.set_memory_model(ram_bytes)
            lprint(f"RAM: {ram_bytes} bytes ({ram_bytes / 1024.0:.2f} KiB)")

    # ── Filesystem speed limit (--fs-speed) ─────────────────────────────
    try:
        fs_rate = _parse_fs_speed(args.fs_speed)
    except ValueError as e:
        print(f"[init] {e}", file=sys.stderr)
        sys.exit(1)

    # Устанавливаем глобальный default ДО регистрации любой ФС,
    # чтобы и root/tmpfs/mount-disk, и последующие component.connect
    # получали этот же лимит.
    filesystem_mod.set_default_rate_limit(fs_rate)
    if fs_rate > 0:
        lprint(f"Filesystem speed limit: {fs_rate} B/s "
               f"(~{fs_rate / 1024.0:.1f} KiB/s)")
    else:
        lprint("Filesystem speed limit: unlimited")

    lprint("LuPI Python Port — L1 INIT")
    machine_address = random_mod.uuid()
    lprint(f"Machine address: {machine_address}")

    computer_mod.init(machine_address, "")
    component_mod.init(push_signal_fn=computer_mod.push_signal,
                       uuid_fn=random_mod.uuid)
    component_mod.prepare()
    computer_mod.prepare()

    lprint("Registering EEPROM...")
    _bios_existed = os.path.isfile(args.bios)
    _meta_existed = os.path.isfile(args.bios + ".meta")
    eeprom_mod.register(uuid=args.eeprom_uuid, filename=args.bios)

    eeprom_iter, _ = component_mod.list("eeprom")
    eeprom_pair = eeprom_iter()
    _eeprom_addr = eeprom_pair[0] if eeprom_pair else "?"
    if _bios_existed:
        src = "bios"
    else:
        src = "DEFAULT_BOOT_CODE"
    lprint(f"EEPROM: uuid={_eeprom_addr}, code={args.bios} (from {src}), "
           f"meta={'present' if _meta_existed else 'created'}")

    lprint("Registering GPIO...")
    gpio_mod.register()
    lprint("Registering Internet...")
    internet_mod.start()
    lprint("Registering Sound...")
    sound_mod.set_emit_beeps(args.allow_beep)
    sound_mod.set_emit_sounds(args.allow_sounds)
    sound_mod.register()
    lprint("Registering Clipboard...")
    clipboard_mod.register()

    lprint("Registering Data...")
    from core import data as data_mod
    data_mod.register(tier=3)

    lprint("Registering Modem (wired)...")
    from core import modem as modem_mod
    modem_mod.register(wireless=False)

    lprint("Registering Modem (wireless)...")
    modem_mod.register(wireless=True)

    lprint("Registering Computer component...")
    def _comp_beep(freq=440, duration=0.1):
        _lua_beep(freq, duration)
        return True

    comp_computer_proxy = {
        "isRunning": lambda: True,
        "beep": _comp_beep,
        "stop": lambda: False,
        "start": lambda: False,
        "getDeviceInfo": computer_mod.get_device_info,
        "getProgramLocations": computer_mod.get_program_locations,
    }
    component_mod.register(None, "computer", comp_computer_proxy)

    root_dir = os.path.abspath(args.root)
    os.makedirs(root_dir, exist_ok=True)

    tmp_dir = _resolve_tmpfs_path(args.tmpfs, root_dir)
    os.makedirs(tmp_dir, exist_ok=True)

    root_label = args.root_label or os.path.basename(root_dir) or "root"

    lprint(f"Registering root filesystem: {root_dir} (label={root_label!r})")
    root_fs_addr = filesystem_mod.register(root_dir, label=root_label)

    lprint(f"Registering tmpfs: {tmp_dir} (label={args.tmp_label!r})")
    tmp_addr = filesystem_mod.register(tmp_dir, label=args.tmp_label)
    computer_mod._tmp_address = tmp_addr

    for spec in _expand_mounts(args.mount_disk):
        _mount_disk(spec, readonly=True, filesystem_mod=filesystem_mod)

    for spec in _expand_mounts(args.mount_disk_rw):
        _mount_disk(spec, readonly=False, filesystem_mod=filesystem_mod)

    from gpu.base import (BASE_CELL_W, BASE_CELL_H,
                          HARD_MAX_COLS, HARD_MAX_ROWS)

    cell_size = None
    if args.cell:
        try:
            cell_size = _parse_size(args.cell)
        except ValueError as e:
            lprint(f"Warning: {e} -- ignoring --cell")
            cell_size = None

    cols = args.cols
    rows = args.rows

    if args.resolution:
        try:
            rw, rh = _parse_size(args.resolution)
        except ValueError as e:
            lprint(f"Warning: {e} -- ignoring --resolution")
        else:
            cw, chh = cell_size if cell_size else (BASE_CELL_W, BASE_CELL_H)
            calc_cols = max(1, rw // cw)
            calc_rows = max(1, rh // chh)
            cols = min(calc_cols, HARD_MAX_COLS)
            rows = min(calc_rows, HARD_MAX_ROWS)
            if args.max_cols is None:
                args.max_cols = cols
            if args.max_rows is None:
                args.max_rows = rows
            actual_w = cols * cw
            actual_h = rows * chh
            if cols != calc_cols or rows != calc_rows:
                lprint(f"Resolution {rw}x{rh} (cell {cw}x{chh}) -> "
                       f"grid {calc_cols}x{calc_rows}, clamped to "
                       f"{cols}x{rows} (actual window {actual_w}x{actual_h})")
            else:
                lprint(f"Resolution {rw}x{rh} (cell {cw}x{chh}) -> "
                       f"grid {cols}x{rows} (actual window {actual_w}x{actual_h})")

    gpu_addr = None
    if args.pygame:
        lprint(f"Starting pygame GPU (grid {cols}x{rows})...")
        from gpu import pygamegpu as pygamegpu_mod
        result = pygamegpu_mod.start(
            cols=cols, rows=rows, scale=args.scale,
            cell_size=cell_size, fps=args.fps,
            allow_resize=args.allow_resize,
            max_cols=args.max_cols, max_rows=args.max_rows,
        )
    elif args.tk:
        lprint(f"Starting tkinter GPU (grid {cols}x{rows})...")
        from gpu import tkgpu as tkgpu_mod
        result = tkgpu_mod.start(
            cols=cols, rows=rows, scale=args.scale,
            cell_size=cell_size, fps=args.fps,
            allow_resize=args.allow_resize,
            max_cols=args.max_cols, max_rows=args.max_rows,
        )
    elif args.sdl2:
        lprint(f"Starting SDL2 GPU (grid {cols}x{rows})...")
        from gpu import sdl2gpu as sdl2gpu_mod
        result = sdl2gpu_mod.start(
            cols=cols, rows=rows, scale=args.scale,
            cell_size=cell_size, fps=args.fps,
            allow_resize=args.allow_resize,
            max_cols=args.max_cols, max_rows=args.max_rows,
        )
    else:
        lprint("Starting text GPU...")
        result = textgpu_mod.start()

    if isinstance(result, tuple):
        lprint("GPU failed:", result[1])
        if _GPU_KIND == "graphics":
            _GPU_KIND = "none"
    else:
        gpu_addr = result

    if gpu_addr is None:
        if _GPU_KIND == "graphics":
            _GPU_KIND = "none"
        lprint("No GPU initialized — running headless")

    eeprom_iter, _ = component_mod.list("eeprom")
    eeprom_pair = eeprom_iter()
    eeprom_addr = eeprom_pair[0] if eeprom_pair else None
    if eeprom_addr is None:
        _bsod("No EEPROM found")
        return

    try:
        bios_code = component_mod.invoke(eeprom_addr, "get")
    except Exception as e:
        _bsod(f"Failed to read BIOS from EEPROM: {e}")
        return

    if not isinstance(bios_code, str) or not bios_code:
        _bsod("EEPROM is empty")
        return

    lprint(f"BIOS loaded from {args.bios} ({len(bios_code)} B)")

    try:
        r = component_mod.invoke(eeprom_addr, "setData", root_fs_addr)
        if r is True:
            lprint(f"Boot address set to {root_fs_addr}")
        else:
            lprint(f"Warning: setData returned {r!r}")
    except Exception as e:
        lprint(f"Warning: setData raised: {e}")

    lprint("LuPI L1 INIT complete — booting...")
    _boot(gpu_addr, eeprom_addr, bios_code, root_fs_addr,
          enforce_ram=not args.no_ram_enforce)


# ── Встроенный unicode-модуль ─────────────────────────────────────────────────
_UNICODE_LUA = r"""
local u = {}

local function utf8_codepoints(s)
  local cps = {}
  for ch in s:gmatch("[\1-\127\194-\244][\128-\191]*") do
    local b = ch:byte(1)
    local cp
    if b < 0x80 then
      cp = b
    elseif b < 0xE0 then
      local b2 = ch:byte(2) or 0x80
      cp = (b - 0xC0) * 0x40 + (b2 - 0x80)
    elseif b < 0xF0 then
      local b2 = ch:byte(2) or 0x80
      local b3 = ch:byte(3) or 0x80
      cp = (b - 0xE0) * 0x1000
         + (b2 - 0x80) * 0x40
         + (b3 - 0x80)
    else
      local b2 = ch:byte(2) or 0x80
      local b3 = ch:byte(3) or 0x80
      local b4 = ch:byte(4) or 0x80
      cp = (b - 0xF0) * 0x40000
         + (b2 - 0x80) * 0x1000
         + (b3 - 0x80) * 0x40
         + (b4 - 0x80)
    end
    cps[#cps + 1] = cp
  end
  return cps
end

local function cp_to_utf8(cp)
  cp = math.floor(cp)
  if cp < 0 then return "" end
  if cp < 0x80 then return string.char(cp)
  elseif cp < 0x800 then
    return string.char(0xC0 + math.floor(cp / 0x40), 0x80 + (cp % 0x40))
  elseif cp < 0x10000 then
    return string.char(
      0xE0 + math.floor(cp / 0x1000),
      0x80 + (math.floor(cp / 0x40) % 0x40),
      0x80 + (cp % 0x40))
  elseif cp < 0x200000 then
    return string.char(
      0xF0 + math.floor(cp / 0x40000),
      0x80 + (math.floor(cp / 0x1000) % 0x40),
      0x80 + (math.floor(cp / 0x40) % 0x40),
      0x80 + (cp % 0x40))
  else error("codepoint out of range") end
end

function u.char(...)
  local out = {}
  for i = 1, select("#", ...) do out[#out + 1] = cp_to_utf8(select(i, ...)) end
  return table.concat(out)
end

function u.len(s)
  if type(s) ~= "string" then return 0 end
  local n = 0
  for _ in s:gmatch("[\1-\127\194-\244][\128-\191]*") do n = n + 1 end
  return n
end

function u.sub(s, i, j)
  s = tostring(s)
  if not i then return s end
  local cps = utf8_codepoints(s)
  local n = #cps
  i = i or 1
  j = j or n
  if i < 0 then i = n + i + 1 end
  if j < 0 then j = n + j + 1 end
  if i < 1 then i = 1 end
  if j > n then j = n end
  if i > j then return "" end
  local out = {}
  for k = i, j do out[#out + 1] = cp_to_utf8(cps[k]) end
  return table.concat(out)
end

function u.lower(s) return string.lower(s) end
function u.upper(s) return string.upper(s) end

function u.reverse(s)
  local cps = utf8_codepoints(s)
  local out = {}
  for k = #cps, 1, -1 do out[#out + 1] = cp_to_utf8(cps[k]) end
  return table.concat(out)
end

function u.isWide(_) return false end

function u.charWidth(s)
  if type(s) ~= "string" or #s == 0 then return 0 end
  return u.isWide(s) and 2 or 1
end

function u.wlen(s)
  if type(s) ~= "string" then return 0 end
  local n = 0
  for ch in s:gmatch("[\1-\127\194-\244][\128-\191]*") do
    n = n + (u.isWide(ch) and 2 or 1)
  end
  return n
end

function u.wtrunc(s, w)
  if type(s) ~= "string" then s = tostring(s) end
  if w == nil then return s end
  local out = {}
  local n = 0
  for ch in s:gmatch("[\1-\127\194-\244][\128-\191]*") do
    local cw = u.isWide(ch) and 2 or 1
    if n + cw > w then break end
    out[#out + 1] = ch
    n = n + cw
  end
  return table.concat(out)
end

return u
"""

def _make_lua_print():
    def _decode(a):
        if not isinstance(a, str):
            return a
        try:
            a.encode('ascii')
            return a
        except UnicodeEncodeError:
            pass
        try:
            return a.encode('latin-1').decode('utf-8')
        except (UnicodeEncodeError, UnicodeDecodeError):
            return a

    def _lua_print(*parts):
        try:
            msg = "\t".join(str(_decode(p)) for p in parts)
        except Exception:
            msg = "\t".join(repr(p) for p in parts)
        logger.info("[lua] %s", msg)

    return _lua_print

def _lua_beep(freq=1000.0, duration=0.1, *_):
    try:
        f = float(freq)
    except (TypeError, ValueError):
        f = 1000.0
    if not math.isfinite(f):
        f = 1000.0
    try:
        d = float(duration)
    except (TypeError, ValueError):
        d = 0.1
    if not math.isfinite(d) or d < 0:
        d = 0.1
    computer_mod.beep(f, d * 1000.0)

# ── Memory hook ───────────────────────────────────────────────────────────────

def _install_memory_hook(lua, limit_kb: float, baseline_kb: float = 0.0,
                         interval: int = _MEM_HOOK_INTERVAL):
    try:
        limit_kb = float(limit_kb)
        baseline_kb = float(baseline_kb)
    except (TypeError, ValueError):
        return

    effective_kb = (limit_kb * _REAL_MEMORY_MULTIPLIER) + _OOM_GRACE_KB

    script = _lua_src(f"""
        do
            local limit_kb = {effective_kb!r}
            local baseline_kb = {baseline_kb!r}
            local function _lupi_mem_check()
                local used_kb = collectgarbage("count") - baseline_kb
                if used_kb > limit_kb then
                    collectgarbage("collect")
                    used_kb = collectgarbage("count") - baseline_kb
                    if used_kb > limit_kb then
                        error(string.format(
                            "not enough memory (%.1f KB used of %.1f KB)",
                            used_kb, limit_kb), 0)
                    end
                end
            end
            debug.sethook(_lupi_mem_check, "", {interval})
        end
    """)
    lua.execute(script)
    logger.info(
        "Memory enforcement: limit=%.1f KB, baseline=%.1f KB, effective=%.1f KB, interval=%d",
        limit_kb, baseline_kb, effective_kb, interval)


def _remove_memory_hook(lua):
    try:
        lua.execute(_lua_src("debug.sethook()"))
    except Exception:
        pass


# ── Pump helpers ──────────────────────────────────────────────────────────────

def _pump_events_once():
    try:
        from core import computer as _c
    except Exception:
        return
    block = getattr(_c, "_pump_block_hook", None)
    if block is not None:
        try:
            block(0.05)
            return
        except Exception:
            pass
    pump = getattr(_c, "_pump_hook", None)
    if pump is not None:
        try:
            pump()
        except Exception:
            pass


def _wait_for_key() -> None:
    try:
        from core import computer as _c
    except Exception:
        time.sleep(3600)
        return
    while True:
        try:
            sig = _c.pull_signal(timeout=0.05)
        except Exception:
            time.sleep(0.05)
            continue
        if sig and sig[0] in ("key_down", "touch", "drag"):
            return


def _error_beeps():
    """
    Классический POST-сигнал: 2 бипа с интервалом 0.5 с.
    Играет только если включён --allow-beep (проверка внутри computer.beep).
    """
    try:
        for i in range(2):
            computer_mod.beep(880, 150)
            if i == 0:
                deadline = time.monotonic() + 0.5
                while True:
                    remain = deadline - time.monotonic()
                    if remain <= 0:
                        break
                    _pump_events_once()
                    time.sleep(min(0.02, remain))
    except Exception:
        logger.exception("_error_beeps failed")


def _drain_stale_signals():
    try:
        from core import computer as _c
    except Exception:
        return
    for _ in range(200):
        try:
            if _c.pull_signal(timeout=0) is None:
                return
        except Exception:
            return


# ── Error screens ─────────────────────────────────────────────────────────────

def _invoke_tuple(addr, method, *args) -> tuple:
    try:
        r = component_mod.invoke(addr, method, *args)
    except Exception:
        return ()
    if r is None:
        return ()
    if isinstance(r, (tuple, list)):
        return tuple(r)
    try:
        n = len(r)
        return tuple(r[i] for i in range(1, n + 1))
    except Exception:
        return (r,)


def _draw_bsod_via_gpu(title: str, lines: list[str]) -> bool:
    try:
        gpu_addr = None
        for addr, entry in component_mod._components.items():
            if entry.type == "gpu":
                gpu_addr = addr
                break
        if not gpu_addr:
            return False

        for addr, entry in component_mod._components.items():
            if entry.type == "screen":
                try:
                    component_mod.invoke(gpu_addr, "bind", addr, False)
                except Exception:
                    pass
                break

        w, h = 80, 25
        for method in ("getResolution", "getViewport", "maxResolution"):
            r = _invoke_tuple(gpu_addr, method)
            if len(r) >= 2:
                try:
                    w = int(r[0])
                    h = int(r[1])
                    break
                except (TypeError, ValueError):
                    continue

        def call(name, *args):
            try:
                return component_mod.invoke(gpu_addr, name, *args)
            except Exception:
                return None

        def put_center(row, text, fg=0xFFFFFF):
            if row < 1 or row > h or not text:
                return
            text = str(text)
            if len(text) > w - 2:
                text = text[:w - 2]
            x = max(1, (w - len(text)) // 2 + 1)
            call("setForeground", fg)
            call("set", x, row, text)

        def put_left(row, text, col, fg=0xFFFFFF, max_width=None):
            if row < 1 or row > h or not text:
                return
            text = str(text)
            mw = max_width if max_width is not None else (w - col)
            if len(text) > mw:
                text = text[:mw]
            call("setForeground", fg)
            call("set", col, row, text)

        call("setBackground", 0x0000AA)
        call("setForeground", 0xFFFFFF)
        call("fill", 1, 1, w, h, " ")

        banner_w = min(w - 4, 40)
        put_center(2, "═" * banner_w, 0xAAAAAA)
        put_center(3, "OpenComputers", 0xFFFFFF)
        put_center(4, "═" * banner_w, 0xAAAAAA)

        put_center(6, title.upper(), 0xFFAA00)
        put_center(7, "─" * banner_w, 0xAAAAAA)

        row = 9
        for ln in lines:
            if row > h - 3:
                break
            if ln is None:
                continue
            put_left(row, str(ln), 3, 0xFFFFFF, max_width=w - 5)
            row += 1

        put_center(h - 1, "Press any key...", 0xAAAAAA)

        _pump_events_once()
        return True
    except Exception:
        logger.exception("GPU BSOD draw failed")
        return False


def _oom_screen(used_kb: float, limit_kb: float, original: str = ""):
    over_kb = max(0.0, used_kb - limit_kb)
    pct = (used_kb / limit_kb * 100.0) if limit_kb > 0 else 0.0

    title = "Out of Memory"
    lines = [
        f"Used:   {used_kb:>10.1f} KB",
        f"Limit:  {limit_kb:>10.1f} KB",
        f"Over:   {over_kb:>10.1f} KB  ({pct:.1f} %)",
        "",
        "A program tried to allocate more memory",
        "than this machine has installed.",
        "",
        "Increase --ram, or close some programs.",
    ]
    if original:
        lines.append("")
        lines.append(original[:80])

    logger.error("=== %s ===", title.upper())
    for ln in lines:
        logger.error("OOM: %s", ln)

    drawn = False
    if _GPU_KIND in ("graphics", "text"):
        _drain_stale_signals()
        drawn = _draw_bsod_via_gpu(title, lines)

    _error_beeps()

    if drawn:
        _wait_for_key()
        try:
            computer_mod.shutdown()
        except Exception:
            pass
        sys.exit(0)

    if _GPU_KIND == "text":
        try:
            import curses
            curses.endwin()
        except Exception:
            pass

    print(f"\n=== {title.upper()} ===", file=sys.stderr)
    for ln in lines:
        print("  " + ln, file=sys.stderr)
    print("=" * (len(title) + 8), file=sys.stderr)
    sys.exit(0)


def _bsod(*messages):
    for msg in messages:
        logger.error("BOOT ERROR: %s", msg)

    title = "Critical Boot Error"
    lines = [str(m) for m in messages[:10]]

    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass

    drawn = False
    if _GPU_KIND in ("graphics", "text"):
        _drain_stale_signals()
        drawn = _draw_bsod_via_gpu(title, lines)

    _error_beeps()

    if drawn:
        _wait_for_key()
        try:
            computer_mod.shutdown()
        except Exception:
            pass
        sys.exit(0)

    if _GPU_KIND == "text":
        try:
            import curses
            curses.endwin()
        except Exception:
            pass

    print(f"\n=== {title.upper()} ===", file=sys.stderr)
    for ln in lines:
        print("  " + ln, file=sys.stderr)
    print("=" * (len(title) + 8), file=sys.stderr)
    sys.exit(0)


# ── _boot ─────────────────────────────────────────────────────────────────────

def _boot(gpu_addr: str | None, eeprom_addr: str, bios_code: str,
          root_fs_addr: str, enforce_ram: bool = True):
    import lupa
    from lupa import LuaRuntime
    import traceback
    from gpu import base as gpu_base

    try:
        lua = LuaRuntime(unpack_returned_tuples=True, encoding='latin-1')
        G = lua.globals()

        if _DEBUG_MODE:
            G.print = _make_lua_print()

        try:
            _mem_fn = lua.eval("function() return collectgarbage('count') end")
            _gc_collect_fn = lua.eval("function() collectgarbage('collect') end")
        except Exception:
            _mem_fn = None
            _gc_collect_fn = None
            logger.warning("Cannot install Lua memory query — "
                           "freeMemory will report full RAM")

        _gc_state = {"last": 0.0}
        _gc_min_interval = _GC_MIN_INTERVAL

        if _mem_fn is not None:
            def _lua_mem_used():
                try:
                    now = time.monotonic()
                    if _gc_collect_fn is not None and \
                       now - _gc_state["last"] >= _gc_min_interval:
                        _gc_state["last"] = now
                        try:
                            _gc_collect_fn()
                        except Exception:
                            logger.exception("collectgarbage('collect') failed")
                    kb = _mem_fn()
                    return int(float(kb) * 1024.0)
                except Exception:
                    return 0
            computer_mod.set_memory_usage_getter(_lua_mem_used)
            logger.info("Memory tracker installed (Lua collectgarbage + "
                        "rate-limited full GC every %.2fs)", _gc_min_interval)

        unicode_lib = lua.execute(_lua_src(_UNICODE_LUA))
        G.unicode = unicode_lib

        wide_ranges = _compute_wide_ranges(gpu_base._WIDE_CODEPOINTS)
        lr = lua.table()
        for i, (s, e) in enumerate(wide_ranges, 1):
            lr[i * 2 - 1] = s
            lr[i * 2] = e
        G._luaWideRanges = lr
        logger.info(
            "unicode.isWide: %d ranges for %d wide codepoints "
            "(compact array instead of hash table)",
            len(wide_ranges), len(gpu_base._WIDE_CODEPOINTS))

        lua.execute(_lua_src(r"""
            local _ranges = _luaWideRanges
            local _n = #_ranges // 2

            local function _is_wide_cp(cp)
                local lo, hi = 1, _n
                while lo <= hi do
                    local mid = (lo + hi) // 2
                    local s = _ranges[mid * 2 - 1]
                    local e = _ranges[mid * 2]
                    if cp < s then
                        hi = mid - 1
                    elseif cp > e then
                        lo = mid + 1
                    else
                        return true
                    end
                end
                return false
            end

            function unicode.isWide(x)
                local t = type(x)
                if t == "number" then
                    return _is_wide_cp(x)
                end
                if t ~= "string" or #x == 0 then return false end
                local b = x:byte(1)
                local cp
                if b < 0x80 then
                    cp = b
                elseif b < 0xE0 then
                    if #x < 2 then return false end
                    cp = (b - 0xC0) * 0x40 + (x:byte(2) - 0x80)
                elseif b < 0xF0 then
                    if #x < 3 then return false end
                    cp = (b - 0xE0) * 0x1000
                       + (x:byte(2) - 0x80) * 0x40
                       + (x:byte(3) - 0x80)
                else
                    if #x < 4 then return false end
                    cp = (b - 0xF0) * 0x40000
                       + (x:byte(2) - 0x80) * 0x1000
                       + (x:byte(3) - 0x80) * 0x40
                       + (x:byte(4) - 0x80)
                end
                return _is_wide_cp(cp)
            end
        """))

        lua.execute(_lua_src(r"""
            do
                local _os_date = os.date
                os.date = function(fmt, t)
                    if t ~= nil and type(t) == "number" then
                        if t > 32503680000 then
                            t = math.floor(t / 1000)
                        else
                            t = math.floor(t)
                        end
                    end
                    if type(fmt) == "string" then
                        fmt = fmt:gsub("%%F", "%%Y-%%m-%%d")
                        fmt = fmt:gsub("%%T", "%%H:%%M:%%S")
                    end
                    return _os_date(fmt, t)
                end
            end
        """))

        lua.execute(_lua_src(r"""
            if not os.sleep then
                os.sleep = function(t)
                    if computer and computer.pullSignal then
                        computer.pullSignal(t)
                    end
                end
            end
        """))

        lua.execute(_lua_src(r"""
            do
                if math.pow == nil then
                    math.pow = function(x, y) return x ^ y end
                end
                if math.ldexp == nil then
                    math.ldexp = function(m, e) return m * 2.0 ^ e end
                end
                if math.frexp == nil then
                    math.frexp = function(x)
                        if x == 0 or x ~= x or x == math.huge
                           or x == -math.huge then
                            return x, 0
                        end
                        local e = math.floor(math.log(math.abs(x)) / math.log(2)) + 1
                        local m = x / (2 ^ e)
                        return m, e
                    end
                end
                if math.atan2 == nil then
                    math.atan2 = function(y, x) return math.atan(y, x) end
                end
                if math.cosh == nil then
                    math.cosh = function(x)
                        return (math.exp(x) + math.exp(-x)) / 2
                    end
                end
                if math.sinh == nil then
                    math.sinh = function(x)
                        return (math.exp(x) - math.exp(-x)) / 2
                    end
                end
                if math.tanh == nil then
                    math.tanh = function(x)
                        local ex = math.exp(2 * x)
                        return (ex - 1) / (ex + 1)
                    end
                end
                if math.log10 == nil then
                    math.log10 = function(x) return math.log(x, 10) end
                end

                if bit32 == nil then
                    bit32 = {}
                    bit32.band = function(a, b)
                        if b == nil then return a end
                        return a & b
                    end
                    bit32.bor = function(a, b)
                        if b == nil then return a end
                        return a | b
                    end
                    bit32.bxor = function(a, b)
                        if b == nil then return a end
                        return a ~ b
                    end
                    bit32.bnot = function(a) return ~a end
                    bit32.lshift = function(a, n)
                        return (a << n) & 0xFFFFFFFF
                    end
                    bit32.rshift = function(a, n)
                        return (a & 0xFFFFFFFF) >> n
                    end
                    bit32.arshift = function(a, n)
                        a = a & 0xFFFFFFFF
                        if a >= 0x80000000 then a = a - 0x100000000 end
                        local r = a >> n
                        return r & 0xFFFFFFFF
                    end
                    bit32.extract = function(n, field, width)
                        width = width or 1
                        return (n >> field) & ((1 << width) - 1)
                    end
                    bit32.replace = function(n, v, field, width)
                        width = width or 1
                        local mask = ((1 << width) - 1) << field
                        return (n & ~mask) | ((v << field) & mask)
                    end
                    bit32.btest = function(...)
                        local args = table.pack(...)
                        local acc = 0
                        for i = 1, args.n do
                            acc = acc | args[i]
                        end
                        return (acc & 0xFFFFFFFF) ~= 0
                    end
                    _G.bit32 = bit32
                end
            end
        """))

        lua.execute(_lua_src(r"""
            os.exit = function(code)
                error({reason="terminated", code=code or 0}, 0)
            end
        """))

        lua.execute(_lua_src(r"""
            do
                local _G      = _G
                local _pcall  = pcall
                local _xpcall = xpcall
                local _type   = type
                local _tostr  = tostring
                local _orig_require = rawget(_G, "require")

                local function _fallback_gpl()
                    local out = {}
                    local path = nil
                    local os_m = _G.os
                    if _type(os_m) == "table"
                       and _type(os_m.getenv) == "function" then
                        local ok, v = _pcall(os_m.getenv, "PATH")
                        if ok and _type(v) == "string" and #v > 0 then
                            path = v
                        end
                    end
                    if not path or #path == 0 then
                        path = "/bin:/usr/bin"
                    end
                    for seg in path:gmatch("[^:]+") do
                        out[#out + 1] = seg
                    end
                    if #out == 0 then
                        out = {"/bin", "/usr/bin"}
                    end
                    return out
                end

                local function _ensure_pkg(pkg)
                    if _type(pkg) ~= "table" then return end
                    if _type(pkg.getProgramLocations) ~= "function" then
                        pkg.getProgramLocations = _fallback_gpl
                    end
                end

                local function _patch_all()
                    _ensure_pkg(_G.package)
                    local p = _G.package
                    if _type(p) == "table"
                       and _type(p.loaded) == "table" then
                        for k, mod in pairs(p.loaded) do
                            if _type(k) == "string"
                               and k:find("package", 1, true) then
                                _ensure_pkg(mod)
                            end
                        end
                    end
                end

                if _type(_orig_require) == "function" then
                    _G.require = function(name, ...)
                        local mod = _orig_require(name, ...)
                        if _type(name) == "string"
                           and name:find("package", 1, true) then
                            _ensure_pkg(mod)
                        end
                        return mod
                    end
                end

                function _lupi_errMsg(err)
                    if _type(err) == "string" then return err end
                    if _type(err) == "table" and err.reason then return err end
                    
                    local mt = getmetatable(err)
                    if _type(mt) == "table" and mt.__tostring then
                        local ok, s = _pcall(mt.__tostring, err)
                        if ok and _type(s) == "string" then return s end
                    end
                    local ok2, s2 = _pcall(function() return err.message end)
                    if ok2 and _type(s2) == "string" then return s2 end
                    local ok3, s3 = _pcall(function() return err.args end)
                    if ok3 and _type(s3) == "table" and #s3 > 0 then
                        return _tostr(s3[1])
                    end
                    return "<lua error: " .. _type(err) .. ">"
                end

                _G.pcall = function(f, ...)
                    _patch_all()
                    local r = table.pack(_pcall(f, ...))
                    if not r[1] then 
                        r[2] = _lupi_errMsg(r[2]) 
                    end
                    return table.unpack(r, 1, r.n)
                end

                _G.xpcall = function(f, handler, ...)
                    _patch_all()
                    local args = table.pack(...)
                    return _xpcall(f, function(err)
                        return handler(_lupi_errMsg(err))
                    end, table.unpack(args, 1, args.n))
                end

                _patch_all()
            end
        """))
        
        _make_callable_table = lua.eval(_lua_src("""
            function(tbl)
                return setmetatable(tbl, {
                    __call = function(self, prev)
                        local next_idx = 1
                        if prev ~= nil then
                            for i = 1, #tbl do
                                if tbl[i] == prev then
                                    next_idx = i + 1
                                    break
                                end
                            end
                        end
                        if next_idx <= #tbl then
                            return tbl[next_idx]
                        end
                    end
                })
            end
        """))

        def _lua_convert(value):
            if isinstance(value, filesystem_mod.LuaIterable):
                tbl = lua.table()
                for i, v in enumerate(value.items, 1):
                    tbl[i] = _lua_convert(v)
                return _make_callable_table(tbl)
            if isinstance(value, list):
                tbl = lua.table()
                for i, v in enumerate(value, 1):
                    tbl[i] = _lua_convert(v)
                return tbl
            if isinstance(value, dict):
                tbl = lua.table()
                for k, v in value.items():
                    tbl[k] = _lua_convert(v)
                return tbl
            if isinstance(value, tuple):
                return tuple(_lua_convert(v) for v in value)
            if isinstance(value, str):
                try:
                    value.encode('latin-1')
                    return value
                except UnicodeEncodeError:
                    return value.encode('utf-8').decode('latin-1')
            return value

        component_mod.set_result_converter(_lua_convert)

        def _pack_result(result):
            tbl = lua.table()
            if isinstance(result, tuple):
                tbl["n"] = len(result)
                for i, v in enumerate(result, 1):
                    tbl[i] = _lua_convert(v)
            else:
                tbl["n"] = 1
                tbl[1] = _lua_convert(result)
            return tbl

        def _invoke_packed(address, method, *args):
            try:
                r = component_mod.invoke(address, method, *args)
            except Exception as e:
                tb = traceback.format_exc()
                logger.error("component.invoke(%s, %s) raised:\n%s",
                             address, method, tb)
                tbl = lua.table()
                tbl["n"] = 0
                safe_err = f"{type(e).__name__}: {e}".encode('latin-1', errors='replace').decode('latin-1')
                tbl["error"] = safe_err
                return tbl
            return _pack_result(r)

        comp = lua.table()
        comp._invokePacked = _invoke_packed
        comp.methods       = component_mod.methods
        comp.fields        = component_mod.fields
        comp.doc           = component_mod.doc
        comp.getType       = component_mod.get_type
        comp.slot          = component_mod.slot
        comp.exists        = component_mod.exists
        comp.connect       = component_mod.connect
        comp.disconnect    = component_mod.disconnect
        G.component = comp

        lua.execute(_lua_src(r"""
            local c = component
            local _ip = c._invokePacked
            c.invoke = function(...)
                local t = _ip(...)
                if t.error then error(t.error, 2) end
                return table.unpack(t, 1, t.n)
            end
        """))

        def _components_as_lua_table():
            tbl = lua.table()
            for addr, entry in component_mod._components.items():
                tbl[addr] = entry.type
            return tbl

        comp._components = _components_as_lua_table

        def _proxy_as_lua_table(address):
            entry = component_mod._components.get(address)
            if entry is None:
                return None
            tbl = lua.table()
            for k, v in entry.proxy.items():
                tbl[k] = _lua_convert(v)
            tbl["address"] = entry.address
            tbl["type"] = entry.type
            return tbl

        comp._proxyLua = _proxy_as_lua_table

        lua.execute(_lua_src(r"""
            local comp_tbl = component

            local function _components_lua()
                return comp_tbl._components()
            end

            function comp_tbl.list(filter, exact)
                if type(filter) == "table" then
                    exact = filter.exact
                    filter = filter.filter
                end
                local all = _components_lua()
                local result = {}
                for address, ctype in pairs(all) do
                    if filter == nil
                        or (exact and ctype == filter)
                        or (not exact and ctype:sub(1, #filter) == filter) then
                        result[address] = ctype
                    end
                end
                local mt = {
                    __call = function(tbl, _, prev)
                        return next(tbl, prev)
                    end
                }
                return setmetatable(result, mt), result, nil
            end

            function comp_tbl.proxy(address)
                if type(address) ~= "string" then
                    error("bad argument #1 (string expected, got " .. type(address) .. ")", 2)
                end
                return comp_tbl._proxyLua(address)
            end

            function comp_tbl.isAvailable(ctype)
                local it = comp_tbl.list(ctype)
                return it() ~= nil
            end

            function comp_tbl.getPrimary(ctype)
                local it = comp_tbl.list(ctype)
                local addr = it()
                if addr then return comp_tbl.proxy(addr) end
            end

            function comp_tbl.type(proxyOrAddr)
                if type(proxyOrAddr) == "table" then
                    return proxyOrAddr.type
                elseif type(proxyOrAddr) == "string" then
                    return comp_tbl.getType(proxyOrAddr)
                end
            end
        """))

        computer = lua.table()
        computer.address        = computer_mod.get_address
        computer.tmpAddress     = computer_mod.tmp_address
        computer.uptime         = computer_mod.uptime
        computer.realTime       = computer_mod.real_time
        computer.energy         = computer_mod.energy
        computer.maxEnergy      = computer_mod.max_energy
        computer.freeMemory     = computer_mod.free_memory
        computer.totalMemory    = computer_mod.total_memory
        computer.pushSignal     = computer_mod.push_signal
        computer.beep           = _lua_beep
        computer.shutdown       = computer_mod.shutdown
        computer.getBootAddress = lambda: component_mod.invoke(eeprom_addr, "getData")
        computer.setBootAddress = lambda addr=None: component_mod.invoke(eeprom_addr, "setData", addr or "")
        computer.getDeviceInfo  = lambda: _lua_convert(computer_mod.get_device_info())

        computer.getArchitectures    = lambda: _lua_convert(computer_mod.get_architectures())
        computer.getArchitecture     = computer_mod.get_architecture
        computer.setArchitecture     = computer_mod.set_architecture
        computer.addUser             = computer_mod.add_user
        computer.removeUser          = computer_mod.remove_user
        computer.getProgramLocations = lambda: _lua_convert(computer_mod.get_program_locations())

        G.computer = computer

        def _pull_signal_packed(timeout=None):
            try:
                r = computer_mod.pull_signal(timeout)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                tb = traceback.format_exc()
                logger.error("computer.pullSignal raised:\n%s", tb)
                tbl = lua.table()
                tbl["n"] = 0
                safe_err = f"{type(e).__name__}: {e}".encode('latin-1', errors='replace').decode('latin-1')
                tbl["error"] = safe_err
                return tbl
            if r is None:
                return None
            return _pack_result(r)

        computer._pullPacked = _pull_signal_packed

        lua.execute(_lua_src(r"""
            function computer.users()
                local users = { "User" }
                local i = 0
                return function()
                    i = i + 1
                    return users[i]
                end
            end
        """))

        lua.execute(_lua_src(r"""
            local c = computer
            local _p = c._pullPacked
            c.pullSignal = function(t)
                local r = _p(t)
                if r == nil then return nil end
                if r.error then error(r.error, 2) end
                return table.unpack(r, 1, r.n)
            end
        """))

        if gpu_addr:
            try:
                G.gpu = component_mod.proxy(gpu_addr)
                screen_iter, _ = component_mod.list("screen")
                screen_pair = screen_iter()
                screen_addr = screen_pair[0] if screen_pair else None
                if screen_addr:
                    G.screen = component_mod.proxy(screen_addr)
            except Exception as e:
                lprint(f"Warning: Could not expose GPU/Screen to Lua: {e}")

        internet_iter, _ = component_mod.list("internet")
        internet_pair = internet_iter()
        internet_addr = internet_pair[0] if internet_pair else None
        if internet_addr:
            G.internet = component_mod.proxy(internet_addr)

        lua.execute(_lua_src("""
            function checkArg(n, value, ...)
                local types = {...}
                if #types == 0 then return true end
                local t = type(value)
                for _, expected in ipairs(types) do
                    if t == expected then return true end
                end
                error(string.format("bad argument #%d (%s expected, got %s)",
                    n, table.concat(types, " or "), t), 3)
            end
        """))

        baseline_kb = 0.0
        if _mem_fn is not None:
            try:
                baseline_kb = float(_mem_fn())
            except Exception:
                baseline_kb = 0.0
        computer_mod.set_memory_baseline(int(baseline_kb * 1024.0))
        lprint(f"Memory baseline captured: {baseline_kb:.1f} KB")

        if enforce_ram:
            total_kb = computer_mod.total_memory() / 1024.0
            _install_memory_hook(lua, total_kb, baseline_kb=baseline_kb)
        else:
            logger.info("RAM enforcement disabled (--no-ram-enforce)")

        used, total, ratio = computer_mod.memory_stats()
        lprint(f"Before BIOS: used={used/1024.0:.1f} KiB "
               f"raw={computer_mod.raw_memory_used()/1024.0:.1f} KiB "
               f"total={total/1024.0:.1f} KiB ({ratio*100:.1f}%)")

        lprint("Lua runtime ready. Running BIOS...")
        try:
            lua.execute(bios_code)
            lprint("✓ BIOS completed.")
        except Exception as e:
            msg = str(e)
            if "not enough memory" in msg.lower():
                try:
                    used_kb = computer_mod.used_memory() / 1024.0
                except Exception:
                    used_kb = 0.0
                limit_kb = computer_mod.total_memory() / 1024.0
                _oom_screen(used_kb, limit_kb, msg)
                return
            logger.exception("Lua execution error")
            safe_msg = msg.encode('latin-1', errors='replace').decode('latin-1')
            _bsod(f"Lua error: {type(e).__name__}: {safe_msg}")
            return

        _lua_event_loop()

    except Exception as e:
        msg = str(e)
        if "not enough memory" in msg.lower():
            try:
                used_kb = computer_mod.used_memory() / 1024.0
            except Exception:
                used_kb = 0.0
            limit_kb = computer_mod.total_memory() / 1024.0
            _oom_screen(used_kb, limit_kb, msg)
            return
        logger.exception("Lua execution error")
        safe_msg = msg.encode('latin-1', errors='replace').decode('latin-1')
        _bsod(f"Lua error: {type(e).__name__}: {safe_msg}")


def _lua_event_loop():
    lprint("Entering main event loop (press Ctrl+C to exit)...")
    try:
        while True:
            signal = computer_mod.pull_signal(timeout=0.1)
            if signal:
                logger.debug("Signal received: %s", signal[0])
    except KeyboardInterrupt:
        lprint("Shutdown by user (Ctrl+C)")
    except Exception:
        logger.exception("Event loop crashed")
    finally:
        computer_mod.shutdown()


if __name__ == "__main__":
    exit_code = 0
    try:
        main()
    except KeyboardInterrupt:
        pass
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except Exception:
        logger.exception("Fatal error in main()")
        exit_code = 1
    finally:
        try:
            from core import computer as _c
            _c.shutdown()
        except Exception:
            pass
        _close_log_stream()
        os._exit(exit_code)