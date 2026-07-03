#!/usr/bin/env python3
"""生成 RayShark 应用图标：macOS 圆角(squircle 近似) + 蓝色渐变 + 鲨鱼鳍。
纯 stdlib(zlib) 输出干净 RGBA PNG(仅 IHDR/IDAT/IEND)，避开 Pillow 签名问题，
也规避移动端解码器对多余 chunk 的兼容问题。

用法: gen_icon.py <out.png> <size>
坐标在 1024 设计空间定义，按输出尺寸重采样。
"""
import struct
import zlib
import sys

D = 1024.0  # 设计空间边长
RADIUS = 0.2237 * D  # macOS 圆角半径 ≈ 22.37% 边长


def lerp(a, b, t):
    return a + (b - a) * t


def bg_color(y):
    t = y / (D - 1)
    r = int(lerp(0x2b, 0x0b, t))
    g = int(lerp(0x6c, 0x3a, t))
    b = int(lerp(0xff, 0xa8, t))
    return r, g, b


def in_rounded_rect(x, y):
    r = RADIUS
    cx = min(max(x, r), D - 1 - r)
    cy = min(max(y, r), D - 1 - r)
    dx = x - cx
    dy = y - cy
    return dx * dx + dy * dy <= r * r


def point_in_poly(x, y, poly):
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


FIN = [
    (300, 690), (360, 560), (450, 420), (560, 320), (700, 300),
    (660, 400), (648, 520), (636, 640), (600, 680),
    (500, 640), (400, 645), (330, 675),
]
EYE = (560, 440, 28)


def sample(x, y):
    """返回设计空间点 (x,y) 的 RGBA。"""
    if not in_rounded_rect(x, y):
        return (0, 0, 0, 0)
    r, g, b = bg_color(y)
    if y > 720:
        wave = 0.35 if y > 800 else 0.2
        r = int(r * (1 - wave)); g = int(g * (1 - wave)); b = int(b * (1 - wave))
    if point_in_poly(x, y, FIN):
        r, g, b = 245, 249, 255
    ex, ey, er = EYE
    if (x - ex) ** 2 + (y - ey) ** 2 <= er * er:
        r, g, b = 0x0b, 0x3a, 0xa8
    return (r, g, b, 255)


def build_rows(size, ss=2):
    """size: 输出边长。ss: 每像素超采样倍数(抗锯齿)。"""
    rows = bytearray()
    scale = D / size
    for oy in range(size):
        row = bytearray()
        row.append(0)
        for ox in range(size):
            ar = ag = ab = aa = 0
            for sy in range(ss):
                for sx in range(ss):
                    dx = (ox + (sx + 0.5) / ss) * scale
                    dy = (oy + (sy + 0.5) / ss) * scale
                    r, g, b, a = sample(dx, dy)
                    ar += r * a; ag += g * a; ab += b * a; aa += a
            n = ss * ss
            if aa == 0:
                row += bytes((0, 0, 0, 0))
            else:
                row += bytes((ar // aa, ag // aa, ab // aa, aa // n))
        rows += row
    return bytes(rows), size


def png_chunk(tag, data):
    c = struct.pack(">I", len(data)) + tag + data
    crc = zlib.crc32(tag + data) & 0xffffffff
    return c + struct.pack(">I", crc)


def write_png(path, size):
    raw, sz = build_rows(size)
    comp = zlib.compress(raw, 9)
    ihdr = struct.pack(">IIBBBBB", sz, sz, 8, 6, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(png_chunk(b"IHDR", ihdr))
        f.write(png_chunk(b"IDAT", comp))
        f.write(png_chunk(b"IEND", b""))


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "icon_256.png"
    size = int(sys.argv[2]) if len(sys.argv) > 2 else 256
    write_png(out, size)
    print("wrote", out, size)
