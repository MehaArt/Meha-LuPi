"""
util/color.py — порт src/lua/core/util/color.lua
Цветовые преобразования: RGB <-> YUV, RGB <-> HSV, поиск ближайшего цвета.
"""


def rgb_to_yuv(r: float, g: float, b: float) -> tuple[float, float, float]:
    y = r * 0.299 + g * 0.587 + b * 0.114
    u = r * (-0.147) + g * (-0.289) + b * 0.436
    v = r * 0.615 + g * (-0.515) + b * (-0.1)
    return y, u, v


def rgb_to_hsv(r: float, g: float, b: float) -> tuple[float, float, float]:
    min_c = min(r, g, b)
    max_c = max(r, g, b)
    v = max_c
    delta = max_c - min_c

    if delta < 0.00001:
        return 0.0, 0.0, v

    if max_c != 0:
        s = delta / max_c
    else:
        return -1.0, 0.0, v

    if r == max_c:
        h = (g - b) / delta
    elif g == max_c:
        h = 2.0 + (b - r) / delta
    else:
        h = 4.0 + (r - g) / delta

    h *= 60.0
    if h < 0:
        h += 360.0
    return h, s, v


def hsv_to_rgb(h: float, s: float, v: float) -> tuple[float, float, float]:
    if s == 0:
        return v, v, v

    h /= 60.0
    i = int(h)
    f = h - i
    p = v * (1 - s)
    q = v * (1 - s * f)
    t = v * (1 - s * (1 - f))

    if i == 0:
        return v, t, p
    elif i == 1:
        return q, v, p
    elif i == 2:
        return p, v, t
    elif i == 3:
        return p, q, v
    elif i == 4:
        return t, p, v
    else:
        return v, p, q


def nearest(to: int, colors: dict) -> object:
    """Найти ключ ближайшего цвета из словаря colors к цвету to (0xRRGGBB)."""
    lowest = float("inf")
    lowest_k = None

    th, ts, tv = rgb_to_yuv(
        (to & 0xFF0000) >> 16,
        (to & 0xFF00) >> 8,
        to & 0xFF,
    )

    for k, col in colors.items():
        if col == to:
            return k
        h, s, v = rgb_to_yuv(
            (col & 0xFF0000) >> 16,
            (col & 0xFF00) >> 8,
            col & 0xFF,
        )
        d = abs(h - th) + abs(s - ts) + abs(v - tv)
        if d < lowest:
            lowest = d
            lowest_k = k

    return lowest_k